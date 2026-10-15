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
    """以 root 执行一条命令（sudo -n，无交互）。"""
    res = subprocess.run(
        ["sudo", "-n", *[str(a) for a in args]],
        capture_output=True,
        text=True,
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"sudo {' '.join(map(str, args))} failed: rc={res.returncode}\n{res.stderr}"
        )
    return res


def ensure_base_dir() -> None:
    """确保 BASE_DIR 存在且归当前用户所有。"""
    if BASE_DIR.exists():
        return
    sudo("mkdir", "-p", BASE_DIR)
    sudo("chown", f"{os.getuid()}:{os.getgid()}", BASE_DIR)
