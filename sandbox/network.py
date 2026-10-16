"""端口分配（任务 2.2）。

网络是 host 模式：沙箱/服务直接用宿主网络栈，所以不需要 veth / netns，
唯一要协调的是"谁用哪个宿主端口"。策略：

- 每个逻辑端口（如 redis 6379）映射到一段宿主区间（如 16379-16479）
- 分配记录写进 SQLite，服务停止时释放；同时探测宿主端口是否被别的东西占了
- 区间默认表可被模板里的 external_range 覆盖
"""
from __future__ import annotations

import socket
from datetime import datetime, timezone

from state.db import connect, init_db, list_port_rows

# internal 端口 → 默认宿主区间 [start, end)
PORT_RANGES: dict[int, tuple[int, int]] = {
    6379: (16379, 16479),    # redis
    3306: (13306, 13406),    # mysql
    5432: (15432, 15532),    # postgres
    27017: (27017, 27117),   # mongodb
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    init_db()
    conn = connect()
    with conn:
        conn.execute("DELETE FROM ports WHERE service_id=?", (service_id,))
    conn.close()


def get_port(service_id: str, internal: int | None = None) -> int | None:
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
    init_db()
    return list_port_rows()
