"""在跑的执行的登记表（异步执行 B4）。

同步执行不需要登记；异步执行会把 `subprocess.Popen`、unit 名、输出文件路径
记在这里，`POST /scripts/{task_id}/cancel` 靠它来终止任务。

只用标准库，避免和 overlayfs / service 形成循环 import。
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ActiveRun:
    """一次正在跑的执行。"""

    task_id: str
    run_id: str
    unit: str
    proc: subprocess.Popen
    out_path: Path
    err_path: Path
    started_at: float
    timeout: float
    image: str = "base"
    network: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    cancelled: bool = False
    status: str = "running"

    @property
    def elapsed(self) -> float:
        return round(time.time() - self.started_at, 3)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "unit": self.unit,
            "pid": self.proc.pid,
            "started_at": self.started_at,
            "elapsed": self.elapsed,
            "timeout": self.timeout,
            "image": self.image,
            "network": self.network,
            "labels": self.labels,
            "status": self.status,
            "cancelled": self.cancelled,
        }


_LOCK = threading.Lock()
_ACTIVE: dict[str, ActiveRun] = {}


def register(run: ActiveRun) -> None:
    with _LOCK:
        _ACTIVE[run.task_id] = run


def unregister(task_id: str) -> ActiveRun | None:
    with _LOCK:
        return _ACTIVE.pop(task_id, None)


def get(task_id: str) -> ActiveRun | None:
    with _LOCK:
        return _ACTIVE.get(task_id)


def all_active() -> list[ActiveRun]:
    with _LOCK:
        return list(_ACTIVE.values())
