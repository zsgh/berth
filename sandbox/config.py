"""集中读取环境变量配置。

所有配置都在**调用时**读取（不是 import 时），方便测试脚本在 import 之后改环境变量。

| 变量 | 默认 | 说明 |
|---|---|---|
| `BERTH_TOKEN` | - | Bearer token；未设置 = 不鉴权（仅限本机自用） |
| `BERTH_HOST` | `127.0.0.1` | 默认监听地址；对外监听必须显式配置 |
| `BERTH_PORT` | `9000` | 默认端口 |
| `BERTH_MAX_TIMEOUT` | `600` | `/scripts/run` 的 timeout 上限（秒） |
| `BERTH_MAX_MEMORY_MB` | `4096` | 内存上限（MB） |
| `BERTH_MAX_CPU_PERCENT` | `400` | CPU 配额上限（100 = 1 核） |
| `BERTH_MAX_PROCESSES` | `512` | 进程数上限（cgroup TasksMax） |
| `BERTH_MOUNT_ROOTS` | `/srv/berth-mounts` | 只读挂载允许的宿主目录（冒号分隔） |
| `BERTH_UI_ENABLED` | `1` | 是否托管 `/ui` 静态页面 |
| `BERTH_UI_DIR` | `<仓库>/ui` | UI 静态目录 |
| `BERTH_EVENTS_ENABLED` | `1` | 是否记录操作审计（`/events`） |
| `BERTH_TOKEN_FILE` | - | 本地令牌文件：启动时不存在就自动生成（0600）；同机 agent 直接读它 |
| `BERTH_BOOTSTRAP_TTL` | `1800` | 一次性引导码的有效期（秒） |
| `BERTH_ALLOW_IMAGE_BUILD` | `0` | 是否允许**制作**镜像（宿主 root 级操作，默认关） |
| `BERTH_ALLOW_IMAGE_DELETE` | `1` | 是否允许**删除**镜像（默认开） |
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent


def token() -> str | None:
    """Bearer token；未配置返回 None（表示不鉴权）。"""
    values = tokens()
    return values[0] if values else None


def token_file() -> Path | None:
    """本地令牌文件路径（BERTH_TOKEN_FILE）。

    设了这个 → 启动时不存在就自动生成一个（0600），同机的 agent 直接读它，
    不用把长期凭证复制到各个地方。
    """
    raw = (os.environ.get("BERTH_TOKEN_FILE") or "").strip()
    return Path(raw).expanduser() if raw else None


def tokens() -> list[str]:
    """有效的 Bearer token 列表（admin）。

    - `BERTH_TOKEN`：逗号/空格分隔的多个 —— 轮换时同时挂新旧两个，
      客户端切完再把旧的摘掉（零停机）
    - `BERTH_TOKEN_FILE` 指向的文件里那个（自动生成的本机 token）
    """
    raw = os.environ.get("BERTH_TOKEN") or ""
    out = [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
    path = token_file()
    if path is not None:
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    out.append(value)
        except OSError:
            pass
    return out


def bootstrap_ttl() -> int:
    """一次性引导码有效期（秒）。"""
    return _int_env("BERTH_BOOTSTRAP_TTL", 1800)


def bind_host() -> str:
    """默认监听地址：127.0.0.1（对外监听必须显式配置 BERTH_HOST）。"""
    return (os.environ.get("BERTH_HOST") or "127.0.0.1").strip()


def bind_port() -> int:
    try:
        return int(os.environ.get("BERTH_PORT") or "9000")
    except ValueError:
        return 9000


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def max_timeout() -> int:
    """timeout 上限（秒）。"""
    return _int_env("BERTH_MAX_TIMEOUT", 600)


def max_memory_mb() -> int:
    """内存上限（MB）。"""
    return _int_env("BERTH_MAX_MEMORY_MB", 4096)


def max_cpu_percent() -> int:
    """CPU 配额上限（100 = 1 核）。"""
    return _int_env("BERTH_MAX_CPU_PERCENT", 400)


def max_processes() -> int:
    """进程数上限（cgroup TasksMax）。"""
    return _int_env("BERTH_MAX_PROCESSES", 512)


def mount_roots() -> list[Path]:
    """允许被 `mounts` 只读挂载的宿主目录白名单。"""
    raw = os.environ.get("BERTH_MOUNT_ROOTS")
    if raw is None:
        raw = "/srv/berth-mounts"
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(part).expanduser())
        except OSError:  # pragma: no cover - 极端路径
            continue
    return roots


def ui_enabled() -> bool:
    return (os.environ.get("BERTH_UI_ENABLED") or "1").strip().lower() not in ("0", "false", "no")


def ui_dir() -> Path:
    return Path(os.environ.get("BERTH_UI_DIR") or str(REPO_DIR / "ui"))


def events_enabled() -> bool:
    return (os.environ.get("BERTH_EVENTS_ENABLED") or "1").strip().lower() not in ("0", "false", "no")


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def allow_image_build() -> bool:
    """是否允许**制作**镜像（默认关）。

    制作镜像 = 在宿主上以 root 跑 chroot + apt（maintainer script 也是 root），
    权限比 berth 其它接口高一档，所以要显式开。
    """
    return _flag("BERTH_ALLOW_IMAGE_BUILD", False)


def allow_image_delete() -> bool:
    """是否允许**删除**镜像（默认开；路径有强校验，只删 berth 自己管的目录）。"""
    return _flag("BERTH_ALLOW_IMAGE_DELETE", True)


def limits() -> dict:
    """对外暴露的上限（B4 / `GET /system/info`）。"""
    return {
        "timeout_seconds": max_timeout(),
        "memory_mb": max_memory_mb(),
        "cpu_percent": max_cpu_percent(),
        "processes": max_processes(),
    }
