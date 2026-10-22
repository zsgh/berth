"""长驻服务生命周期（任务 2.3 / 3.1 / 3.2 + B5/B8/B9/B10/B11/B12）。

启动一个服务 = 分配端口 → 准备 overlay → `systemd-run --unit=berth-{id}` 拉起
（systemd 托管，重启/日志交给 systemd）→ 健康检查 → 执行 init → 记录 SQLite。

沙箱构造与 run_script 完全一致（overlay + pivot_root + setpriv + host 网络），
区别只是 systemd 单元是长驻 service 而不是用完即停的 scope。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import yaml

from sandbox.images import resolve_image
from sandbox.mounts import normalize_mounts
from sandbox.network import allocate_port, normalize_policy, release_port
from sandbox.overlayfs import SETPRIV, exec_command, sandbox_shell, systemd_run_cmd
from sandbox.paths import BASE_DIR, SANDBOX_GID, SANDBOX_UID, ensure_base_dir, priv_cmd, sudo
from sandbox.systemd_units import kill_unit, list_managed_units, stop_unit, unit_active
from sandbox.tracing import span
from state.db import (
    delete_service_row,
    get_service_row,
    init_db,
    list_port_rows,
    list_service_rows,
    match_labels,
    record_service,
    update_service,
    update_service_status,
)

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(
    os.environ.get("BERTH_TEMPLATES_DIR", str(Path(__file__).resolve().parent.parent / "templates"))
)

ACTIVE_STATUSES = ("starting", "running")


# ─────────────────────────── 模板 ───────────────────────────

# berth 模板的合法字段（语义与 K8s 的 command/args 一致）
ALLOWED_FIELDS = {
    "name",
    "image",
    "command",
    "args",
    "env",
    "ports",
    "volumes",
    "healthcheck",
    "resources",
    "network",   # B2：网络策略
    "init",      # B5：初始化怎么做
    "mounts",    # B11：只读挂载（被测代码目录等）
}

# 常见「从 docker 抄过来」的字段 → 迁移提示
FIELD_HINTS = {
    "image_rootfs": "改用 image: <名字>（镜像名表：images.yaml 或自动发现 /opt/berth/rootfs-*；base=基础镜像）",
    "entrypoint": "用 command（berth 与 K8s 语义一致：command=可执行程序、args=参数；"
                  "compose 的 entrypoint 对应 command，compose 的 command 对应 args）",
    "environment": "用 env",
    "container_name": "用 name",
    "expose": "用 ports（[{internal: ..., external_range: [...]}]）",
    "build": "不支持 build（镜像 = 预构建的 rootfs 目录）",
    "mem_limit": "用 resources.memory_mb",
    "cpus": "用 resources.cpu_percent（100 = 1 核）",
    "deploy": "用 resources（memory_mb / cpu_percent）",
    "restart": "不支持 restart（服务由 systemd 托管，daemon 重启时会自动恢复状态）",
    "networks": "不支持 networks（host 网络模式；出网限制用 network: {mode: none|allow}）",
    "network_mode": "不支持 network_mode（host 网络模式）",
    "depends_on": "不支持 depends_on（单服务模板）",
}

# 各子结构的合法字段
SECTION_FIELDS = {
    "ports": {"internal", "external_range"},
    "volumes": {"internal", "persist"},
    "healthcheck": {"type", "port", "timeout", "interval", "path", "expect_status", "headers"},
    "resources": {"memory_mb", "cpu_percent"},
    "network": {"mode", "allow"},
    "init": {"image", "command", "args", "env", "timeout"},
    "mounts": {"source", "target", "read_only"},
}

_PORTS_HINT = (
    "berth 的宿主端口是动态分配的：请写成 "
    "[{internal: 6379, external_range: [16379, 16479]}]，实际端口在启动返回值里"
)


def _check_fields(tpl_name: str, section: str, value: dict) -> None:
    allowed = SECTION_FIELDS[section]
    bad = sorted(set(value.keys()) - allowed)
    if bad:
        raise ValueError(
            f"模板 {tpl_name} 的 {section} 不支持字段 {bad}（可用: {sorted(allowed)}）"
        )


def validate_template(tpl_name: str, tpl: dict) -> dict:
    """校验一份模板（文件里的或内联 spec）。"""
    if not isinstance(tpl, dict):
        raise ValueError(f"模板 {tpl_name} 必须是 YAML 映射（key: value）")

    # 1) 未知字段：报错 + 迁移提示
    unknown = sorted(set(tpl.keys()) - ALLOWED_FIELDS)
    if unknown:
        msgs = []
        for key in unknown:
            hint = FIELD_HINTS.get(str(key))
            if hint:
                msgs.append(f"{key!r} → {hint}")
            else:
                msgs.append(f"{key!r}（未知字段）")
        raise ValueError(
            f"模板 {tpl_name} 包含不支持的字段：" + "；".join(msgs)
            + f"。可用字段: {sorted(ALLOWED_FIELDS)}"
        )

    # 2) 必填
    for key in ("name", "image", "command", "ports"):
        if not tpl.get(key):
            raise ValueError(f"模板 {tpl_name} 缺少字段: {key}")

    # 3) 类型与子结构
    if not isinstance(tpl["command"], list):
        raise ValueError(
            f"模板 {tpl_name} 的 command 必须是数组（argv），如 [\"/usr/bin/redis-server\"]"
        )
    if tpl.get("args") is not None and not isinstance(tpl["args"], list):
        raise ValueError(f"模板 {tpl_name} 的 args 必须是数组，如 [\"--port\", \"{'{port}'}\"]")
    if tpl.get("env") is not None and not isinstance(tpl["env"], dict):
        raise ValueError(f"模板 {tpl_name} 的 env 必须是映射（KEY: value）")

    ports = tpl["ports"]
    if not isinstance(ports, list):
        raise ValueError(f"模板 {tpl_name} 的 ports 必须是数组")
    for i, p in enumerate(ports):
        if isinstance(p, str):
            raise ValueError(f"模板 {tpl_name} 的 ports[{i}] 是字符串 {p!r}：{_PORTS_HINT}")
        if not isinstance(p, dict) or "internal" not in p:
            raise ValueError(f"模板 {tpl_name} 的 ports[{i}] 需要 internal（+ 可选 external_range）")
        _check_fields(tpl_name, "ports", p)

    for vol in tpl.get("volumes") or []:
        if not isinstance(vol, dict) or "internal" not in vol:
            raise ValueError(f"模板 {tpl_name} 的每个 volume 需要 internal（+ 可选 persist）")
        _check_fields(tpl_name, "volumes", vol)

    if tpl.get("healthcheck") is not None:
        if not isinstance(tpl["healthcheck"], dict):
            raise ValueError(f"模板 {tpl_name} 的 healthcheck 必须是映射")
        _check_fields(tpl_name, "healthcheck", tpl["healthcheck"])
        hc_type = tpl["healthcheck"].get("type", "tcp")
        if hc_type not in ("tcp", "http"):
            raise ValueError(
                f"模板 {tpl_name} 的 healthcheck.type 只能是 tcp / http（收到 {hc_type!r}）"
            )
        if hc_type == "http" and not tpl["healthcheck"].get("path"):
            raise ValueError(f"模板 {tpl_name} 的 http 健康检查需要 path（如 /healthz）")

    if tpl.get("resources") is not None:
        if not isinstance(tpl["resources"], dict):
            raise ValueError(f"模板 {tpl_name} 的 resources 必须是映射")
        _check_fields(tpl_name, "resources", tpl["resources"])

    if tpl.get("network") is not None:
        if not isinstance(tpl["network"], dict):
            raise ValueError(f"模板 {tpl_name} 的 network 必须是映射（mode/allow）")
        _check_fields(tpl_name, "network", tpl["network"])
        normalize_policy(tpl["network"])  # 顺带校验 mode

    if tpl.get("init") is not None:
        if not isinstance(tpl["init"], dict):
            raise ValueError(f"模板 {tpl_name} 的 init 必须是映射")
        _check_fields(tpl_name, "init", tpl["init"])
        init = tpl["init"]
        if not init.get("command"):
            raise ValueError(f"模板 {tpl_name} 的 init 需要 command（怎么执行初始化）")

    for m in tpl.get("mounts") or []:
        if not isinstance(m, dict) or "source" not in m or "target" not in m:
            raise ValueError(
                f"模板 {tpl_name} 的每个 mount 需要 source 和 target（+ 可选 read_only）"
            )
        _check_fields(tpl_name, "mounts", m)

    return tpl


def load_template(name: str) -> dict:
    """读取并校验服务模板。

    语义上等同「docker run 的参数模板」，字段与 K8s 风格一致：
    command = 可执行程序，args = 参数，最终 argv = command + args。
    未知/写错的字段直接报错，并尽量给出 docker/compose 习惯的迁移提示。
    """
    path = TEMPLATES_DIR / f"{name}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in TEMPLATES_DIR.glob("*.yaml"))
        raise ValueError(f"模板不存在: {name}（可用: {available}）")
    tpl = yaml.safe_load(path.read_text(encoding="utf-8"))
    return validate_template(name, tpl)


def list_templates() -> list[str]:
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.yaml"))


# ─────────────────────────── 覆盖 / 合并（B5） ───────────────────────────


def render(text: str, ctx: dict) -> str:
    """安全占位符替换：只替换已知 key，未知的大括号原样保留。

    比 str.format 稳（模板里的 `${VAR}` / JSON 花括号不会炸）。
    """
    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key in ctx:
            return str(ctx[key])
        return match.group(0)

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, str(text))


def apply_overrides(tpl: dict, overrides: dict | None) -> dict:
    """把请求里的 overrides 合并进模板（env/resources 合并，其余覆盖）。"""
    if not overrides:
        return dict(tpl)
    out = dict(tpl)

    env = dict(out.get("env") or {})
    env.update({str(k): str(v) for k, v in (overrides.get("env") or {}).items()})
    out["env"] = env

    if overrides.get("command") is not None:
        out["command"] = [str(x) for x in overrides["command"]]
    if overrides.get("args") is not None:
        out["args"] = [str(x) for x in overrides["args"]]
    if overrides.get("ports") is not None:
        out["ports"] = overrides["ports"]
    if overrides.get("healthcheck") is not None:
        hc = dict(out.get("healthcheck") or {})
        hc.update(overrides["healthcheck"])
        out["healthcheck"] = hc

    resources = dict(out.get("resources") or {})
    resources.update(overrides.get("resources") or {})
    if resources:
        out["resources"] = resources

    if overrides.get("network") is not None:
        net = dict(out.get("network") or {})
        net.update(overrides["network"])
        out["network"] = net

    if overrides.get("mounts") is not None:
        out["mounts"] = overrides["mounts"]

    return out


def resolve_spec(
    template: str | None = None,
    spec: dict | None = None,
    overrides: dict | None = None,
) -> tuple[str, dict]:
    """算出最终生效的模板（名字 + 模板内容）。"""
    if spec:
        tpl = validate_template("(inline)", dict(spec))
        name = str(spec.get("name") or "custom")
    elif template:
        tpl = load_template(template)
        name = template
    else:
        raise ValueError("必须给 template（模板名）或 spec（内联模板）")
    tpl = apply_overrides(tpl, overrides)
    return name, tpl


# ─────────────────────────── 健康状况 ───────────────────────────


def _wait_tcp(host: str, port: int, timeout: float, interval: float = 0.3, unit: str | None = None) -> bool:
    """等 TCP 就绪。可选传 unit：unit 挂了（起不来）就提前失败，不干等超时。"""
    deadline = time.time() + timeout
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        if unit and not unit_active(unit):
            return False  # 进程都退出了，再等也没用
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def _http_probe(url: str, timeout: float, expect: set[int], headers: dict | None = None) -> bool:
    """发一个 HTTP 请求，状态码命中期望就算就绪。"""
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status in expect
    except urllib.error.HTTPError as e:
        return e.code in expect
    except Exception:
        return False


def _wait_http(
    host: str,
    port: int,
    path: str,
    expect: set[int],
    timeout: float,
    interval: float = 0.3,
    unit: str | None = None,
    headers: dict | None = None,
) -> bool:
    """等 HTTP 健康检查通过（tcp 通了不代表应用就绪）。"""
    url = f"http://{host}:{port}{path if path.startswith('/') else '/' + path}"
    deadline = time.time() + timeout
    while True:
        if _http_probe(url, min(5.0, max(1.0, timeout)), expect, headers):
            return True
        if unit and not unit_active(unit):
            return False
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def check_health(tpl: dict, host: str, port: int, timeout: float | None = None, unit: str | None = None) -> bool:
    """健康检查：tcp（默认）或 http（B10）。"""
    hc = tpl.get("healthcheck") or {}
    hc_type = hc.get("type", "tcp")
    if hc_type == "tcp":
        return _wait_tcp(host, port, timeout or hc.get("timeout", 20), hc.get("interval", 0.3), unit=unit)
    if hc_type == "http":
        expect_raw = hc.get("expect_status", 200)
        expect = {int(expect_raw)} if not isinstance(expect_raw, (list, tuple, set)) else {
            int(x) for x in expect_raw
        }
        return _wait_http(
            host,
            port,
            hc.get("path", "/"),
            expect,
            timeout or hc.get("timeout", 20),
            hc.get("interval", 0.3),
            unit=unit,
            headers=hc.get("headers"),
        )
    raise ValueError(f"不支持的健康检查类型: {hc_type}")


# ─────────────────────────── 启动 ───────────────────────────


def _start_unit(unit: str, inner: str, tpl: dict, network: dict | None = None) -> None:
    resources = tpl.get("resources") or {}
    cmd = systemd_run_cmd(
        inner,
        unit=unit,
        scope=False,
        memory_mb=int(resources.get("memory_mb", 512)),
        cpu_percent=int(resources.get("cpu_percent", 100)),
        tasks_max=128,
        network=network,
    )
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"systemd-run 启动失败: {res.stderr.strip() or res.stdout.strip()}")


def _rm_rf(path: Path) -> None:
    """删目录树：先普通删（权限够时），不行再走 sudo。"""
    import shutil

    try:
        shutil.rmtree(path)
        return
    except (OSError, PermissionError):
        pass
    sudo("rm", "-rf", path, check=False)


class ServiceConflictError(ValueError):
    """同名服务冲突（名字已被不同模板的实例占用）。API 层会映射成 409。"""


def _latest_by_name(name: str) -> dict | None:
    """按名字找最近的实例（刷新过状态）。"""
    from state.db import find_service_by_name

    row = find_service_by_name(name)
    return _refresh_row(row) if row else None


def start_service(
    template: str,
    name: str | None = None,
    timeout: float | None = None,
    *,
    spec: dict | None = None,
    overrides: dict | None = None,
    init: dict | None = None,
    labels: dict | None = None,
    task_id: str | None = None,
    network: dict | None = None,
) -> dict:
    """启动服务。**同名幂等（ensure 语义）**，当传入 name 时：

    - 名字不存在              → 新建
    - 同名且已在运行          → 直接返回现有实例（reused=true），绝不重复创建
    - 同名但已停止/掉线       → 原样再起，复用数据卷（reused=true）
    - 同名但模板不同          → ServiceConflictError（409）

    不传 name → 每次新建匿名实例（名字自动取 service_id）。
    """
    name = (name or "").strip() or None
    tpl_name, tpl = resolve_spec(template=template, spec=spec, overrides=overrides)
    if network:
        tpl = apply_overrides(tpl, {"network": network})

    if name:
        existing = _latest_by_name(name)
        if existing:
            if existing["template"] != tpl_name:
                raise ServiceConflictError(
                    f"名字 {name!r} 已被 {existing['service_id']} 占用"
                    f"（template={existing['template']}，status={existing['status']}）；"
                    f"换个名字，或先 DELETE /services/{existing['service_id']}"
                )
            if existing["status"] in ACTIVE_STATUSES and unit_active(existing["unit"]):
                log.info("同名服务已在运行，直接返回: %s (%s)", existing["service_id"], name)
                # 已在运行：init 不重放（幂等语义）；标签可以更新
                if labels:
                    update_service(
                        existing["service_id"], labels=json.dumps(labels, ensure_ascii=False)
                    )
                    existing = dict(existing, labels=dict(labels))
                return dict(existing, reused=True, init_replayed=False)
            # 已停止 / 掉线：原样再起（保留数据卷）
            # 请求里带了新的 spec / overrides / init / labels → 先更新再起
            if spec or overrides or init or labels:
                _apply_pending_updates(existing, tpl_name, tpl, overrides, init, labels)
            log.info("同名服务已存在（%s），原样再起: %s", existing["status"], existing["service_id"])
            return dict(start_existing_service(existing["service_id"], timeout), reused=True)

    with span("service.start", template=tpl_name, name=name or "") as s:
        svc = _start_service_impl(
            tpl_name,
            name=name,
            timeout=timeout,
            spec=tpl,
            init=init,
            labels=labels,
            task_id=task_id,
            overrides=overrides,
        )
        s.set_attribute("service_id", svc["service_id"])
        s.set_attribute("port", svc["port"])
        s.set_attribute("status", svc["status"])
        return dict(svc, reused=False)


def _apply_pending_updates(
    existing: dict,
    tpl_name: str,
    tpl: dict,
    overrides: dict | None,
    init: dict | None,
    labels: dict | None,
) -> None:
    """服务已存在（已停止）时：把新的 spec / overrides / init / labels 写进记录。

    下次启动（`/start`）会用新的配置；`init` 也会重放。
    """
    fields: dict = {}
    if tpl:
        fields["spec"] = json.dumps(tpl, ensure_ascii=False)
        fields["template"] = tpl_name
    if init is not None:
        fields["init_spec"] = json.dumps(init, ensure_ascii=False)
    if labels:
        fields["labels"] = json.dumps(labels, ensure_ascii=False)
    if fields:
        update_service(existing["service_id"], **fields)


def _start_service_impl(
    template: str,
    name: str | None = None,
    timeout: float | None = None,
    service_id: str | None = None,
    svc_dir: Path | None = None,
    created_at: str | None = None,
    prefer_port: int | None = None,
    *,
    spec: dict | None = None,
    init: dict | None = None,
    labels: dict | None = None,
    task_id: str | None = None,
    overrides: dict | None = None,
    init_result: dict | None = None,
    replay_init: bool = True,
) -> dict:
    """service_id/svc_dir 传入时表示「复用已有服务」：保留数据卷，端口重新分配（优先原端口）。"""
    if spec is None:
        tpl = load_template(template)
    else:
        tpl = validate_template(template, dict(spec))
    init_db()
    ensure_base_dir()

    host = "localhost"
    service_id = service_id or "svc-" + uuid.uuid4().hex[:8]
    unit = f"berth-{service_id}"
    svc_dir = svc_dir or BASE_DIR / "services" / service_id
    run_dir = svc_dir / "run"
    upper, work, newroot = run_dir / "upper", run_dir / "work", run_dir / "newroot"

    # 1. 解析镜像名（缺镜像时给出明确指引，而不是晦涩的 mount 报错）
    image_path = resolve_image(tpl["image"])

    port_spec = tpl["ports"][0]
    internal = int(port_spec["internal"])
    external_range = tuple(port_spec["external_range"]) if port_spec.get("external_range") else None
    port = allocate_port(service_id, internal, external_range, prefer=prefer_port)

    try:
        # 2. 准备 overlay 目录
        for d in (upper, work, newroot):
            d.mkdir(parents=True, exist_ok=True)

        # 3. 数据卷（persist: true 的目录保留在 svc_dir/volumes 下）
        binds: dict[str, Path] = {}
        binds_ro: dict[str, Path] = {}
        persist = False
        for vol in tpl.get("volumes") or []:
            host_dir = svc_dir / "volumes" / str(vol["internal"]).lstrip("/")
            host_dir.mkdir(parents=True, exist_ok=True)
            binds[str(vol["internal"])] = host_dir
            persist = persist or bool(vol.get("persist"))

        # 3b. 只读挂载（B11：被测代码目录等）
        for m in normalize_mounts(tpl.get("mounts") or []):
            binds_ro[m["target"]] = Path(m["source"])

        # overlay/数据卷归沙箱 uid，服务进程（uid 1000）才能写
        sudo("chown", "-R", f"{SANDBOX_UID}:{SANDBOX_GID}", upper, *binds.values())

        # 4. 组装命令（{port} 等占位符替换）
        fmt = {
            "port": port,
            "service_id": service_id,
            "name": name or service_id,
        }
        cmdline = [str(c) for c in tpl["command"]] + [
            render(a, fmt) for a in (tpl.get("args") or [])
        ]
        env = {str(k): render(v, fmt) for k, v in (tpl.get("env") or {}).items()}

        inner = sandbox_shell(
            upper=upper,
            work=work,
            newroot=newroot,
            lower=str(image_path),
            binds=binds,
            binds_ro=binds_ro,
            env=env,
            # 同 run_script：bash 作为 pid ns init，捕获退出码（长驻进程退出码进 journal）
            entry="set +e\n" + f"{SETPRIV} {shlex.join(cmdline)}\n" + "rc=$?\nexit $rc",
        )

        network = normalize_policy(tpl.get("network"))

        # 5. systemd 托管启动
        try:
            _start_unit(unit, inner, tpl, network=network)
        except Exception as e:
            raise RuntimeError(f"服务启动失败: {e}") from e
        record_service(
            service_id=service_id,
            name=name or service_id,
            template=template,
            unit=unit,
            host=host,
            port=port,
            internal_port=internal,
            status="starting",
            dir=str(svc_dir),
            persist=1 if persist else 0,
            labels=json.dumps(labels or {}, ensure_ascii=False),
            overrides=json.dumps(overrides or {}, ensure_ascii=False),
            spec=json.dumps(tpl, ensure_ascii=False),
            init_spec=json.dumps(init or {}, ensure_ascii=False),
            init_result=json.dumps(init_result or {}, ensure_ascii=False),
            network=json.dumps(network, ensure_ascii=False),
            task_id=task_id,
            **(dict(created_at=created_at) if created_at else {}),
        )

        # 6. 等待健康检查（unit 挂了会提前失败，不干等）
        if not check_health(tpl, host, port, timeout, unit=unit):
            logs = subprocess.run(
                priv_cmd("journalctl", "-u", f"{unit}.service", "--no-pager", "-n", "20"),
                capture_output=True,
                text=True,
            )
            stop_unit(unit)
            sudo("rm", "-rf", svc_dir, check=False)
            release_port(service_id)
            update_service_status(service_id, "crashed")
            raise TimeoutError(
                f"健康检查失败（{host}:{port} 未就绪）。journal:\n{logs.stdout.strip()}"
            )

        update_service_status(service_id, "running")
        log.info("服务已启动: %s (%s @ %s:%s)", service_id, template, host, port)

        # 7. 初始化（B5）：健康检查通过后执行
        if init and replay_init:
            result = run_service_init(
                service_id=service_id,
                template=tpl,
                init=init,
                port=port,
                name=name or service_id,
                svc_dir=svc_dir,
                task_id=task_id,
            )
            update_service(service_id, init_result=json.dumps(result, ensure_ascii=False))
            if result.get("exit_code") not in (0, None):
                # 初始化失败：停掉服务（结果已经记进 init_result，详情里能查到）
                stop_unit(unit)
                update_service_status(service_id, "crashed")
                raise RuntimeError(
                    f"初始化失败（exit={result.get('exit_code')}）：\n"
                    f"{(result.get('stderr') or result.get('stdout') or '').strip()[:2000]}"
                )

        return get_service(service_id)

    except Exception:
        # 启动路径出错：回滚端口（unit / 目录在各自分支里已清理）
        release_port(service_id)
        raise


# ─────────────────────────── 初始化（B5） ───────────────────────────


def _service_workspace(svc_dir: Path, task_id: str | None) -> Path:
    """服务 workspace：指定了 task_id 就和该任务共享，否则用服务自己的目录。"""
    if task_id:
        from sandbox.files import task_workspace

        return task_workspace(task_id)
    return svc_dir / "workspace"


def _resolve_init_file(path: str, workspace: Path) -> tuple[str, str]:
    """在 workspace / 白名单目录里找 init 用的文件，返回 (内容, 类型)。"""
    from sandbox.files import safe_join
    from sandbox.mounts import resolve_source

    candidates = []
    try:
        candidates.append(safe_join(workspace, path))
    except ValueError:
        pass
    try:
        candidates.append(resolve_source(path))
    except ValueError:
        pass
    for candidate in candidates:
        if candidate.is_file():
            content = candidate.read_text(errors="replace")
            kind = "script" if candidate.suffix in (".sh", ".bash") else "sql"
            return content, kind
    raise ValueError(f"init.file 找不到: {path!r}（workspace={workspace}）")


def run_service_init(
    *,
    service_id: str,
    template: dict,
    init: dict,
    port: int,
    name: str,
    svc_dir: Path,
    task_id: str | None = None,
) -> dict:
    """执行服务的初始化内容（SQL / 脚本 / workspace 文件）。

    模板里的 `init` 段描述"怎么执行"（command/args/env/image），
    请求里的 `init` 描述"执行什么"（sql/script/file）。

    初始化内容会先落成 workspace 里的文件（`.berth-init.sql` / `.berth-init.sh`），
    模板的 init 命令用 `{init_file}` 引用它 —— 这样 SQL 里带引号 / 特殊字符也安全。
    """
    tpl_init = dict(template.get("init") or {})
    request_init = dict(init or {})
    if not request_init:
        return {}

    sql_text = request_init.get("sql") or ""
    script_text = request_init.get("script") or ""
    file_path = request_init.get("file")
    if file_path:
        content, kind = _resolve_init_file(str(file_path), _service_workspace(svc_dir, task_id))
        if kind == "script" and not script_text:
            script_text = content
        elif not sql_text:
            sql_text = content

    if not sql_text and not script_text:
        return {}

    workspace = _service_workspace(svc_dir, task_id)
    workspace.mkdir(parents=True, exist_ok=True)

    # 初始化内容落成文件（模板的 init 命令用 {init_file} 引用）
    if sql_text:
        payload_name = ".berth-init.sql"
        payload = sql_text
    else:
        payload_name = ".berth-init.sh"
        payload = script_text
    (workspace / payload_name).write_text(payload, encoding="utf-8")
    try:
        os.chmod(workspace / payload_name, 0o644)
    except OSError:
        pass
    init_file = f"/workspace/{payload_name}"

    env_ctx = {
        "port": port,
        "service_id": service_id,
        "name": name,
        "sql": sql_text,
        "script": script_text,
        "init_sql": sql_text,
        "init_script": script_text,
        "init_file": init_file,
    }
    # 模板 env 也进上下文（init 里可以引用 {POSTGRES_PASSWORD} 这种）
    for k, v in (template.get("env") or {}).items():
        env_ctx[str(k)] = render(v, {"port": port, "service_id": service_id, "name": name})

    command = list(tpl_init.get("command") or [])
    args = list(tpl_init.get("args") or [])
    if command:
        argv = [render(c, env_ctx) for c in command] + [render(a, env_ctx) for a in args]
    elif not sql_text:
        # 没有模板 init 段，但传的是脚本 → 用 sh -c 执行
        argv = ["/bin/sh", "-c", script_text]
    else:
        raise ValueError(
            "模板没有定义 init 段（不知道怎么执行 SQL 初始化）："
            "请在模板里加 init.command，或改成传 script（用 sh -c 执行）"
        )

    env = {str(k): render(v, env_ctx) for k, v in (tpl_init.get("env") or {}).items()}
    env.update({str(k): str(v) for k, v in (request_init.get("env") or {}).items()})
    # 服务自身的 env 也带过去（PGPASSWORD 之类）
    for k, v in (template.get("env") or {}).items():
        env.setdefault(str(k), render(v, {"port": port, "service_id": service_id, "name": name}))

    image = str(request_init.get("image") or tpl_init.get("image") or template.get("image") or "base")
    timeout = int(request_init.get("timeout") or tpl_init.get("timeout") or 60)

    result = exec_command(
        argv,
        image=image,
        binds={"/workspace": workspace},
        env=env,
        timeout=timeout,
        memory_mb=512,
        cpu_percent=100,
    )
    result["command"] = argv
    result["image"] = image
    result["init_file"] = init_file
    result["executed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return result


# ─────────────────────────── 查询 / 状态刷新 ───────────────────────────


def _refresh_row(row: dict) -> dict:
    """修正记录状态：之前标记 running，但 unit 已经不在 → crashed。"""
    if row["status"] in ACTIVE_STATUSES and not unit_active(row["unit"]):
        update_service_status(row["service_id"], "crashed")
        release_port(row["service_id"])
        row = dict(row, status="crashed")
    return row


# ─────────────────────────── 用量（内存/CPU/磁盘） ───────────────────────────


def _dir_size(path) -> int | None:
    """目录实际占用字节数（du -sb；普通权限读不到时用 sudo 兜底）。"""
    if not path:
        return 0
    path = str(path)
    res = subprocess.run(["du", "-sb", "--", path], capture_output=True, text=True)
    if res.returncode != 0:
        res = sudo("du", "-sb", "--", path, check=False)
    try:
        return int(res.stdout.split()[0])
    except (ValueError, IndexError):
        return None


def _systemd_usage(unit: str) -> dict:
    """从 systemd 取实时资源：内存当前值/上限、CPU 累计时间。"""
    out = {"memory_bytes": None, "memory_limit_bytes": None, "cpu_time_ms": None}
    if not unit:
        return out
    res = subprocess.run(
        ["systemctl", "show", "-p", "MemoryCurrent", "-p", "MemoryMax", "-p", "CPUUsageNSec", unit],
        capture_output=True,
        text=True,
    )
    for line in res.stdout.splitlines():
        k, _, v = line.partition("=")
        if not v.isdigit():
            continue  # [not set] / infinity 等
        n = int(v)
        if k == "MemoryCurrent":
            out["memory_bytes"] = None if n >= 2**63 else n  # u64max = 未限制
        elif k == "MemoryMax":
            out["memory_limit_bytes"] = None if n >= 2**63 else n
        elif k == "CPUUsageNSec":
            out["cpu_time_ms"] = round(n / 1e6, 1)
    return out


def _human_bytes(n: int | None) -> str | None:
    """13598720 → '13.0 MiB'；49537 → '48.4 KiB'。"""
    if n is None:
        return None
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if v < 1024 or unit == "TiB":
            return f"{int(v)} B" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return None


def _human_time_ms(ms: float | None) -> str | None:
    """13459 → '13.5s'；731.6 → '732ms'；3744000 → '1h 2m 24s'。"""
    if ms is None:
        return None
    seconds = ms / 1000.0
    if seconds < 1:
        return f"{int(round(ms))}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {seconds % 60:.0f}s"


def _with_usage(row: dict) -> dict:
    """给服务行附上用量：

    usage    —— 机器可读原始值（bytes / ms）
    usage-h  —— 人类可读视图（"13.0 MiB" / "1h 2m 24s"），-h = human
    """
    row = dict(row)
    raw = {
        "memory_bytes": None,
        "memory_limit_bytes": None,
        "cpu_time_ms": None,
        "disk_bytes": _dir_size(row.get("dir")),
    }
    if row.get("status") in ACTIVE_STATUSES:
        raw.update(_systemd_usage(row.get("unit") or ""))
    row["usage"] = raw
    row["usage-h"] = {
        "memory": _human_bytes(raw["memory_bytes"]),
        "memory_limit": _human_bytes(raw["memory_limit_bytes"]),
        "cpu_time": _human_time_ms(raw["cpu_time_ms"]),
        "disk": _human_bytes(raw["disk_bytes"]),
    }
    for key in ("labels", "network", "init_spec", "init_result", "spec", "overrides"):
        if isinstance(row.get(key), str):
            try:
                row[key] = json.loads(row[key]) if row[key] else ({} if key != "spec" else None)
            except (TypeError, json.JSONDecodeError):
                pass
    return row


# ─────────────────────────── 查询 ───────────────────────────


def resolve_ref(ref: str) -> dict | None:
    """ref 可以是 svc-id 或服务名；返回刷新过状态的记录（找不到返回 None）。"""
    ref = str(ref or "").strip()
    if not ref:
        return None
    if ref.startswith("svc-"):
        row = get_service_row(ref)
        if row:
            return _refresh_row(row)
    return _latest_by_name(ref)


def get_service(ref: str) -> dict | None:
    row = resolve_ref(ref)
    return _with_usage(row) if row else None


def list_services(
    include_inactive: bool = False,
    status: str | None = None,
    with_usage: bool = True,
    labels: dict | None = None,
) -> list[dict]:
    """服务列表。

    默认只返回活跃（starting/running）的服务，等价于 `docker ps`；
    include_inactive=True 时把历史记录（stopped/crashed）也带上，等价于 `docker ps -a`。
    with_usage=True 时附带内存/CPU/磁盘用量（等价 docker stats 的静态快照）。
    labels 非空时按标签过滤（B12）。
    """
    # 先全量刷新状态（DB 里写 running 但 unit 已经没了的会被修正为 crashed）
    rows = [_refresh_row(r) for r in list_service_rows()]
    if status:
        rows = [r for r in rows if r["status"] == status]
    elif not include_inactive:
        rows = [r for r in rows if r["status"] in ACTIVE_STATUSES]
    if labels:
        rows = [r for r in rows if match_labels(r.get("labels") or {}, labels)]
    if with_usage:
        rows = [_with_usage(r) for r in rows]
    return rows


def list_volumes() -> list[dict]:
    """所有数据卷一览：归属服务、占用字节数、是否孤儿（记录已删但卷还在）。"""
    root = BASE_DIR / "services"
    if not root.is_dir():
        return []
    rows = {r["service_id"]: r for r in list_service_rows()}
    out = []
    for svc_dir in sorted(root.iterdir()):
        vol_dir = svc_dir / "volumes"
        if not vol_dir.is_dir():
            continue
        row = rows.get(svc_dir.name)
        out.append(
            {
                "service_id": svc_dir.name,
                "name": row["name"] if row else None,
                "template": row["template"] if row else None,
                "status": row["status"] if row else None,
                "path": str(vol_dir),
                "bytes": _dir_size(vol_dir),
                "orphan": row is None,
            }
        )
    return out


def delete_volume(service_id: str) -> dict:
    """清理**孤儿**数据卷（记录已删但卷还在磁盘上）。

    非孤儿卷（服务还在）→ 抛 ValueError（API 层映射成 409）。
    """
    service_id = str(service_id or "").strip()
    if not service_id:
        raise ValueError("缺少 service_id")
    if get_service_row(service_id):
        raise ValueError(
            f"{service_id} 不是孤儿卷（服务记录还在）：先 DELETE /services/{service_id}"
        )
    vol_dir = BASE_DIR / "services" / service_id / "volumes"
    if not vol_dir.exists():
        raise ValueError(f"没有这个卷: {service_id}")
    _rm_rf(vol_dir)
    return {"service_id": service_id, "deleted": True, "path": str(vol_dir)}


# ─────────────────────────── 停止 / 删除 / 重置 ───────────────────────────


def start_existing_service(ref: str, timeout: float | None = None) -> dict:
    """把已停止的服务原样再起：复用原目录和数据卷（persist 的数据还在），端口优先复用。

    ref 可以是 svc-id 或服务名。
    """
    row = resolve_ref(ref)
    if not row:
        raise ValueError(f"没有这个服务: {ref}")
    if row["status"] in ACTIVE_STATUSES and unit_active(row["unit"]):
        raise ValueError(f"服务已在运行: {row['service_id']}（要重启先 stop）")

    with span("service.start", template=row["template"], name=row.get("name") or "", reuse=1) as s:
        svc = _start_service_impl(
            row["template"],
            name=row.get("name"),
            timeout=timeout,
            service_id=row["service_id"],
            svc_dir=Path(row["dir"]),
            created_at=row.get("created_at"),
            prefer_port=row.get("port"),
            spec=_load_stored_spec(row),
            init=_load_stored_init(row),
            labels=row.get("labels") or {},
            task_id=row.get("task_id"),
        )
        s.set_attribute("service_id", svc["service_id"])
        s.set_attribute("port", svc["port"])
        s.set_attribute("status", svc["status"])
        return svc


def _load_stored_spec(row: dict) -> dict | None:
    """服务启动时存下的生效模板（内联 spec 也能重启）。"""
    spec = row.get("spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except (TypeError, json.JSONDecodeError):
            return None
    return spec or None


def _load_stored_init(row: dict) -> dict | None:
    """服务启动时存下的 init 请求（重放用）。"""
    init = row.get("init_spec")
    if isinstance(init, str):
        try:
            init = json.loads(init)
        except (TypeError, json.JSONDecodeError):
            return None
    return init or None


def stop_service(ref: str) -> dict:
    """停止服务（保留记录与数据卷）：systemd unit → overlay → 端口（带 trace 埋点）。"""
    row = resolve_ref(ref)
    if not row:
        raise ValueError(f"没有这个服务: {ref}")
    with span("service.stop", service_id=row["service_id"]) as s:
        svc = _stop_service_impl(row["service_id"])
        s.set_attribute("status", svc["status"])
        s.set_attribute("template", svc.get("template", ""))
        return svc


def delete_service(ref: str, volumes: bool = False) -> dict:
    """停止并删除服务记录；volumes=True 时连数据卷一起删（docker rm -v 语义）。"""
    row = resolve_ref(ref)
    if not row:
        raise ValueError(f"没有这个服务: {ref}")
    with span("service.stop", service_id=row["service_id"], deleted=True) as s:
        _stop_service_impl(row["service_id"], purge=True)
        if volumes:
            _rm_rf(Path(row["dir"]) / "volumes")
        s.set_attribute("volumes_deleted", volumes)
    log.info("服务已删除: %s%s", row["service_id"], "（含数据卷）" if volumes else "")
    return dict(row, status="stopped", deleted=True, volumes_deleted=bool(volumes))


def reset_service(ref: str, keep_backup: bool = False) -> dict:
    """重置服务：停止 → 清数据卷 → 重新初始化（重放 init）→ 启动。

    keep_backup=True 时把旧数据卷挪到 `<dir>/volumes.backup.<ts>`（不删）。
    """
    row = resolve_ref(ref)
    if not row:
        raise ValueError(f"没有这个服务: {ref}")
    service_id = row["service_id"]
    svc_dir = Path(row["dir"])
    started = time.time()

    # 1. 停止
    _stop_service_impl(service_id)

    # 2. 清数据卷（先挪走再删：重置瞬间完成，删在后面慢慢来）
    vol_dir = svc_dir / "volumes"
    backup = None
    if vol_dir.exists():
        if keep_backup:
            backup = svc_dir / f"volumes.backup.{int(started)}"
            os.rename(vol_dir, backup)
            vol_dir.mkdir(parents=True, exist_ok=True)
        else:
            _rm_rf(vol_dir)
            vol_dir.mkdir(parents=True, exist_ok=True)
    else:
        vol_dir.mkdir(parents=True, exist_ok=True)

    # 3. 再起（会重放 init）
    svc = start_existing_service(service_id)
    duration = round(time.time() - started, 3)
    log.info("服务已重置: %s（耗时 %.3fs）", service_id, duration)
    return {
        "service_id": service_id,
        "name": row.get("name"),
        "status": svc.get("status"),
        "port": svc.get("port"),
        "reset_duration_ms": int(duration * 1000),
        "init": _load_stored_init(row),
        "init_result": svc.get("init_result"),
        "backup": str(backup) if backup else None,
    }


def _stop_service_impl(service_id: str, purge: bool = False) -> dict:
    row = get_service_row(service_id)
    if not row:
        raise ValueError(f"没有这个服务: {service_id}")

    stop_unit(row["unit"])
    # overlay 工作目录（每次启动重新生成）
    run_dir = Path(row["dir"]) / "run"
    sudo("rm", "-rf", run_dir, check=False)

    # persist: false → 数据卷也删掉
    if not row["persist"]:
        _rm_rf(Path(row["dir"]) / "volumes")

    release_port(service_id)
    update_service_status(service_id, "stopped")
    log.info("服务已停止: %s%s", service_id, "（记录已清除）" if purge else "")
    if purge:
        delete_service_row(service_id)
        return dict(row, status="stopped", purged=True)
    fresh = get_service_row(service_id)
    return _with_usage(fresh) if fresh else dict(row, status="stopped")


# ─────────────────────────── 日志（B9） ───────────────────────────


def service_logs(ref: str, tail: int = 100, since: str | None = None) -> dict:
    """服务日志（journalctl -u berth-svc-{id}）。"""
    row = resolve_ref(ref)
    if not row:
        raise ValueError(f"没有这个服务: {ref}")
    unit = row["unit"]
    cmd = priv_cmd("journalctl", "-u", unit, "--no-pager", "-o", "short-iso")
    if tail and tail > 0:
        cmd += ["-n", str(int(tail))]
    if since:
        cmd += ["--since", str(since)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    text = res.stdout or ""
    return {
        "service_id": row["service_id"],
        "name": row.get("name"),
        "unit": unit,
        "text": text,
        "lines": [line for line in text.splitlines() if line.strip()],
    }


def service_logs_stream(ref: str, tail: int = 10):
    """流式读日志（SSE 用）：yield 一行行文本。"""
    row = resolve_ref(ref)
    if not row:
        raise ValueError(f"没有这个服务: {ref}")
    cmd = priv_cmd("journalctl", "-u", row["unit"], "--no-pager", "-o", "short-iso", "-f")
    if tail and tail > 0:
        cmd += ["-n", str(int(tail))]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")
    finally:
        try:
            proc.terminate()
        except OSError:
            pass


# ─────────────────────────── 恢复 / 孤儿清理（阶段三） ───────────────────────────


def recover_services() -> None:
    """daemon 重启后：核对 SQLite 记录与 systemd 实际状态。"""
    init_db()
    for row in list_service_rows():
        if row["status"] in ACTIVE_STATUSES:
            if unit_active(row["unit"]):
                update_service_status(row["service_id"], "running")
                log.info("恢复服务: %s（unit 仍在运行）", row["service_id"])
            else:
                update_service_status(row["service_id"], "crashed")
                release_port(row["service_id"])
                log.warning("服务 %s 的 unit 已不在，标记 crashed", row["service_id"])


def cleanup_orphans() -> list[str]:
    """清理 daemon 崩了以后遗留的资源：

    1. 宿主上存在但 SQLite 里没有记录的 workload unit（berth-svc-* / berth-run-*）
       → 停掉删掉。**只认这两个前缀**，不碰 daemon 自己的 unit（见 systemd_units.py）
    2. SQLite 里 state 非活跃、但端口还占着的记录 → 释放
    """
    init_db()
    # systemd 列出的单元名带后缀（xxx.service / xxx.scope），DB 里存的是裸名
    known_units = {f"{r['unit']}.service" for r in list_service_rows()}
    cleaned = []
    for unit in list_managed_units():
        if unit not in known_units:
            log.warning("清理孤儿 unit: %s", unit)
            kill_unit(unit)
            stop_unit(unit, graceful_timeout=5)
            cleaned.append(unit)

    active_ids = {r["service_id"] for r in list_service_rows() if r["status"] in ACTIVE_STATUSES}
    for p in list_port_rows():
        if p["service_id"] not in active_ids:
            log.warning("释放没有活跃服务的端口: %s:%s", p["service_id"], p["external_port"])
            release_port(p["service_id"])
    return cleaned
