"""任务 1.3 验证：SQLite 元数据存储。

运行：python3 tests/test_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from state.db import get_run, init_db, list_runs, record_run  # noqa: E402


def main():
    init_db()

    record_run(
        "test-db-1",
        'print("hi")',
        {
            "stdout": "hi\n",
            "stderr": "",
            "exit_code": 0,
            "duration": 0.25,
            "artifacts": ["a.txt", "b.txt"],
        },
    )
    run = get_run("test-db-1")
    assert run is not None, "get_run 应该能查到"
    assert run["stdout"] == "hi\n"
    assert run["exit_code"] == 0
    assert run["duration_ms"] == 250, run
    assert run["artifacts"] == ["a.txt", "b.txt"], run
    assert run["created_at"] and run["finished_at"]

    # 覆盖写：同一 task_id 再跑一次，记录更新
    record_run(
        "test-db-1",
        'print("hi again")',
        {"stdout": "hi again\n", "stderr": "", "exit_code": 0, "duration": 0.1, "artifacts": []},
    )
    run = get_run("test-db-1")
    assert run["stdout"] == "hi again\n"
    assert run["artifacts"] == []

    runs = list_runs()
    assert len(runs) >= 1, runs
    assert {"task_id", "code", "stdout", "exit_code", "artifacts", "created_at"} <= set(runs[0])

    assert get_run("no-such-task") is None

    print(f"ok：runs={len(runs)}，最近一条: {runs[0]['task_id']}")
    print("全部通过 ✔")


if __name__ == "__main__":
    main()
