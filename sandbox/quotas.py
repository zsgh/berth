"""每 user 的配额（M4）。

多租户时，一个 user 不能把整台机器吃满。三个维度：

| 配额 | 含义 | 判定 |
|---|---|---|
| `quota_runs` | 同时在跑的任务数 | 数该 user 处于 running 的执行记录 |
| `quota_services` | 同时存在的服务数 | 数该 user 的服务记录 |
| `quota_memory_mb` | 同时占用的内存上限 | 加总该 user 在跑的任务 + 服务的 memory 限额 |

`0` = 不限。超了 → API 返回 429（配额）而不是 500。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class QuotaExceeded(Exception):
    """超出配额。"""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


def _user_row(user: str) -> dict:
    from state.db import get_user

    return get_user(user) or {}


def quota_of(user: str | None) -> dict:
    """user 的配额（没有 user → 不限）。"""
    if not user:
        return {"runs": 0, "memory_mb": 0, "services": 0}
    row = _user_row(user)
    return {
        "runs": int(row.get("quota_runs") or 0),
        "memory_mb": int(row.get("quota_memory_mb") or 0),
        "services": int(row.get("quota_services") or 0),
    }


def usage_of(user: str | None) -> dict:
    """user 当前占用的量。"""
    if not user:
        return {"runs": 0, "memory_mb": 0, "services": 0}
    from state.db import list_run_rows, list_service_rows

    runs = [r for r in list_run_rows() if (r.get("owner") or "") == user]
    active_runs = [r for r in runs if (r.get("status") or "") == "running"]
    services = [s for s in list_service_rows() if (s.get("owner") or "") == user]
    active_services = [
        s for s in services if (s.get("status") or "") in ("running", "starting")
    ]
    return {
        "runs": len(active_runs),
        "memory_mb": _memory_of(active_runs, active_services),
        "services": len(services),
    }


def _memory_of(runs: list[dict], services: list[dict]) -> int:
    """在跑的任务 + 服务的内存限额加总（MB）。"""
    total = 0
    for row in runs:
        total += int(row.get("memory_mb") or 0)
    for row in services:
        spec = row.get("spec")
        if isinstance(spec, str):
            import json

            try:
                spec = json.loads(spec)
            except (TypeError, ValueError):
                spec = {}
        res = (spec or {}).get("resources") or {}
        total += int(res.get("memory_mb") or 0)
    return total


def check_run(user: str | None, memory_mb: int = 0) -> None:
    """起一个任务前检查配额（超了抛 QuotaExceeded）。"""
    if not user:
        return
    q = quota_of(user)
    u = usage_of(user)
    if q["runs"] and u["runs"] >= q["runs"]:
        raise QuotaExceeded(
            f"并发任务数超配额（{u['runs']}/{q['runs']}）",
            {"kind": "runs", "used": u["runs"], "limit": q["runs"]},
        )
    if q["memory_mb"]:
        used = u["memory_mb"]
        if used + int(memory_mb or 0) > q["memory_mb"]:
            raise QuotaExceeded(
                f"内存配额超限（{used}+{memory_mb} > {q['memory_mb']} MB）",
                {
                    "kind": "memory_mb",
                    "used": used,
                    "requested": int(memory_mb or 0),
                    "limit": q["memory_mb"],
                },
            )


def check_service(user: str | None) -> None:
    """建一个服务前检查配额。"""
    if not user:
        return
    q = quota_of(user)
    u = usage_of(user)
    if q["services"] and u["services"] >= q["services"]:
        raise QuotaExceeded(
            f"服务数超配额（{u['services']}/{q['services']}）",
            {"kind": "services", "used": u["services"], "limit": q["services"]},
        )


def capacity() -> dict:
    """节点容量（给平台调度用）：CPU / 内存 / 磁盘 / 并发余量 / 镜像清单。"""
    import shutil

    from state.db import list_run_rows, list_service_rows
    from sandbox.paths import BASE_DIR

    out: dict = {"home": str(BASE_DIR)}
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                key, _, value = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    mem[key] = int(value.strip().split()[0]) * 1024
        out["memory"] = {
            "total_bytes": mem.get("MemTotal"),
            "available_bytes": mem.get("MemAvailable"),
        }
    except OSError:
        out["memory"] = {}
    try:
        usage = shutil.disk_usage(str(BASE_DIR))
        out["disk"] = {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent": round(100.0 * usage.used / usage.total, 1) if usage.total else None,
        }
    except OSError:
        out["disk"] = {}
    out["cpu_cores"] = __import__("os").cpu_count()

    runs = [r for r in list_run_rows() if (r.get("status") or "") == "running"]
    services = [
        s for s in list_service_rows() if (s.get("status") or "") in ("running", "starting")
    ]
    out["active"] = {"runs": len(runs), "services": len(services)}

    # 每个 user 的余量
    from state.db import list_users

    users = []
    for row in list_users():
        name = row["name"]
        users.append(
            {
                "user": name,
                "quota": quota_of(name),
                "usage": usage_of(name),
            }
        )
    out["users"] = users

    from sandbox.images import available_images

    out["images"] = sorted(available_images())
    return out
