"""任务 workspace 的文件进出（B3）。

    PUT  /scripts/{task_id}/files         上传（多文件 / tar 包）→ 解到 /workspace
    GET  /scripts/{task_id}/files         打包下载（整个 workspace 或子目录）
    GET  /scripts/{task_id}/files/{path}  下载单个文件
    GET  /scripts/{task_id}/tree          文件树（带大小 / mime）

安全：
- task_id 必须是安全名字（不能带路径分隔符 / ..）
- 上传解包拒绝绝对路径、`..` 穿越、软链接越界
- 挂载点固定在 `BASE_DIR/scripts/{task_id}/workspace`，沙箱内看到的就是 `/workspace`
"""
from __future__ import annotations

import io
import mimetypes
import os
import re
import tarfile
import time
from pathlib import Path

from sandbox.paths import BASE_DIR, SANDBOX_GID, SANDBOX_UID, ensure_base_dir, sudo

TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# 预览时永远当纯文本渲染的类型
TEXT_MIME_PREFIXES = ("text/",)
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/svg+xml"}
HTML_MIMES = {"text/html", "application/xhtml+xml"}


def validate_task_id(task_id: str) -> str:
    """task_id 只允许安全字符（它会变成目录名）。"""
    name = str(task_id or "").strip()
    if not name or not TASK_ID_RE.match(name) or not name.strip("."):
        raise ValueError(
            f"task_id 不合法: {task_id!r}（只允许字母/数字/点/下划线/短横线，1-128 字符）"
        )
    return name


def task_workspace(task_id: str) -> Path:
    """任务 workspace 目录（宿主路径）。"""
    return BASE_DIR / "scripts" / validate_task_id(task_id) / "workspace"


def safe_join(root: Path, rel: str) -> Path:
    """把相对路径拼到 root 下，拒绝越界。"""
    rel = str(rel or "").strip().lstrip("/")
    if not rel:
        return root
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"非法路径（含 ..）: {rel!r}")
    target = root.joinpath(*parts)
    # 再确认没跑出 root（防符号链接）
    try:
        resolved = target.resolve()
        root_resolved = root.resolve()
    except OSError:  # pragma: no cover
        resolved, root_resolved = target, root
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"非法路径（越界）: {rel!r}")
    return target


def _chown_sandbox(path: Path) -> None:
    """让沙箱里的 uid 1000 能读（上传的文件）。失败不影响上传本身。"""
    try:
        os.chown(path, SANDBOX_UID, SANDBOX_GID)
    except (OSError, PermissionError):
        try:
            sudo("chown", "-R", f"{SANDBOX_UID}:{SANDBOX_GID}", path, check=False)
        except Exception:
            pass  # 没有 root 权限就跳过（开发机 / 测试环境）


# ─────────────────────────── 上传 ───────────────────────────


def _looks_like_tar(data: bytes) -> bool:
    return len(data) > 262 and data[257:262] == b"ustar"


def extract_tar(data: bytes, dest: Path) -> list[str]:
    """解 tar 包到 dest（拒绝穿越 / 越界软链接）。"""
    written: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for member in tar.getmembers():
            name = member.name.replace("\\", "/").lstrip("/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts or any(p == ".." for p in parts):
                continue
            if member.issym() or member.islnk():
                # 软链接：只允许指向包内相对路径
                link = member.linkname.replace("\\", "/").lstrip("/")
                if not link or link.startswith("/") or ".." in link.split("/"):
                    continue
            target = dest.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with open(target, "wb") as f:
                f.write(src.read())
            written.append("/".join(parts))
    return written


def upload_files(
    task_id: str,
    files: list[tuple[str, bytes]] | None = None,
    archive: bytes | None = None,
) -> dict:
    """上传文件到任务 workspace。

    files: [(相对路径, 内容)]；archive: tar 包内容（可选，和 files 可以同时给）。
    """
    task_id = validate_task_id(task_id)
    ensure_base_dir()
    ws = task_workspace(task_id)
    ws.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for name, data in files or []:
        rel = str(name or "").strip().lstrip("/")
        if not rel:
            continue
        target = safe_join(ws, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = data
        # 单个上传就是 tar 包 → 自动解包
        if _looks_like_tar(data):
            written.extend(extract_tar(data, ws))
            continue
        target.write_bytes(payload)
        written.append(rel)

    if archive:
        written.extend(extract_tar(archive, ws))

    _chown_sandbox(ws)
    return {"task_id": task_id, "workspace": str(ws), "files": sorted(set(written))}


def write_file(task_id: str, rel: str, data: bytes) -> dict:
    """写单个文件（上传接口的简化版）。"""
    task_id = validate_task_id(task_id)
    ws = task_workspace(task_id)
    ws.mkdir(parents=True, exist_ok=True)
    target = safe_join(ws, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    _chown_sandbox(target)
    return {"task_id": task_id, "path": str(rel), "size": len(data)}


def delete_path(task_id: str, rel: str) -> dict:
    """删 workspace 里的文件 / 目录。"""
    task_id = validate_task_id(task_id)
    ws = task_workspace(task_id)
    target = safe_join(ws, rel)
    if not target.exists():
        raise ValueError(f"没有这个文件: {rel}")
    if target.is_dir():
        try:
            import shutil

            shutil.rmtree(target)
        except OSError:
            sudo("rm", "-rf", target, check=False)
    else:
        try:
            target.unlink()
        except OSError:
            sudo("rm", "-f", target, check=False)
    return {"task_id": task_id, "path": str(rel), "deleted": True}


# ─────────────────────────── 读取 / 下载 ───────────────────────────


def read_file(task_id: str, rel: str) -> bytes:
    ws = task_workspace(task_id)
    target = safe_join(ws, rel)
    if not target.is_file():
        raise FileNotFoundError(str(rel))
    return target.read_bytes()


def guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def file_tree(task_id: str, rel: str = "") -> list[dict]:
    """文件树（相对路径 + 大小 + mime + 修改时间）。"""
    ws = task_workspace(task_id)
    root = safe_join(ws, rel) if rel else ws
    if not root.exists():
        return []
    out: list[dict] = []
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            out.append(
                {
                    "path": str(p.relative_to(ws)),
                    "name": p.name,
                    "is_dir": True,
                    "size": 0,
                    "mime": "inode/directory",
                    "modified_at": _mtime(p),
                }
            )
        elif p.is_file():
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            out.append(
                {
                    "path": str(p.relative_to(ws)),
                    "name": p.name,
                    "is_dir": False,
                    "size": size,
                    "mime": guess_mime(p.name),
                    "modified_at": _mtime(p),
                }
            )
    return out


def _mtime(path: Path) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime))
    except OSError:
        return ""


def make_tar(task_id: str, rel: str = "") -> bytes:
    """把 workspace（或子目录）打包成 tar（内存里）。"""
    ws = task_workspace(task_id)
    root = safe_join(ws, rel) if rel else ws
    if not root.exists():
        return b""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # tar.add 会递归目录（不要再逐个加文件，否则会重复）
        tar.add(root, arcname=root.name)
    return buf.getvalue()


def workspace_size(task_id: str) -> int:
    ws = task_workspace(task_id)
    if not ws.exists():
        return 0
    total = 0
    for p in ws.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total
