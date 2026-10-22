"""per-user 网络命名空间（M2：网络隔离）。

**每个 user 一个 netns**（不是每沙箱一个）：

- user 内：沙箱与服务都在同一个 netns → `127.0.0.1:16379` 语义保留
  （现有测试连 127.0.0.1 连的就是自己的服务，不用改一行）
- user 间：不同网段 → 防火墙按 IP 隔离 → **端口级隔离**
  （同机不同端口挡不住的问题，这里解决了）
- 沙箱 → berth 自己的 API：走宿主 IP 也被 INPUT 规则挡住（127.0.0.1 在 ns 里
  根本不是宿主）→ token 降级成第二道防线

拓扑：

```
  宿主 eth0 192.168.5.15 ──┐
                            │  PREROUTING DNAT: :16379 → 10.200.3.2:16379
      ┌─────────────────────┴──────────────────────┐
      │ 宿主 netns                                 │
      │   veth<idx>h 10.200.3.1/24  ── veth<idx>n  │
      │   FORWARD: 10.200/16 → 10.200/16 DROP      │
      │   INPUT:   10.200/16 → 22,9000 DROP        │
      │   POSTROUTING: -s 10.200.3.0/24 -o v<idx>h MASQUERADE │
      └────────────────────────────────────────────┘
                          │
              ┌───────────┴────────────┐
              │ netns berth-<user>      │
              │   10.200.3.2/24         │
              │   lo up                 │
              │   default via 10.200.3.1│
              │   沙箱 + 服务都在这里    │
              │   127.0.0.1:16379 = 本 user 的服务 │
              └─────────────────────────┘
```

开关：`BERTH_NETNS=0` → 全部退回 host 网络模式（向后兼容，出问题一键回退）。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from sandbox import config

log = logging.getLogger(__name__)

NETNS_PREFIX = "berth-"
SUBNET_BASE = "10.200"          # 用户网段：10.200.<idx>.0/24
GATEWAY_OFFSET = 1               # 宿主侧 .1
HOST_OFFSET = 2                  # ns 侧 .2
VETH_PREFIX = "v"                # veth 名：v<idx>h / v<idx>n（≤15 字符）

# 宿主上要挡住 ns 的端口（sshd、berth API…）
DEFAULT_BLOCK_PORTS = (22, 9000)

_COMMENT = "berth"


def enabled() -> bool:
    """是否启用 netns 隔离。"""
    return config.setting("BERTH_NETNS", "1") not in ("0", "false", "no", "off")


def _run(
    cmd: list[str], check: bool = True, stdin: str | None = None
) -> subprocess.CompletedProcess:
    """跑一条特权命令（root 直接跑，否则 sudo -n）。"""
    argv = cmd if os.geteuid() == 0 else ["sudo", "-n", *cmd]
    res = subprocess.run(
        argv, capture_output=True, text=True, input=stdin if stdin is not None else ""
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd)} 失败: rc={res.returncode}\n{res.stderr.strip()}"
        )
    return res


def _iptables(table: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["iptables", "-t", table, *args], check=check)


def available() -> bool:
    """这台机器能不能用（ip / iptables / netns 都能用）。"""
    if not shutil.which("ip") or not shutil.which("iptables"):
        return False
    try:
        _run(["ip", "netns", "list"], check=False)
    except Exception:
        return False
    return True


# ─────────────────────────── 命名与地址 ───────────────────────────


def netns_name(user: str) -> str:
    return f"{NETNS_PREFIX}{user}"


def veth_names(idx: int) -> tuple[str, str]:
    return f"{VETH_PREFIX}{idx}h", f"{VETH_PREFIX}{idx}n"


def subnet_for(idx: int) -> str:
    return f"{SUBNET_BASE}.{idx}.0/24"


def ip_for(idx: int) -> str:
    return f"{SUBNET_BASE}.{idx}.{HOST_OFFSET}"


def gateway_for(idx: int) -> str:
    return f"{SUBNET_BASE}.{idx}.{GATEWAY_OFFSET}"


def index_of(cidr: str | None) -> int | None:
    """从网段字符串反推用户序号（10.200.3.0/24 → 3）。"""
    if not cidr:
        return None
    try:
        net = ipaddress.ip_network(str(cidr), strict=False)
    except ValueError:
        return None
    parts = str(net).split(".")
    if len(parts) < 3 or parts[0] != SUBNET_BASE.split(".")[0] or parts[1] != SUBNET_BASE.split(".")[1]:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def allocate_index(used: set[int]) -> int:
    """给新 user 分配一个网段序号（跳过已用的）。"""
    idx = 1
    while idx in used:
        idx += 1
    return idx


# ─────────────────────────── 生命周期 ───────────────────────────


def ensure_netns(user: str, cidr: str | None = None) -> dict:
    """确保 user 的 netns 存在（不存在就建）。返回 {netns, cidr, ip, gateway}。

    幂等：已存在就只补齐缺失的地址/路由。
    """
    from state.db import ensure_user, get_user, list_users, update_user

    if not enabled():
        return {"netns": None, "cidr": cidr, "ip": None, "gateway": None, "enabled": False}

    row = get_user(user) or ensure_user(user)
    cidr = cidr or row.get("cidr")
    if not cidr:
        used = {index_of(u.get("cidr")) for u in list_users()}
        used.discard(None)
        idx = allocate_index({i for i in used if i is not None})
        cidr = subnet_for(idx)
        update_user(user, cidr=cidr)
    idx = index_of(cidr)
    if idx is None:
        raise ValueError(f"user {user} 的网段不合法: {cidr}")

    ns = netns_name(user)
    host_veth, ns_veth = veth_names(idx)
    gw, ip = gateway_for(idx), ip_for(idx)

    # 1) netns
    if not _netns_exists(ns):
        _run(["ip", "netns", "add", ns])
        log.info("已创建 netns: %s", ns)
    _run(["ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up"], check=False)

    # 2) veth 对（ns 侧要搬进 netns！）
    if _link_exists(host_veth) and not _ns_link_exists(ns, ns_veth):
        # 半残的（netns 重建过）：删掉重来
        _run(["ip", "link", "del", host_veth], check=False)
    if not _link_exists(host_veth):
        _run(["ip", "link", "add", host_veth, "type", "veth", "peer", "name", ns_veth])
        _run(["ip", "link", "set", ns_veth, "netns", ns])
    # 宿主侧
    _run(["ip", "addr", "replace", f"{gw}/24", "dev", host_veth], check=False)
    _run(["ip", "link", "set", host_veth, "up"], check=False)
    # ns 侧
    _run(["ip", "netns", "exec", ns, "ip", "addr", "replace", f"{ip}/24", "dev", ns_veth], check=False)
    _run(["ip", "netns", "exec", ns, "ip", "link", "set", ns_veth, "up"], check=False)
    _run(["ip", "netns", "exec", ns, "ip", "route", "replace", "default", "via", gw], check=False)

    # 3) DNS：/etc/netns/<ns>/resolv.conf 会被 ip netns exec 自动挂载
    _write_ns_resolvconf(ns)

    # 4) 转发 + 防火墙
    _enable_forwarding()
    _ensure_global_rules()
    _ensure_user_rules(user, idx)

    return {
        "netns": ns,
        "cidr": cidr,
        "ip": ip,
        "gateway": gw,
        "index": idx,
        "enabled": True,
    }


def destroy_netns(user: str) -> dict:
    """删掉 user 的 netns（连带防火墙规则）。"""
    from state.db import get_user, update_user

    row = get_user(user) or {}
    idx = index_of(row.get("cidr"))
    ns = netns_name(user)
    if idx is not None:
        _remove_user_rules(user, idx)
    if _netns_exists(ns):
        _run(["ip", "netns", "del", ns], check=False)
        log.info("已删除 netns: %s", ns)
    update_user(user, cidr=None)
    return {"netns": ns, "deleted": True}


def _netns_exists(ns: str) -> bool:
    res = _run(["ip", "netns", "list"], check=False)
    return any(line.split()[0] == ns for line in res.stdout.splitlines() if line.strip())


def _link_exists(name: str) -> bool:
    res = _run(["ip", "link", "show", name], check=False)
    return res.returncode == 0


def _ns_link_exists(ns: str, name: str) -> bool:
    """这个接口在 netns 里吗。"""
    res = _run(["ip", "netns", "exec", ns, "ip", "link", "show", name], check=False)
    return res.returncode == 0


def _write_ns_resolvconf(ns: str) -> None:
    """把宿主的 DNS 配置写进 /etc/netns/<ns>/resolv.conf（ip netns exec 会自动挂载）。"""
    try:
        src = Path("/etc/resolv.conf").read_text(encoding="utf-8")
    except OSError:
        src = "nameserver 1.1.1.1\n"
    # 127.0.0.x（systemd-resolved 的本地 stub）在 ns 里不可达 → 换成公共 DNS
    if re.search(r"nameserver\s+127\.0\.0\.\d+", src):
        src = "nameserver 1.1.1.1\nnameserver 8.8.8.8\n"
    path = Path("/etc/netns") / ns
    try:
        path.mkdir(parents=True, exist_ok=True)
        (path / "resolv.conf").write_text(src, encoding="utf-8")
    except OSError as e:
        log.warning("写 %s 失败: %s", path / "resolv.conf", e)


# ─────────────────────────── 防火墙 ───────────────────────────


def _enable_forwarding() -> None:
    """打开 ip 转发（沙箱要靠它出网）+ route_localnet（127.0.0.1 的 DNAT hairpin 要靠它）。"""
    _run(["sh", "-c", "echo 1 > /proc/sys/net/ipv4/ip_forward"], check=False)
    # 宿主本地访问 127.0.0.1:port 被 DNAT 到网段地址时，需要这个
    _run(["sh", "-c", "echo 1 > /proc/sys/net/ipv4/conf/all/route_localnet"], check=False)


def _rule_args(line: str) -> list[str]:
    """把 `iptables -S` 的一行变成 `-D` 需要的参数。

    要跳过 "-A <chain>"，并去掉引号（-S 输出里 comment 的值是带引号的）。
    """
    spec = line.split()
    return [tok.strip('"') for tok in spec[2:]]


def _chain_rules(table: str, chain: str) -> list[str]:
    res = _iptables(table, "-S", chain, check=False)
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def _comment_of(line: str) -> str:
    """取规则里 --comment 的值（精确匹配用）。"""
    parts = line.split()
    for i, part in enumerate(parts):
        if part == "--comment" and i + 1 < len(parts):
            return parts[i + 1].strip('"')
    return ""


def _rules_with_comment(table: str, chain: str, comment: str) -> list[str]:
    """链里带这个 comment（精确匹配）的规则。"""
    return [line for line in _chain_rules(table, chain) if _comment_of(line) == comment]


def _rule_exists(table: str, chain: str, comment: str) -> bool:
    """这条链里有没有带这个 comment 的规则。

    注意：不能用 `iptables -C` 只带 comment 去查 —— 实际规则还带别的匹配条件，
    那样永远查不到 → 每次都重复添加。
    """
    return bool(_rules_with_comment(table, chain, comment))


def _ensure_rule(table: str, chain: str, comment: str, args: list[str]) -> None:
    """确保链里有且只有一条带该 comment 的规则（先删旧的，再按当前定义加）。

    这样规则定义改了（比如加了 -o 或改了匹配条件）也能生效，不会留着旧版。
    """
    for line in _rules_with_comment(table, chain, comment):
        _iptables(table, "-D", chain, *_rule_args(line), check=False)
    _iptables(table, "-A", chain, *args, check=False)


def _dedupe_rules() -> int:
    """去掉重复的 berth 规则（历史上重复添加过的）。"""
    removed = 0
    for table, chain in (("nat", "POSTROUTING"), ("nat", "PREROUTING"), ("nat", "OUTPUT"),
                         ("filter", "FORWARD"), ("filter", "INPUT")):
        seen = set()
        for line in _chain_rules(table, chain):
            if f"{_COMMENT}:" not in line:
                continue
            comment = ""
            parts = line.split()
            for i, part in enumerate(parts):
                if part == "--comment" and i + 1 < len(parts):
                    comment = parts[i + 1]     # 取 comment 的**值**，不是 "--comment" 本身
            key = comment or line
            if key in seen:
                _iptables(table, "-D", chain, *_rule_args(line), check=False)
                removed += 1
            else:
                seen.add(key)
    if removed:
        log.info("清理了 %d 条重复的防火墙规则", removed)
    return removed


def _ensure_global_rules() -> None:
    """全局规则（只建一次）：

    - FORWARD：10.200/16 → 10.200/16 全 DROP（user 之间不通）
    - INPUT：10.200/16 → 宿主敏感端口 DROP（沙箱连不到 sshd / berth API）
    """
    # user 之间不通
    _ensure_rule(
        "filter", "FORWARD", f"{_COMMENT}:cross-user",
        ["-s", f"{SUBNET_BASE}.0.0/16", "-d", f"{SUBNET_BASE}.0.0/16",
         "-m", "comment", "--comment", f"{_COMMENT}:cross-user", "-j", "DROP"],
    )
    # 沙箱 → 宿主敏感端口
    ports = _block_ports()
    if ports:
        _ensure_rule(
            "filter", "INPUT", f"{_COMMENT}:host-block",
            ["-s", f"{SUBNET_BASE}.0.0/16", "-p", "tcp",
             "-m", "multiport", "--dports", ",".join(str(p) for p in ports),
             "-m", "comment", "--comment", f"{_COMMENT}:host-block", "-j", "DROP"],
        )


def _block_ports() -> list[int]:
    raw = config.setting("BERTH_HOST_BLOCK_PORTS")
    if raw is None:
        return list(DEFAULT_BLOCK_PORTS)
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _ensure_user_rules(user: str, idx: int) -> None:
    """user 级别的规则：出网 NAT。"""
    cidr = subnet_for(idx)
    host_veth, _ = veth_names(idx)
    # 只按**源地址**匹配：流量是从 veth 进来、从 eth0 出去的
    # （写 -o <veth> 匹配不到，源地址不会被 NAT，出网就没回包）
    comment = f"{_COMMENT}:{user}:nat"
    _ensure_rule(
        "nat", "POSTROUTING", comment,
        ["-s", cidr, "-m", "comment", "--comment", comment, "-j", "MASQUERADE"],
    )


def _remove_user_rules(user: str, idx: int) -> None:
    """删掉 user 的所有规则（按 comment 前缀匹配）。"""
    prefix = f"{_COMMENT}:{user}:"
    for table, chain in (("nat", "POSTROUTING"), ("nat", "PREROUTING"), ("nat", "OUTPUT")):
        while True:
            lines = [
                l for l in _chain_rules(table, chain)
                if l.startswith("-A") and _comment_of(l).startswith(prefix)
            ]
            if not lines:
                break
            _iptables(table, "-D", chain, *_rule_args(lines[0]), check=False)


def add_service_port(user: str, port: int) -> None:
    """把服务端口发布出去（宿主 :port → user netns 里的同号端口）。

    - PREROUTING：外部客户端 → 宿主 IP:port
    - OUTPUT：宿主本地（127.0.0.1:port，平台/管理员在宿主上连）→ hairpin
    """
    if not enabled():
        return
    from state.db import get_user

    row = get_user(user) or {}
    idx = index_of(row.get("cidr"))
    if idx is None:
        ensure_netns(user)
        row = get_user(user) or {}
        idx = index_of(row.get("cidr"))
    if idx is None:
        return
    ip = ip_for(idx)
    for chain, extra in (
        ("PREROUTING", ["-p", "tcp", "--dport", str(port)]),
        ("OUTPUT", ["-d", "127.0.0.1", "-p", "tcp", "--dport", str(port)]),
    ):
        comment = f"{_COMMENT}:{user}:{port}"
        _ensure_rule(
            "nat", chain, comment,
            [*extra, "-m", "comment", "--comment", comment,
             "-j", "DNAT", "--to-destination", f"{ip}:{port}"],
        )
    # hairpin：宿主本地访问时，源地址是 127.0.0.1 → 改写成网关 IP（否则 ns 里的回包走不通）
    comment = f"{_COMMENT}:{user}:{port}:hair"
    _ensure_rule(
        "nat", "POSTROUTING", comment,
        ["-s", "127.0.0.1", "-d", ip, "-p", "tcp", "--dport", str(port),
         "-m", "comment", "--comment", comment, "-j", "MASQUERADE"],
    )


def remove_service_port(user: str, port: int) -> None:
    """撤掉服务端口的发布规则。"""
    if not enabled():
        return
    for table, chain in (("nat", "PREROUTING"), ("nat", "OUTPUT"), ("nat", "POSTROUTING")):
        comment = f"{_COMMENT}:{user}:{port}"
        while _rule_exists("nat", chain, comment):
            res = _iptables(table, "-S", chain, check=False)
            lines = [l for l in res.stdout.splitlines() if comment in l]
            if not lines:
                break
            _iptables(table, "-D", chain, *_rule_args(lines[0]), check=False)


# ─────────────────────────── 沙箱/服务怎么进 netns ───────────────────────────


def exec_prefix(user: str | None) -> list[str]:
    """进 user 的 netns 跑命令的前缀（没启用/没 user 就空）。"""
    if not enabled() or not user:
        return []
    ns = netns_name(user)
    if not _netns_exists(ns):
        try:
            ensure_netns(user)
        except Exception as e:
            log.warning("准备 netns 失败（退回 host 网络）: %s", e)
            return []
    return ["ip", "netns", "exec", ns]


def netns_for_user(user: str | None) -> str | None:
    """user 的 netns 名（没有就建一个；没启用 → None）。

    注意：不能"没有就算了"—— 那样只跑脚本不建服务的 user 会**退回 host 网络**，
    隔离就没了。所以这里按需创建。
    """
    if not enabled() or not user:
        return None
    ns = netns_name(user)
    if _netns_exists(ns):
        return ns
    try:
        info = ensure_netns(user)
        return info.get("netns")
    except Exception as e:
        log.warning("准备 user %s 的 netns 失败（退回 host 网络）: %s", user, e)
        return None


def service_host(user: str | None) -> str:
    """服务在 netns 里的地址（宿主健康检查/平台访问用）。"""
    if not enabled() or not user:
        return "localhost"
    from state.db import get_user

    row = get_user(user) or {}
    idx = index_of(row.get("cidr"))
    if idx is None:
        return "localhost"
    return ip_for(idx)


# ─────────────────────────── 重建（daemon 重启） ───────────────────────────


def rebuild_all() -> dict:
    """按 DB 重建所有 netns 与防火墙规则（daemon 启动时调）。"""
    if not enabled():
        return {"enabled": False}
    if not available():
        log.warning("这台机器不支持 netns（缺 ip/iptables？）→ 退回 host 网络模式")
        return {"enabled": False, "reason": "unavailable"}
    from state.db import list_service_rows, list_users

    _enable_forwarding()
    _dedupe_rules()
    _ensure_global_rules()

    made, ports = 0, 0
    for row in list_users():
        user = row.get("name")
        if not user:
            continue
        try:
            ensure_netns(user)
            made += 1
        except Exception as e:
            log.warning("重建 user %s 的 netns 失败: %s", user, e)
    for svc in list_service_rows():
        user = svc.get("owner")
        port = svc.get("port")
        if not user or not port:
            continue
        try:
            add_service_port(user, int(port))
            ports += 1
        except Exception as e:
            log.warning("重建服务 %s 的端口规则失败: %s", svc.get("name"), e)
    log.info("netns 重建完成：%d 个 user，%d 个服务端口", made, ports)
    return {"enabled": True, "users": made, "ports": ports}


def status() -> list[dict]:
    """当前 netns 状态（给 UI / 运维看）。"""
    from state.db import list_users

    out = []
    for row in list_users():
        ns = netns_name(row["name"])
        out.append(
            {
                "user": row["name"],
                "netns": ns,
                "exists": _netns_exists(ns) if enabled() else False,
                "cidr": row.get("cidr"),
                "ip": ip_for(index_of(row.get("cidr"))) if index_of(row.get("cidr")) else None,
            }
        )
    return out
