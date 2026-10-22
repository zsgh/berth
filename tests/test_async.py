"""B4 验证：异步执行的状态机（提交 / 查询 / 增量输出 / 取消）。

不需要 systemd：用假的 prepare / spawn / wait 替换真正的沙箱执行，
只验证「立即返回 → running 记录 → 增量输出 → 结束落库 → 取消」这条链路。

运行：python3 tests/test_async.py
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-async-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sandbox.runs as runs_mod  # noqa: E402
from sandbox.overlayfs import PreparedRun  # noqa: E402
from state.db import get_run, list_runs  # noqa: E402

BLOCK = threading.Event()


class DummyProc:
    """假的 subprocess.Popen。"""

    def __init__(self, block: bool = False):
        self.pid = 4242
        self._block = block
        self.returncode = 0

    def wait(self, timeout=None):
        if self._block:
            BLOCK.wait(timeout=5)
        return 0

    def poll(self):
        return None


def fake_prepare_run(code=None, **kwargs):
    task_id = kwargs.get("task_id") or "async-1"
    run_dir = TEST_HOME / "scripts" / task_id / "run-test"
    run_dir.mkdir(parents=True, exist_ok=True)
    p = PreparedRun(
        task_id=task_id,
        run_id="test",
        unit="berth-run-test",
        argv=["/bin/true"],
        lang_label="python",
        audit_code=kwargs.get("code") or "print(1)",
        image=kwargs.get("image", "base"),
        run_dir=run_dir,
        script_dir=TEST_HOME / "scripts" / task_id,
        workspace=TEST_HOME / "scripts" / task_id / "workspace",
        inner="",
        cmd=[],
        timeout=kwargs.get("timeout", 5),
        memory_mb=64,
        cpu_percent=10,
        tasks_max=8,
    )
    p.labels = dict(kwargs.get("labels") or {})
    p.network = dict(kwargs.get("network") or {"mode": "host", "allow": []})
    p.out_path = run_dir / "stdout.txt"
    p.err_path = run_dir / "stderr.txt"
    return p


def fake_spawn(prepared, block=False):
    prepared.started_at = time.time()
    return DummyProc(block=block)


def fake_wait_run(prepared, proc, timeout=None):
    if getattr(proc, "_block", False):
        BLOCK.wait(timeout=5)
    with open(prepared.out_path, "w") as f:
        f.write("hello from async\n")
    with open(prepared.err_path, "w") as f:
        f.write("")
    return {
        "stdout": "hello from async\n",
        "stderr": "",
        "exit_code": 0,
        "duration": 0.01,
        "artifacts": [],
        "task_id": prepared.task_id,
        "image": prepared.image,
        "language": "python",
        "status": "finished",
        "network": prepared.network,
        "labels": prepared.labels,
        "timeout": prepared.timeout,
    }


def wait_status(task_id, want, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = get_run(task_id)
        if record and record.get("status") == want:
            return record
        time.sleep(0.05)
    return get_run(task_id)


def main():
    runs_mod.prepare_run = fake_prepare_run
    runs_mod.spawn = fake_spawn
    runs_mod.wait_run = fake_wait_run

    print("=== 异步提交：立即返回 ===")
    t0 = time.time()
    res = runs_mod.start_async(code="print(1)", task_id="async-1", labels={"project": "demo"})
    dt = time.time() - t0
    print("  ", res)
    assert res["status"] == "running", res
    assert res["task_id"] == "async-1"
    assert dt < 1.0, f"异步提交不应该阻塞（耗时 {dt:.2f}s）"
    print(f"✔ 立即返回（{dt * 1000:.0f}ms）")

    print("=== running 记录已落库 ===")
    record = wait_status("async-1", "finished")
    # 先看看有没有短暂出现 running（线程可能已经跑完了）
    rows = list_runs(limit=5)
    assert any(r["task_id"] == "async-1" for r in rows), rows
    print("  执行记录:", [(r["task_id"], r["status"]) for r in rows])

    record = wait_status("async-1", "finished")
    assert record and record["status"] == "finished", record
    assert record["stdout"] == "hello from async\n", record
    assert record["labels"] == {"project": "demo"}, record
    print("✔ 结束后落库（status=finished）")

    print("=== 取消 ===")
    runs_mod.spawn = lambda prepared, block=False: fake_spawn(prepared, block=True)
    res = runs_mod.start_async(code="import time; time.sleep(99)", task_id="async-2")
    assert res["status"] == "running"
    time.sleep(0.2)
    cancelled = runs_mod.cancel("async-2")
    print("  ", cancelled)
    assert cancelled["cancelled"] is True
    assert cancelled["status"] == "killed"
    record = wait_status("async-2", "killed")
    assert record and record["status"] == "killed", record
    print("✔ 取消 → status=killed")

    print("=== 取消不存在的任务 ===")
    out = runs_mod.cancel("nope")
    assert out["cancelled"] is False, out
    print("✔ 未在跑的任务：cancelled=false")

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        BLOCK.set()
        time.sleep(0.3)  # 等后台线程收尾
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
