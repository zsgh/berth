"""异步执行（B4）。

`POST /scripts/run {"async": true}` → 立即返回 run id，后台线程跑；
`GET /scripts/{task_id}` 查状态 + 增量读输出；
`POST /scripts/{task_id}/cancel` 取消。

输出文件放在 `BASE_DIR/scripts/{task_id}/runs/{run_id}/`（不被 overlay 清理删掉），
增量读取直接 tail 这个文件。
"""
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

from sandbox import active as active_mod
from sandbox.active import ActiveRun
from sandbox.overlayfs import PreparedRun, cancel_active, prepare_run, spawn, wait_run
from sandbox.paths import BASE_DIR
from state.db import get_run, record_run, update_run

log = logging.getLogger(__name__)

RUNNING_STATUSES = ("running", "starting")


def _output_dir(task_id: str, run_id: str) -> Path:
    return BASE_DIR / "scripts" / task_id / "runs" / run_id


def start_async(
    code: str | None = None,
    cmd: list[str] | None = None,
    language: str | None = None,
    image: str = "base",
    task_id: str | None = None,
    timeout: int = 30,
    memory_mb: int = 256,
    cpu_percent: int = 50,
    tasks_max: int = 64,
    network: dict | None = None,
    mounts: list | None = None,
    labels: dict | None = None,
    owner: str | None = None,
) -> dict:
    """启动一次异步执行，立刻返回（不阻塞）。"""
    task_id = str(task_id) if task_id else f"auto-{uuid.uuid4().hex[:12]}"
    prepared = prepare_run(
        code,
        cmd=cmd,
        language=language,
        image=image,
        task_id=task_id,
        timeout=timeout,
        memory_mb=memory_mb,
        cpu_percent=cpu_percent,
        tasks_max=tasks_max,
        network=network,
        mounts=mounts,
        labels=labels,
        owner=owner,
        output_dir=_output_dir(task_id, "pending"),
    )
    return _launch(prepared)


def _launch(prepared: PreparedRun) -> dict:
    """真正开跑（输出目录按 run_id 重定位）。"""
    out_dir = _output_dir(prepared.task_id, prepared.run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    old_out = prepared.out_path
    prepared.out_path = out_dir / "stdout.txt"
    prepared.err_path = out_dir / "stderr.txt"
    # 清掉占位目录（prepare 阶段建的 pending/）
    if old_out is not None:
        try:
            old_out.parent.rmdir()
        except OSError:
            pass

    proc = spawn(prepared)
    handle = ActiveRun(
        task_id=prepared.task_id,
        run_id=prepared.run_id,
        unit=prepared.unit,
        proc=proc,
        out_path=prepared.out_path,
        err_path=prepared.err_path,
        started_at=prepared.started_at,
        timeout=prepared.timeout,
        image=prepared.image,
        network=prepared.network,
        labels=prepared.labels,
    )
    active_mod.register(handle)

    # 先落一条 running 记录（这样立刻就能查状态）
    record_run(
        prepared.task_id,
        prepared.audit_code,
        {
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "duration": 0,
            "artifacts": [],
            "status": "running",
            "image": prepared.image,
            "network": prepared.network,
            "labels": prepared.labels,
            "owner": prepared.owner,
            "timeout": prepared.timeout,
            "started_at": prepared.started_at,
        },
    )

    thread = threading.Thread(
        target=_watch, args=(prepared, proc), name=f"berth-run-{prepared.run_id}", daemon=True
    )
    thread.start()

    return {
        "task_id": prepared.task_id,
        "run_id": prepared.run_id,
        "status": "running",
        "image": prepared.image,
        "network": prepared.network,
        "labels": prepared.labels,
        "timeout": prepared.timeout,
        "started_at": prepared.started_at,
        "unit": prepared.unit,
    }


def _watch(prepared: PreparedRun, proc) -> None:
    """后台线程：等执行结束 → 写 DB → 清理 overlay 目录。"""
    try:
        result = wait_run(prepared, proc)
    except Exception as e:  # pragma: no cover - 兜底
        log.exception("异步执行异常: task_id=%s", prepared.task_id)
        result = {
            "stdout": "",
            "stderr": f"内部错误: {e}",
            "exit_code": -2,
            "duration": 0,
            "artifacts": [],
            "task_id": prepared.task_id,
            "image": prepared.image,
            "language": prepared.lang_label,
            "status": "error",
            "network": prepared.network,
            "labels": prepared.labels,
        }
    finally:
        active_mod.unregister(prepared.task_id)

    try:
        record_run(prepared.task_id, prepared.audit_code, result)
    except Exception:
        log.exception("record_run failed: task_id=%s", prepared.task_id)

    # overlay 目录清掉，输出目录留着（增量读 / 下载产物用）
    try:
        from sandbox.paths import sudo

        sudo("rm", "-rf", prepared.run_dir, check=False)
    except Exception:
        pass


def cancel(task_id: str) -> dict:
    """取消正在跑的执行。"""
    run = cancel_active(task_id)
    if run is None:
        return {"task_id": task_id, "status": "not-running", "cancelled": False}
    update_run(task_id, status="killed", updated_at=None)
    return {"task_id": task_id, "status": "killed", "cancelled": True, **run}


def live_output(task_id: str, offset: int = 0) -> dict | None:
    """正在跑的执行：读实时输出（增量）。跑完了返回 None。"""
    run = active_mod.get(task_id)
    if run is None:
        return None
    stdout = _tail(run.out_path, offset)
    stderr = _tail(run.err_path, offset)
    return {
        "task_id": task_id,
        "status": "killed" if run.cancelled else "running",
        "stdout": stdout["text"],
        "stdout_offset": stdout["offset"],
        "stderr": stderr["text"],
        "stderr_offset": stderr["offset"],
        "elapsed": run.elapsed,
        "timeout": run.timeout,
        "unit": run.unit,
        "image": run.image,
        "network": run.network,
        "labels": run.labels,
    }


def _tail(path: Path | None, offset: int = 0) -> dict:
    """从 offset 开始读文件内容（返回新的偏移量）。"""
    if not path or not path.exists():
        return {"text": "", "offset": int(offset)}
    try:
        size = path.stat().st_size
        start = max(0, min(int(offset), size))
        with open(path, "r", errors="replace") as f:
            f.seek(start)
            text = f.read()
            new_offset = f.tell()
    except OSError:
        return {"text": "", "offset": int(offset)}
    return {"text": text, "offset": new_offset}


def run_status(task_id: str) -> dict | None:
    """执行状态（DB + 实时输出合并）。"""
    record = get_run(task_id)
    live = live_output(task_id)
    if live is None:
        return record
    result = dict(record or {})
    result.update(
        {
            "task_id": task_id,
            "status": live["status"],
            "stdout": live["stdout"],
            "stderr": live["stderr"],
            "elapsed": live["elapsed"],
            "unit": live["unit"],
            "network": live["network"],
            "labels": live["labels"],
        }
    )
    return result
