"""systemd 单元辅助：状态查询 + 有界停止。

背景：util-linux unshare 在某些情况下会把信号掩码弄坏（blocked），
沙箱里的进程可能收不到 SIGTERM，`systemctl stop` 会卡到 TimeoutStopSec。
所以所有停止路径统一走这里：优雅 stop 限时 → 到点直接对 cgroup 强杀。
"""
from __future__ import annotations

import json
import re
import subprocess

from sandbox.paths import sudo


def unit_active(unit: str) -> bool:
    res = subprocess.run(["systemctl", "is-active", "--quiet", unit], capture_output=True)
    return res.returncode == 0


def list_sbx_units() -> list[str]:
    """列出宿主上所有 berth-* 开头的 systemd 单元。"""
    res = subprocess.run(
        ["systemctl", "list-units", "--all", "--output=json", "--no-pager", "berth-*"],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        try:
            return [item["unit"] for item in json.loads(res.stdout)]
        except (json.JSONDecodeError, TypeError, KeyError):
            pass
    # 回退：纯文本解析（unit 描述里可能带换行，只认 unit 名开头的行）
    units = []
    for line in res.stdout.splitlines():
        m = re.match(r"^(\S+\.(?:service|scope|socket|timer|mount|slice|target))\s", line)
        if m:
            units.append(m.group(1))
    return units


def kill_unit(unit: str) -> None:
    """SIGKILL 整个 cgroup（SIGKILL 不可被屏蔽，一定能杀掉）。"""
    sudo("systemctl", "kill", "--signal=SIGKILL", unit, check=False)


def stop_unit(unit: str, graceful_timeout: float = 10, force: bool = True) -> None:
    """有界停止一个 unit：先给优雅 stop 的机会，卡住就强杀。"""
    try:
        subprocess.run(
            ["sudo", "-n", "systemctl", "stop", unit],
            capture_output=True,
            text=True,
            timeout=graceful_timeout,
        )
    except subprocess.TimeoutExpired:
        pass

    if force and unit_active(unit):
        kill_unit(unit)
        try:
            subprocess.run(
                ["sudo", "-n", "systemctl", "stop", unit],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            pass

    sudo("systemctl", "reset-failed", unit, check=False)
