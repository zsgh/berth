"""B12 + B4 验证：标签（labels）与执行记录过滤 / 分页。

不需要 systemd / root：只测 SQLite 层。

运行：python3 tests/test_labels.py
"""
import os
import sys
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-labels-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from state.db import (  # noqa: E402
    init_db,
    list_events,
    list_runs,
    match_labels,
    parse_label_filters,
    record_event,
    record_run,
    update_run,
)


def main():
    init_db()

    print("=== 标签解析 ===")
    assert parse_label_filters(["project=foo"]) == {"project": "foo"}
    assert parse_label_filters(["project=foo", "run=7"]) == {"project": "foo", "run": "7"}
    assert parse_label_filters(["project=foo,run=7"]) == {"project": "foo", "run": "7"}
    assert parse_label_filters(["justkey"]) == {"justkey": None}
    print("✔ 标签解析 OK")

    print("=== 标签匹配 ===")
    have = {"project": "foo", "run": "7"}
    assert match_labels(have, {"project": "foo"}) is True
    assert match_labels(have, {"project": "bar"}) is False
    assert match_labels(have, {"project": "foo", "run": "7"}) is True
    assert match_labels(have, {"missing": None}) is False
    assert match_labels(have, {"run": None}) is True
    print("✔ 标签匹配 OK")

    print("=== 执行记录带标签 ===")
    record_run(
        "run-a",
        "print(1)",
        {
            "stdout": "1\n",
            "stderr": "",
            "exit_code": 0,
            "duration": 0.1,
            "artifacts": [],
            "status": "finished",
            "image": "base",
            "labels": {"project": "foo", "run": "1"},
            "network": {"mode": "host", "allow": []},
        },
    )
    record_run(
        "run-b",
        "print(2)",
        {
            "stdout": "2\n",
            "stderr": "",
            "exit_code": 1,
            "duration": 0.2,
            "artifacts": [],
            "status": "finished",
            "image": "runner",
            "labels": {"project": "bar"},
        },
    )
    record_run(
        "run-c",
        "print(3)",
        {
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "duration": 0,
            "artifacts": [],
            "status": "running",
            "image": "base",
        },
    )

    rows = list_runs(limit=10)
    assert len(rows) == 3, rows
    assert all("labels" in r for r in rows)
    print("✔ 执行记录写入（含标签 / 状态 / 镜像）")

    print("=== 按标签过滤 ===")
    only_foo = list_runs(limit=10, labels={"project": "foo"})
    assert [r["task_id"] for r in only_foo] == ["run-a"], only_foo
    only_bar = list_runs(limit=10, labels={"project": "bar"})
    assert [r["task_id"] for r in only_bar] == ["run-b"], only_bar
    both = list_runs(limit=10, labels={"project": "foo", "run": "1"})
    assert [r["task_id"] for r in both] == ["run-a"], both
    none = list_runs(limit=10, labels={"project": "nope"})
    assert none == [], none
    print("✔ 标签过滤 OK")

    print("=== 状态 / 镜像过滤 ===")
    running = list_runs(limit=10, status="running")
    assert [r["task_id"] for r in running] == ["run-c"], running
    base = list_runs(limit=10, image="base")
    assert {r["task_id"] for r in base} == {"run-a", "run-c"}, base
    print("✔ 状态 / 镜像过滤 OK")

    print("=== 异步状态流转 ===")
    update_run("run-c", status="killed", exit_code=-1)
    rows = {r["task_id"]: r for r in list_runs(limit=10)}
    assert rows["run-c"]["status"] == "killed", rows["run-c"]
    print("✔ 状态可更新")

    print("=== 无标签的行为不变 ===")
    assert rows["run-c"]["labels"] == {}, rows["run-c"]
    assert len(list_runs(limit=10, labels={})) == 3
    print("✔ 无标签过滤 = 不过滤")

    print("=== 操作审计 ===")
    record_event("PUT /services/x", target="x", actor="token:abc", method="PUT", status=200)
    record_event("DELETE /services/x", target="x", actor="token:abc", method="DELETE", status=200)
    events = list_events(limit=10)
    assert len(events) == 2, events
    assert events[0]["action"] == "DELETE /services/x"
    assert events[0]["actor"] == "token:abc"
    filtered = list_events(limit=10, action="PUT /services/x")
    assert len(filtered) == 1, filtered
    print("✔ 审计写入 / 过滤 OK")

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
