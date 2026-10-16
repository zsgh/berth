"""长驻服务生命周期（任务 2.3 / 3.1 / 3.2）。

启动一个服务 = 分配端口 → 准备 overlay → `systemd-run --unit=berth-{id}` 拉起
（systemd 托管，重启/日志交给 systemd）→ 健康检查 → 记录 SQLite。

沙箱构造与 run_script 完全一致（overlay + pivot_root + setpriv + host 网络），
区别只是 systemd 单元是长驻 service 而不是用完即停的 scope。
"""
from __future__ import annotations

import logging
import os
import shlex
import socket
import subprocess
import time
import uuid
from pathlib import Path

import yaml

from sandbox.network import allocate_port, release_port
from sandbox.overlayfs import SETPRIV, sandbox_shell, systemd_run_cmd
from sandbox.paths import BASE_DIR, SANDBOX_GID, SANDBOX_UID, ensure_base_dir, sudo
from sandbox.systemd_units import kill_unit, list_sbx_units, stop_unit, unit_active
from sandbox.tracing import span
from state.db import (
    delete_service_row,
    get_service_row,
    init_db,
    list_port_rows,
    list_service_rows,
    record_service,
    update_service_status,
)

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(
    os.environ.get("BERTH_TEMPLATES_DIR", str(Path(__file__).resolve().parent.parent / "templates"))
)

ACTIVE_STATUSES = ("starting", "running")


# ─────────────────────────── 模板 ───────────────────────────


def load_template(name: str) -> dict:
    path = TEMPLATES_DIR / f"{name}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in TEMPLATES_DIR.glob("*.yaml"))
        raise ValueError(f"模板不存在: {name}（可用: {available}）")
    tpl = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in ("name", "image_rootfs", "command", "ports"):
        if not tpl.get(key):
            raise ValueError(f"模板 {name} 缺少字段: {key}")
    return tpl


def list_templates() -> list[str]:
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.yaml"))


# ─────────────────────────── 健康状况 ───────────────────────────


def _wait_tcp(host: str, port: int, timeout: float, interval: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def check_health(tpl: dict, host: str, port: int, timeout: float | None = None) -> bool:
    hc = tpl.get("healthcheck") or {}
    hc_type = hc.get("type", "tcp")
    if hc_type != "tcp":
        raise ValueError(f"不支持的健康检查类型: {hc_type}")
    return _wait_tcp(host, port, timeout or hc.get("timeout", 20), hc.get("interval", 0.3))


# ─────────────────────────── 启动 ───────────────────────────


def _start_unit(unit: str, inner: str, tpl: dict) -> None:
    resources = tpl.get("resources") or {}
    cmd = systemd_run_cmd(
        inner,
        unit=unit,
        scope=False,
        memory_mb=int(resources.get("memory_mb", 512)),
        cpu_percent=int(resources.get("cpu_percent", 100)),
        tasks_max=128,
    )
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"systemd-run 启动失败: {res.stderr.strip() or res.stdout.strip()}")


def start_service(template: str, name: str | None = None, timeout: float | None = None) -> dict:
    """按模板启动一个长驻服务（带 Phoenix trace 埋点）。

    返回: {service_id, name, template, host, port, internal_port, status, unit, created_at}
    """
    with span("service.start", template=template, name=name or "") as s:
        svc = _start_service_impl(template, name=name, timeout=timeout)
        s.set_attribute("service_id", svc["service_id"])
        s.set_attribute("port", svc["port"])
        s.set_attribute("status", svc["status"])
        return svc


def _start_service_impl(template: str, name: str | None = None, timeout: float | None = None) -> dict:
    tpl = load_template(template)
    init_db()
    ensure_base_dir()

    host = "localhost"
    service_id = "svc-" + uuid.uuid4().hex[:8]
    unit = f"berth-{service_id}"
    svc_dir = BASE_DIR / "services" / service_id
    run_dir = svc_dir / "run"
    upper, work, newroot = run_dir / "upper", run_dir / "work", run_dir / "newroot"

    # 1. 分配端口
    port_spec = tpl["ports"][0]
    internal = int(port_spec["internal"])
    external_range = tuple(port_spec["external_range"]) if port_spec.get("external_range") else None
    port = allocate_port(service_id, internal, external_range)

    try:
        # 2. 准备 overlay 目录
        for d in (upper, work, newroot):
            d.mkdir(parents=True, exist_ok=True)

        # 3. 数据卷（persist: true 的目录保留在 svc_dir/volumes 下）
        binds: dict[str, Path] = {}
        persist = False
        for vol in tpl.get("volumes") or []:
            host_dir = svc_dir / "volumes" / str(vol["internal"]).lstrip("/")
            host_dir.mkdir(parents=True, exist_ok=True)
            binds[str(vol["internal"])] = host_dir
            persist = persist or bool(vol.get("persist"))

        # overlay/数据卷归沙箱 uid，服务进程（uid 1000）才能写
        sudo("chown", "-R", f"{SANDBOX_UID}:{SANDBOX_GID}", upper, *binds.values())

        # 4. 组装命令（{port} 等占位符替换）
        fmt = {"port": port, "service_id": service_id, "name": name or service_id}
        cmdline = [str(c) for c in tpl["command"]] + [
            str(a).format(**fmt) for a in (tpl.get("args") or [])
        ]
        env = {str(k): str(v).format(**fmt) for k, v in (tpl.get("env") or {}).items()}

        inner = sandbox_shell(
            upper=upper,
            work=work,
            newroot=newroot,
            lower=tpl["image_rootfs"],
            binds=binds,
            env=env,
            # 同 run_script：bash 作为 pid ns init，捕获退出码（长驻进程退出码进 journal）
            entry="set +e\n" + f"{SETPRIV} {shlex.join(cmdline)}\n" + "rc=$?\nexit $rc",
        )

        # 5. systemd 托管启动
        try:
            _start_unit(unit, inner, tpl)
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
        )

        # 6. 等待健康检查
        if not check_health(tpl, host, port, timeout):
            logs = subprocess.run(
                ["sudo", "-n", "journalctl", "-u", f"{unit}.service", "--no-pager", "-n", "20"],
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
        return get_service(service_id)

    except Exception:
        # 启动路径出错：回滚端口（unit / 目录在各自分支里已清理）
        release_port(service_id)
        raise


# ─────────────────────────── 查询 / 状态刷新 ───────────────────────────


def _refresh_row(row: dict) -> dict:
    """修正记录状态：之前标记 running，但 unit 已经不在 → crashed。"""
    if row["status"] in ACTIVE_STATUSES and not unit_active(row["unit"]):
        update_service_status(row["service_id"], "crashed")
        release_port(row["service_id"])
        row = dict(row, status="crashed")
    return row


def get_service(service_id: str) -> dict | None:
    row = get_service_row(service_id)
    if not row:
        return None
    return _refresh_row(row)


def list_services(include_inactive: bool = False, status: str | None = None) -> list[dict]:
    """服务列表。

    默认只返回活跃（starting/running）的服务，等价于 `docker ps`；
    include_inactive=True 时把历史记录（stopped/crashed）也带上，等价于 `docker ps -a`。
    """
    # 先全量刷新状态（DB 里写 running 但 unit 已经没了的会被修正为 crashed）
    rows = [_refresh_row(r) for r in list_service_rows()]
    if status:
        return [r for r in rows if r["status"] == status]
    if not include_inactive:
        return [r for r in rows if r["status"] in ACTIVE_STATUSES]
    return rows


# ─────────────────────────── 停止 ───────────────────────────


def stop_service(service_id: str, purge: bool = False) -> dict:
    """停止服务并清理：systemd unit → overlay → 端口（带 Phoenix trace 埋点）。

    purge=True 时连 SQLite 里的历史记录一并删除。
    """
    with span("service.stop", service_id=service_id) as s:
        svc = _stop_service_impl(service_id, purge=purge)
        s.set_attribute("status", svc["status"])
        s.set_attribute("template", svc.get("template", ""))
        return svc


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
        sudo("rm", "-rf", Path(row["dir"]) / "volumes", check=False)

    release_port(service_id)
    update_service_status(service_id, "stopped")
    log.info("服务已停止: %s%s", service_id, "（记录已清除）" if purge else "")
    if purge:
        delete_service_row(service_id)
        return dict(row, status="stopped", purged=True)
    return get_service(service_id) or dict(row, status="stopped")


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

    1. 宿主上存在但 SQLite 里没有记录的 berth-* unit → 停掉删掉
    2. SQLite 里 state 非活跃、但端口还占着的记录 → 释放
    """
    init_db()
    # systemd 列出的单元名带后缀（xxx.service / xxx.scope），DB 里存的是裸名
    known_units = {f"{r['unit']}.service" for r in list_service_rows()}
    cleaned = []
    for unit in list_sbx_units():
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
