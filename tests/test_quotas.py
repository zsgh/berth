"""M4 验证：每 user 的配额（并发任务 / 内存 / 服务数）。

不需要 systemd：直接测判定逻辑。

运行：python3 tests/test_quotas.py
"""
import os
import sys
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-quota-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox.quotas import (  # noqa: E402
    QuotaExceeded,
    check_run,
    check_service,
    quota_of,
    usage_of,
)


def main():
    from state.db import init_db, record_run, record_service, update_user

    init_db()

    print("=== 默认不限额 ===")
    from state.db import ensure_user

    ensure_user("acme")
    assert quota_of("acme") == {"runs": 0, "memory_mb": 0, "services": 0}
    check_run("acme", 999999)      # 不限 → 不抛
    check_service("acme")
    print("✔ 0 = 不限")

    print("=== 并发任务数配额 ===")
    update_user("acme", quota_runs=2, quota_memory_mb=1024, quota_services=3)
    assert quota_of("acme")["runs"] == 2
    record_run("r1", "x", {"status": "running", "owner": "acme", "memory_mb": 256})
    record_run("r2", "x", {"status": "running", "owner": "acme", "memory_mb": 256})
    try:
        check_run("acme", 256)
        raise AssertionError("应当超配额")
    except QuotaExceeded as e:
        print("✔ 超了:", e)
        assert e.detail["kind"] == "runs"

    print("=== 内存配额 ===")
    update_user("acme", quota_runs=0, quota_memory_mb=600, quota_services=0)
    try:
        check_run("acme", 512)
        raise AssertionError("应当超内存配额")
    except QuotaExceeded as e:
        print("✔ 超了:", e)
        assert e.detail["kind"] == "memory_mb"

    print("=== 服务数配额 ===")
    update_user("acme", quota_runs=0, quota_memory_mb=0, quota_services=1)
    record_service(
        service_id="s1", name="a", template="redis", unit="u1", host="localhost",
        port=1, internal_port=6379, status="running", dir="/tmp", persist=1, owner="acme",
    )
    try:
        check_service("acme")
        raise AssertionError("应当超服务配额")
    except QuotaExceeded as e:
        print("✔ 超了:", e)
        assert e.detail["kind"] == "services"

    print("=== 别的 user 不受影响 ===")
    ensure_user("other")
    check_run("other", 999999)
    check_service("other")
    print("✔ 配额按 user 隔离")

    print("=== 用量统计 ===")
    u = usage_of("acme")
    print("  ", u)
    assert u["runs"] == 2, u
    assert u["services"] == 1, u
    assert u["memory_mb"] == 512, u
    print("✔ 用量统计 OK")

    print("=== 容量上报 ===")
    from sandbox.quotas import capacity

    cap = capacity()
    print("   cpu_cores:", cap.get("cpu_cores"), "| images:", cap.get("images"))
    assert "cpu_cores" in cap and "users" in cap and "images" in cap
    assert any(x["user"] == "acme" for x in cap["users"])
    print("✔ 容量上报 OK")

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
