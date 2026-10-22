"""端口分配 + 网络策略（任务 2.2 / B2）。

网络是 host 模式：沙箱/服务直接用宿主网络栈，所以不需要 veth / netns，
唯一要协调的是"谁用哪个宿主端口"。策略：

- 每个逻辑端口（如 redis 6379）映射到一段宿主区间（如 16379-16479）
- 分配记录写进 SQLite，服务停止时释放；同时探测宿主端口是否被别的东西占了
- 区间默认表可被模板里的 external_range 覆盖

**网络策略（B2）** 也放在这里：`host` / `none` / `allow` 三种，
靠 systemd 的 cgroup BPF 实现（`IPAddressDeny=any` + `IPAddressAllow=...`）。
"""
from __future__ import annotations

import ipaddress
import os
import socket
from datetime import datetime, timezone

# internal 端口 → 默认宿主区间 [start, end)
PORT_RANGES: dict[int, tuple[int, int]] = {
    6379: (16379, 16479),    # redis
    3306: (13306, 13406),    # mysql
    5432: (15432, 15532),    # postgres
    27017: (27017, 27117),   # mongodb
}

NETWORK_MODES = ("host", "none", "allow")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────── 网络策略（B2） ───────────────────────────


def normalize_policy(policy: dict | str | None) -> dict:
    """把各种写法的网络策略归一化成 {"mode": ..., "allow": [...]}。"""
    if policy is None:
        return {"mode": "host", "allow": []}
    if isinstance(policy, str):
        policy = {"mode": policy}
    mode = str(policy.get("mode") or "host").strip().lower()
    if mode not in NETWORK_MODES:
        raise ValueError(f"network.mode 只能是 {' / '.join(NETWORK_MODES)}（收到 {mode!r}）")
    allow = policy.get("allow") or []
    if isinstance(allow, str):
        allow = [allow]
    return {"mode": mode, "allow": [str(x) for x in allow]}


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def resolve_allow_entries(entries: list[str], timeout: float = 3.0) -> list[str]:
    """把白名单条目归一化成 CIDR 列表（域名先解析成 IP）。

    - `10.0.0.5`      → `10.0.0.5/32`
    - `10.0.0.0/8`    → 原样
    - `example.com`   → 解析成 `93.184.216.34/32`（多个 IP 就都放行）
    """
    out: list[str] = []
    for raw in entries or []:
        item = str(raw).strip()
        if not item:
            continue
        if _is_ip(item):
            addr = ipaddress.ip_address(item)
            out.append(f"{addr}/{addr.max_prefixlen}")
            continue
        if "/" in item:
            try:
                net = ipaddress.ip_network(item, strict=False)
            except ValueError as e:
                raise ValueError(f"无效的 CIDR: {item!r}（{e}）") from e
            out.append(str(net))
            continue
        # 当域名处理：解析成 IP
        try:
            infos = socket.getaddrinfo(item, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise ValueError(f"域名解析失败: {item!r}（{e}）") from e
        resolved = {info[4][0] for info in infos}
        if not resolved:
            raise ValueError(f"域名解析失败: {item!r}（没有 A/AAAA 记录）")
        for ip in sorted(resolved):
            addr = ipaddress.ip_address(ip)
            out.append(f"{addr}/{addr.max_prefixlen}")
    # 去重但保持顺序
    seen: set[str] = set()
    uniq = []
    for item in out:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


def systemd_network_properties(policy: dict | None) -> list[str]:
    """把网络策略翻译成 systemd-run 的 `-p` 属性。

    - host → 不加任何属性（保持现状）
    - none → `IPAddressDeny=any`（连 127.0.0.0/8 一起挡掉）
    - allow → `IPAddressDeny=any` + 每个白名单一条 `IPAddressAllow=`

    systemd 的规则是"更具体的条目优先"，所以 `Deny=any` + `Allow=10.0.0.0/8`
    的效果是：10.0.0.0/8 放行，其余（含 loopback）全挡。
    """
    policy = normalize_policy(policy)
    mode = policy["mode"]
    if mode == "host":
        return []
    props = ["IPAddressDeny=any"]
    if mode == "allow":
        for cidr in resolve_allow_entries(policy.get("allow") or []):
            props.append(f"IPAddressAllow={cidr}")
    return props


def network_capabilities() -> dict:
    """当前宿主是否支持 cgroup BPF（systemd 的 IPAddressDeny/Allow 依赖它）。"""
    cgroup2 = os.path.exists("/sys/fs/cgroup/cgroup.controllers")
    bpf = False
    try:
        with open("/proc/sys/kernel/unprivileged_bpf_disabled", "r") as f:
            # 不可用时 systemd 依然能用（daemon 以 root 跑）
            bpf = True
            _ = f.read()
    except OSError:
        bpf = True
    return {
        "modes": list(NETWORK_MODES),
        "mechanism": "systemd IPAddressDeny/IPAddressAllow (cgroup v2 + BPF)",
        "cgroup_v2": cgroup2,
        "bpf_available": bpf,
        "supported": cgroup2,
        "note": (
            "host 网络下 IP 过滤不区分端口；沙箱仍可访问同 IP 的其它端口，"
            "端口级隔离见 docs/design.md"
        ),
    }


def policy_allows_localhost(policy: dict | None) -> bool:
    """这个策略是否允许访问 loopback（UI/文档用）。"""
    policy = normalize_policy(policy)
    if policy["mode"] == "host":
        return True
    if policy["mode"] == "none":
        return False
    for item in policy.get("allow") or []:
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if net.version == 4 and net.subnet_of(ipaddress.ip_network("127.0.0.0/8")):
            return True
    return False


# ─────────────────────────── 端口分配 ───────────────────────────


def port_in_use(port: int) -> bool:
    """探测宿主端口是否已被占用（先试连，再试绑）。

    绑定探测带 SO_REUSEADDR：TIME_WAIT 状态的端口不算占用（服务自己监听时
    通常也带 SO_REUSEADDR，行为保持一致，避免重启后端口无谓地漂移）。
    """
    # 1) 已经有服务在 listen → connect 成功
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return True
    # 2) 被绑定但没 listen / 绑在别的地址 → bind 失败
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def allocate_port(
    service_id: str,
    internal: int,
    port_range: tuple[int, int] | None = None,
    prefer: int | None = None,
) -> int:
    """为 service_id 的 internal 端口分配一个宿主端口，记录到 SQLite。

    prefer: 优先尝试的端口（重启服务时传上次用的端口，尽量保持稳定）。
    """
    from state.db import connect, init_db

    init_db()
    if port_range is None:
        port_range = PORT_RANGES.get(internal)
    if port_range is None:
        raise ValueError(f"没有 {internal} 的默认端口区间，请在模板里声明 external_range")

    conn = connect()
    used = {r["external_port"] for r in conn.execute("SELECT external_port FROM ports").fetchall()}
    existing = conn.execute(
        "SELECT external_port FROM ports WHERE service_id=? AND internal_port=?",
        (service_id, internal),
    ).fetchone()
    if existing:  # 幂等：同一个 service 重复分配直接返回
        conn.close()
        return existing["external_port"]

    candidates = list(range(*port_range))
    if prefer is not None and prefer in candidates:
        candidates.remove(prefer)
        candidates.insert(0, prefer)
    for port in candidates:
        if port in used or port_in_use(port):
            continue
        with conn:
            conn.execute(
                "INSERT INTO ports (service_id, internal_port, external_port, allocated_at) "
                "VALUES (?, ?, ?, ?)",
                (service_id, internal, port, _now()),
            )
        conn.close()
        return port
    conn.close()
    raise RuntimeError(f"端口区间 {port_range} 已满，无法分配 {internal}")


def release_port(service_id: str) -> None:
    """释放 service_id 占用的所有端口。"""
    from state.db import connect, init_db

    init_db()
    conn = connect()
    with conn:
        conn.execute("DELETE FROM ports WHERE service_id=?", (service_id,))
    conn.close()


def get_port(service_id: str, internal: int | None = None) -> int | None:
    from state.db import connect

    conn = connect()
    if internal is None:
        row = conn.execute(
            "SELECT external_port FROM ports WHERE service_id=? LIMIT 1", (service_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT external_port FROM ports WHERE service_id=? AND internal_port=?",
            (service_id, internal),
        ).fetchone()
    conn.close()
    return row["external_port"] if row else None


def all_ports() -> list[dict]:
    from state.db import init_db, list_port_rows

    init_db()
    return list_port_rows()
