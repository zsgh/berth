#!/bin/bash
# 构建 berth 的沙箱镜像（rootfs）
#
# 用法：
#   sudo scripts/setup_rootfs.sh                 # 基础镜像（含 python3）→ /opt/berth/rootfs
#   sudo scripts/setup_rootfs.sh --with-redis    # 顺带构建 redis 镜像 → /opt/berth/rootfs-redis
#
# 可覆盖的环境变量：
#   PREFIX=/opt/berth     安装前缀（镜像放在 $PREFIX/rootfs 和 $PREFIX/rootfs-redis）
#   SUITE=bookworm        Debian 发行版代号（Ubuntu 可传 jammy/noble 等）
#   MIRROR=http://deb.debian.org/debian   软件源
#
# 说明：镜像是普通目录（debootstrap 产物），berth 用 overlayfs 把它当只读下层。
set -euo pipefail

PREFIX="${PREFIX:-/opt/berth}"
SUITE="${SUITE:-bookworm}"
MIRROR="${MIRROR:-http://deb.debian.org/debian}"
WITH_REDIS=0
[[ "${1:-}" == "--with-redis" ]] && WITH_REDIS=1

BASE="$PREFIX/rootfs"
REDIS="$PREFIX/rootfs-redis"

if [[ $EUID -ne 0 ]]; then
  echo "需要 root 权限：sudo $0 $*" >&2
  exit 1
fi

if ! command -v debootstrap >/dev/null; then
  echo "== 安装 debootstrap =="
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq debootstrap
fi

# ─────────────────────────── 基础镜像 ───────────────────────────
build_base() {
  if [[ -x "$BASE/usr/bin/python3" ]]; then
    echo "[skip] 基础镜像已存在: $BASE"
    return
  fi
  echo "== [1/3] debootstrap $SUITE → $BASE（首次约 3-5 分钟） =="
  rm -rf "$BASE"
  mkdir -p "$PREFIX"
  # python3：跑脚本必需；ca-certificates：沙箱里访问 https 必需；util-linux：umount/setpriv 等
  debootstrap --variant=minbase \
    --include=python3,ca-certificates,util-linux \
    "$SUITE" "$BASE" "$MIRROR"

  echo "== [2/3] 拷贝宿主 DNS 配置（沙箱内 host 网络直连，用它解析域名） =="
  cp /etc/resolv.conf "$BASE/etc/resolv.conf"

  echo "== [3/3] 清理缓存 =="
  chroot "$BASE" apt-get clean >/dev/null 2>&1 || true
  rm -rf "$BASE/var/lib/apt/lists"/* "$BASE/tmp"/* 2>/dev/null || true
  echo "   完成: $BASE（$(du -sh "$BASE" | cut -f1)）"
}

# ─────────────────────────── redis 镜像 ───────────────────────────
build_redis() {
  if [[ -x "$REDIS/usr/bin/redis-server" ]]; then
    echo "[skip] redis 镜像已存在: $REDIS"
    return
  fi
  echo "== [redis 1/2] 从基础镜像复制 → $REDIS =="
  rm -rf "$REDIS"
  cp -a "$BASE" "$REDIS"
  # 禁止 chroot 里 apt 自动启动服务
  printf '#!/bin/sh\nexit 101\n' > "$REDIS/usr/sbin/policy-rc.d"
  chmod +x "$REDIS/usr/sbin/policy-rc.d"
  cp /etc/resolv.conf "$REDIS/etc/resolv.conf"

  echo "== [redis 2/2] chroot 安装 redis-server + redis-tools =="
  DEBIAN_FRONTEND=noninteractive chroot "$REDIS" apt-get update -qq
  DEBIAN_FRONTEND=noninteractive chroot "$REDIS" apt-get install -y -qq --no-install-recommends \
    redis-server redis-tools
  chroot "$REDIS" apt-get clean >/dev/null 2>&1 || true
  rm -rf "$REDIS/var/lib/apt/lists"/* 2>/dev/null || true
  echo "   完成: $REDIS（$(chroot "$REDIS" /usr/bin/redis-server --version | head -1)）"
}

build_base
[[ $WITH_REDIS -eq 1 ]] && build_redis

echo
echo "全部完成。镜像位置："
echo "  基础镜像: $BASE"
[[ $WITH_REDIS -eq 1 ]] && echo "  redis   : $REDIS"
echo
echo "下一步：uvicorn server:app --host 0.0.0.0 --port 9000"
