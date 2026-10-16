"""孤儿清理安全回归：只清 berth-svc-* / berth-run-*，绝不误伤 daemon 自己的 unit。

背景：macOS bootstrap 演练时发现，daemon 启动清理如果按 "berth-*" 一把捞，
会把自己所在的 unit（berth-boottest.service 这类）当孤儿 SIGKILL，
systemd 重启它 → 无限自杀循环（实测踩到）。

运行：python3 tests/test_orphans.py
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox.service import cleanup_orphans  # noqa: E402
from sandbox.systemd_units import list_managed_units  # noqa: E402

DAEMON_LIKE = "berth-daemon-test"        # 模仿 daemon 自己的 unit 名
SERVICE_LIKE = "berth-svc-testfake"      # 模仿 workload 服务名


def start_unit(unit: str) -> None:
    subprocess.run(
        ["sudo", "-n", "systemd-run", "--unit", unit, "--collect", "--", "sleep", "120"],
        check=True,
        capture_output=True,
        text=True,
    )


def is_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def main():
    for u in (DAEMON_LIKE, SERVICE_LIKE):
        subprocess.run(
            ["sudo", "-n", "systemctl", "reset-failed", f"{u}.service"], capture_output=True
        )
    start_unit(DAEMON_LIKE)
    start_unit(SERVICE_LIKE)
    time.sleep(0.5)

    units = list_managed_units()
    print("managed units:", units)
    assert f"{SERVICE_LIKE}.service" in units, "workload 服务单元应被纳入管理"
    assert f"{DAEMON_LIKE}.service" not in units, "daemon 风格的单元绝不能被列入（否则会被自杀）"
    print("✔ 名单过滤正确（只认 berth-svc-* / berth-run-*）")

    cleaned = cleanup_orphans()
    print("cleaned:", cleaned)
    assert all(DAEMON_LIKE not in u for u in cleaned), "不应清理 daemon 风格的单元"
    assert is_active(f"{DAEMON_LIKE}.service"), "daemon 风格的单元必须活着"
    time.sleep(0.5)
    assert not is_active(f"{SERVICE_LIKE}.service"), "DB 里没有的伪服务应被回收"
    print("✔ 清理行为正确（daemon 风格单元存活 / 伪服务被回收）")

    subprocess.run(["sudo", "-n", "systemctl", "stop", DAEMON_LIKE], capture_output=True)
    subprocess.run(
        ["sudo", "-n", "systemctl", "reset-failed", f"{DAEMON_LIKE}.service"], capture_output=True
    )
    print("全部通过 ✔")


if __name__ == "__main__":
    main()
