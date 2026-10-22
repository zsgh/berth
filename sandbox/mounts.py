"""只读挂载的白名单（B3 / B11）。

沙箱可以按请求把宿主目录挂进沙箱（被测代码库），但**不能**挂任意宿主路径 ——
否则沙箱里的代码等于拿到了宿主 root 视野。

规则：
- 源路径必须落在 `BERTH_MOUNT_ROOTS`（默认 `/srv/berth-mounts`）白名单目录内
- 路径会先 `resolve()`（解符号链接）再判断，防止 `白名单/../../etc` 这种绕过
- 挂载默认只读（`read_only: true`）
"""
from __future__ import annotations

from pathlib import Path

from sandbox import config


def allowed_roots() -> list[Path]:
    """当前允许挂载的宿主目录。"""
    return config.mount_roots()


def _resolve(path: Path) -> Path:
    """尽量把路径解析成绝对真实路径（不存在时退化成绝对路径）。"""
    try:
        return path.expanduser().resolve()
    except OSError:  # pragma: no cover - 极端权限
        return path.expanduser().absolute()


def resolve_source(source: str) -> Path:
    """校验并返回一个允许挂载的源目录。"""
    raw = str(source or "").strip()
    if not raw:
        raise ValueError("mounts[].source 不能为空")
    path = _resolve(Path(raw))
    roots = allowed_roots()
    if not roots:
        raise ValueError(
            "未配置 BERTH_MOUNT_ROOTS，不允许任何只读挂载"
            "（白名单为空；请设置成逗号/冒号分隔的目录列表）"
        )
    for root in roots:
        root_resolved = _resolve(root)
        if path == root_resolved or root_resolved in path.parents:
            if not path.exists():
                raise ValueError(f"挂载源不存在: {path}")
            if not path.is_dir():
                raise ValueError(f"挂载源不是目录: {path}")
            return path
    raise ValueError(
        f"挂载源 {path} 不在白名单内（允许: {[str(_resolve(r)) for r in roots]}；"
        "用 BERTH_MOUNT_ROOTS 配置）"
    )


def resolve_target(target: str) -> str:
    """沙箱内的挂载点：必须是绝对路径，且不能是 / 本身。"""
    raw = str(target or "").strip()
    if not raw:
        raise ValueError("mounts[].target 不能为空")
    if not raw.startswith("/"):
        raw = "/" + raw
    normalized = str(Path(raw))
    if normalized == "/":
        raise ValueError("mounts[].target 不能是 /")
    return normalized


def normalize_mounts(items) -> list[dict]:
    """把请求里的 mounts 归一化成 [{source, target, read_only}]（并校验白名单）。"""
    out: list[dict] = []
    for item in items or []:
        if isinstance(item, dict):
            source = item.get("source")
            target = item.get("target")
            read_only = item.get("read_only", True)
        else:
            source = getattr(item, "source", None)
            target = getattr(item, "target", None)
            read_only = getattr(item, "read_only", True)
        out.append(
            {
                "source": str(resolve_source(source)),
                "target": resolve_target(target),
                "read_only": bool(read_only),
            }
        )
    return out
