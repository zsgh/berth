"""镜像名解析（image: base / redis / mysql / <自定义>）。

约定：
- 名字 → 目录的映射来源（按优先级）：
  1) 绝对路径：直接当镜像用（如 /opt/berth/rootfs）
  2) images.yaml（默认 <仓库>/images.yaml，可用 BERTH_IMAGES_FILE 覆盖）：
       images:
         base: /opt/berth/rootfs
         node: /opt/berth/rootfs-node
  3) 自动发现：BERTH_ROOTFS（默认 /opt/berth/rootfs）的同级 rootfs-* 目录，
     后缀即名字 —— 正好匹配 scripts/setup_rootfs.sh 的产物命名
     （rootfs / rootfs-redis / rootfs-mysql …）

用法：
    path = resolve_image("base")
    path = resolve_image("/srv/custom/rootfs")
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path

import yaml

from sandbox.paths import ROOTFS, dir_size, priv_cmd, sudo

log = logging.getLogger(__name__)

IMAGES_FILE = Path(
    os.environ.get("BERTH_IMAGES_FILE", str(Path(__file__).resolve().parent.parent / "images.yaml"))
)


def _discovered() -> dict[str, str]:
    """自动发现：base = BERTH_ROOTFS，兄弟目录 rootfs-* 的 后缀 → 目录。"""
    images: dict[str, str] = {"base": str(ROOTFS)}
    root = Path(ROOTFS)
    parent = root.parent
    prefix = root.name + "-"
    if parent.is_dir():
        for p in sorted(parent.glob(prefix + "*")):
            if p.is_dir():
                images[p.name[len(prefix):]] = str(p)
    return images


def available_images() -> dict[str, str]:
    """当前可用的镜像表（images.yaml 优先，否则自动发现）。"""
    if IMAGES_FILE.exists():
        try:
            data = yaml.safe_load(IMAGES_FILE.read_text(encoding="utf-8")) or {}
            configured = data.get("images") or {}
            if configured:
                return {str(k): str(v) for k, v in configured.items()}
        except Exception:
            pass  # 配置坏了退回自动发现（不阻塞执行）
    return _discovered()


def resolve_image(name: str) -> Path:
    """把镜像名（或绝对路径）解析成 rootfs 目录。"""
    if not name:
        raise ValueError("image 不能为空")
    p = Path(name)
    if p.is_absolute():
        if p.is_dir():
            return p
        raise ValueError(f"镜像路径不存在: {p}")

    images = available_images()
    if name not in images:
        raise ValueError(
            f"未知镜像: {name!r}（可用: {sorted(images)}；"
            f"自定义可建 images.yaml 或用 BERTH_IMAGES_FILE 指定）"
        )
    path = Path(images[name])
    if not path.is_dir():
        raise ValueError(
            f"镜像 {name!r} 的目录不存在: {path}（构建: sudo scripts/setup_rootfs.sh --with-{name}）"
        )
    return path


# ─────────────────────────── 镜像管理（制作 / 删除） ───────────────────────────

# 镜像名 → 目录名：只允许安全字符（它会变成 rootfs-<name> 目录）
IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

BUILD_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_image.sh"


def images_root() -> Path:
    """镜像所在目录（BERTH_ROOTFS 的同级目录，setup_rootfs.sh 的产物都在这）。"""
    return Path(ROOTFS).parent


def managed_images() -> dict[str, str]:
    """berth 自己管的镜像（rootfs-* 目录）：这些才允许删除。

    images.yaml 里指向别处的镜像不归 berth 管（可能是共享的），不提供删除。
    """
    root = images_root()
    prefix = Path(ROOTFS).name + "-"
    out: dict[str, str] = {}
    if root.is_dir():
        for p in sorted(root.glob(prefix + "*")):
            if p.is_dir():
                out[p.name[len(prefix):]] = str(p)
    return out


def validate_image_name(name: str) -> str:
    """镜像名校验（会变成目录名）。"""
    name = str(name or "").strip()
    if not IMAGE_NAME_RE.match(name):
        raise ValueError(
            f"镜像名不合法: {name!r}（只允许小写字母/数字/点/短横线，1-32 字符，且以字母或数字开头）"
        )
    return name


def target_path_for(name: str) -> Path:
    """新镜像的目标目录（必须在 images_root 下）。"""
    name = validate_image_name(name)
    root = images_root().resolve()
    target = (root / f"{Path(ROOTFS).name}-{name}").resolve()
    if target.parent != root:
        raise ValueError(f"镜像目录越界: {target}")
    return target


def images_in_use() -> dict[str, list[str]]:
    """哪些镜像正被服务用着（按服务名列出）。"""
    from state.db import list_service_rows

    used: dict[str, list[str]] = {}
    for row in list_service_rows():
        spec = row.get("spec")
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except (TypeError, json.JSONDecodeError):
                spec = {}
        image = (spec or {}).get("image")
        if not image:
            continue
        used.setdefault(str(image), []).append(str(row.get("name") or row.get("service_id")))
    return used


def delete_image(name: str) -> dict:
    """删除一个镜像（只删 berth 自己管的 rootfs-* 目录）。

    拒绝：base 镜像、正在被服务使用的镜像、正在构建中的镜像。
    """
    from sandbox import config

    if not config.allow_image_delete():
        raise PermissionError("未开启镜像删除（BERTH_ALLOW_IMAGE_DELETE=0）")

    name = str(name or "").strip()
    if not name:
        raise ValueError("缺少镜像名")
    if name == Path(ROOTFS).name or name == "base":
        raise ValueError("不能删除 base 镜像（其它镜像都基于它）")

    managed = managed_images()
    if name not in managed:
        raise ValueError(
            f"镜像 {name!r} 不在 berth 管理范围内（只能删 rootfs-* 目录；"
            f"当前可删: {sorted(managed)}）"
        )
    path = Path(managed[name])
    # 二次确认路径确实在 images_root 下
    root = images_root().resolve()
    resolved = path.resolve()
    if resolved.parent != root or not resolved.name.startswith(Path(ROOTFS).name + "-"):
        raise ValueError(f"拒绝删除越界路径: {resolved}")

    in_use = images_in_use()
    if name in in_use:
        raise ValueError(
            f"镜像 {name!r} 正被服务使用（{', '.join(in_use[name])}）："
            f"先删掉这些服务，或把它们换成别的镜像"
        )
    from state.db import list_image_builds

    for build in list_image_builds():
        if build.get("name") == name and build.get("status") == "building":
            raise ValueError(f"镜像 {name!r} 正在构建中，先取消构建")

    size = dir_size(path)
    _rm_rf(path)
    return {"name": name, "path": str(path), "deleted": True, "size_bytes": size}


def _rm_rf(path: Path) -> None:
    """删目录树：普通权限优先，不行再 sudo（镜像目录是 root 的）。"""
    import shutil

    try:
        shutil.rmtree(path)
        return
    except (OSError, PermissionError):
        pass
    sudo("rm", "-rf", path, check=False)
    if path.exists():
        raise RuntimeError(f"删除失败（需要 root 权限）: {path}")


# ─────────────────────────── 制作镜像 ───────────────────────────


def start_image_build(
    *,
    name: str,
    base: str = "base",
    packages: list[str] | None = None,
    pip_packages: list[str] | None = None,
    commands: list[str] | None = None,
    force: bool = False,
) -> dict:
    """开始制作镜像（后台跑，立即返回 build id）。

    制作镜像 = 在宿主上以 root 跑 chroot + apt → 权限比别的接口高一档，
    所以要 `BERTH_ALLOW_IMAGE_BUILD=1` 才允许。
    """
    from sandbox import config

    if not config.allow_image_build():
        raise PermissionError(
            "未开启镜像制作（BERTH_ALLOW_IMAGE_BUILD=0）："
            "这是宿主 root 级操作，需要显式打开"
        )

    name = validate_image_name(name)
    target = target_path_for(name)
    source = resolve_image(base)
    if source.resolve() == target.resolve():
        raise ValueError("源镜像和目标镜像不能相同")
    if target.exists() and not force:
        raise ValueError(f"镜像已存在: {target.name}（要重建请加 force=true）")

    build_id = "img-" + uuid.uuid4().hex[:8]
    spec = {
        "name": name,
        "base": base,
        "source": str(source),
        "target": str(target),
        "packages": list(packages or []),
        "pip_packages": list(pip_packages or []),
        "commands": list(commands or []),
        "force": bool(force),
    }
    from state.db import record_image_build

    record_image_build(build_id, name, spec)

    thread = threading.Thread(
        target=_run_build, args=(build_id, spec), name=f"berth-build-{build_id}", daemon=True
    )
    thread.start()
    return {"build_id": build_id, "name": name, "status": "building", "spec": spec}


def _build_command(spec: dict) -> list[str]:
    """组装构建脚本的命令行（需要 root：daemon 是 root 就不加 sudo）。"""
    cmd = priv_cmd(
        str(BUILD_SCRIPT),
        "--name", spec["name"],
        "--source", spec["source"],
    )
    if spec.get("packages"):
        cmd += ["--packages", " ".join(spec["packages"])]
    if spec.get("pip_packages"):
        cmd += ["--pip", " ".join(spec["pip_packages"])]
    for c in spec.get("commands") or []:
        cmd += ["--command", c]
    if spec.get("force"):
        cmd += ["--force"]
    return cmd


def _run_build(build_id: str, spec: dict) -> None:
    """后台跑构建脚本，把输出实时写进 build 记录。"""
    from state.db import update_image_build

    cmd = _build_command(spec)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        log_lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            log_lines.append(line)
            update_image_build(build_id, log="".join(log_lines[-400:]))
        proc.wait()
        ok = proc.returncode == 0
        update_image_build(
            build_id,
            status="success" if ok else "failed",
            log="".join(log_lines[-400:]),
            error=None if ok else f"构建脚本退出码 {proc.returncode}",
            image_path=spec["target"] if ok else None,
        )
    except Exception as e:  # pragma: no cover - 兜底
        log.exception("镜像构建失败: %s", build_id)
        update_image_build(build_id, status="failed", error=str(e))

