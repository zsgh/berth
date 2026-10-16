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

import os
from pathlib import Path

import yaml

from sandbox.paths import ROOTFS

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
