"""公共路径 / 配置 / 提权辅助。

所有需要 root 的操作都走 `sudo -n`（环境已配 NOPASSWD）。
运行数据统一放在 BASE_DIR（默认 /var/lib/berth）：

    /var/lib/berth/
    ├── daemon.db                     # SQLite 元数据
    ├── scripts/{task_id}/
    │   ├── run-{run_id}/             # 单次执行的 overlay（用完即删）
    │   │   ├── upper/ work/ newroot/
    │   └── workspace/                # 任务工作区，跨多次执行保留
    └── services/{service_id}/        # 长驻服务
        ├── run-{run_id}/upper|work|newroot
        └── volumes/{容器内路径}/     # persist 卷
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# 只读基础 rootfs（debootstrap 构建）
ROOTFS = os.environ.get("BERTH_ROOTFS", "/opt/berth/rootfs")

# 运行数据根目录
BASE_DIR = Path(os.environ.get("BERTH_HOME", "/var/lib/berth"))

# 沙箱内降权到的 uid/gid（宿主上的数值 uid，不需要真实用户）
SANDBOX_UID = 1000
SANDBOX_GID = 1000


def sudo(*args, check: bool = True) -> subprocess.CompletedProcess:
    """以 root 执行一条命令。

    - 已经是 root（euid==0）：直接执行（某些最小系统没有 sudo）
    - 普通用户：走 `sudo -n`（需要免密 sudo，见 README）
    """
    cmd = [str(a) for a in args]
    argv = cmd if os.geteuid() == 0 else ["sudo", "-n", *cmd]
    try:
        res = subprocess.run(argv, capture_output=True, text=True)
    except OSError as e:
        # 比如宿主没装 sudo（最小系统）
        res = subprocess.CompletedProcess(argv, 127, "", str(e))
    if check and res.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} failed: rc={res.returncode}\n{res.stderr}"
        )
    return res


def priv_cmd(*args) -> list[str]:
    """需要 root 权限的命令行（已经是 root 就直接跑，否则加 `sudo -n`）。

    用于 systemctl / journalctl 这类必须以 root 执行的命令。
    """
    cmd = [str(a) for a in args]
    return cmd if os.geteuid() == 0 else ["sudo", "-n", *cmd]


def ensure_base_dir() -> None:
    """确保 BASE_DIR 存在；非 root 模式下归当前用户所有。"""
    if BASE_DIR.exists():
        return
    sudo("mkdir", "-p", BASE_DIR)
    if os.geteuid() != 0:
        sudo("chown", f"{os.getuid()}:{os.getgid()}", BASE_DIR)
