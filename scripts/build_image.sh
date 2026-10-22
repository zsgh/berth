#!/bin/bash
# 构建一个自定义沙箱镜像（rootfs-<name>）
#
# berth 的镜像 = 一个普通目录（debootstrap 产物），overlayfs 拿它当只读下层。
# 这个脚本从已有镜像复制一份，再在里面装东西。
#
# 用法（一般由 berth 的 API 调用，也可以手工跑）：
#   sudo scripts/build_image.sh --name myimg \
#        --source /opt/berth/rootfs \
#        --packages "jq ripgrep" \
#        --pip "httpx==0.27" \
#        --command "ln -s /opt/app /usr/local/app"
#
# 参数：
#   --name <名字>        镜像名（会变成 $PREFIX/rootfs-<名字>）
#   --source <目录>      从哪个镜像复制（默认 base）
#   --packages "a b"     apt 安装的包
#   --pip "a b"          pip 安装的包
#   --command "<shell>"  在 chroot 里执行的命令（可重复）
#   --force              目标已存在时先删掉重建
#   --no-namespace       不在 mount/pid namespace 里构建（默认在）
#
# 说明：
# - 构建过程在 mount/pid/uts/ipc namespace 里跑（unshare）：装的包起的后台进程
#   不会漏到宿主，mount 改动也不会影响宿主
# - 但构建本身是 **宿主 root 级操作**（chroot + apt 的 maintainer script 以 root 跑），
#   所以调用方必须可信；berth API 侧还有 BERTH_ALLOW_IMAGE_BUILD 开关兜底
set -euo pipefail

PREFIX="${PREFIX:-/opt/berth}"
NAME=""
SOURCE=""
PACKAGES=""
PIP_PKGS=""
FORCE=0
USE_NS=1
COMMANDS=()

usage() { sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="${2:-}"; shift 2 ;;
    --source) SOURCE="${2:-}"; shift 2 ;;
    --packages) PACKAGES="${2:-}"; shift 2 ;;
    --pip) PIP_PKGS="${2:-}"; shift 2 ;;
    --command) COMMANDS+=("${2:-}"); shift 2 ;;
    --force) FORCE=1; shift ;;
    --no-namespace) USE_NS=0; shift ;;
    -h|--help) usage 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "需要 root 权限：sudo $0 $*" >&2
  exit 1
fi

if [[ -z "$NAME" ]]; then
  echo "缺少 --name" >&2
  exit 1
fi
# 名字会变成目录名：只允许安全字符
if ! [[ "$NAME" =~ ^[a-z0-9][a-z0-9._-]{0,31}$ ]]; then
  echo "镜像名不合法: $NAME（只允许小写字母/数字/点/短横线，1-32 字符）" >&2
  exit 1
fi

SOURCE="${SOURCE:-$PREFIX/rootfs}"
if [[ ! -d "$SOURCE" ]]; then
  echo "源镜像不存在: $SOURCE" >&2
  exit 1
fi

TARGET="$PREFIX/rootfs-$NAME"
if [[ -e "$TARGET" && $FORCE -ne 1 ]]; then
  echo "镜像已存在: $TARGET（要重建加 --force）" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 构建步骤写成独立脚本，方便丢进 namespace 里跑
cat > "$WORK/body.sh" <<'INNER'
#!/bin/bash
set -euo pipefail
TARGET="$1"; PACKAGES="$2"; PIP_PKGS="$3"; COMMANDS_FILE="$4"

echo "[2/6] apt 安装: ${PACKAGES:-（无）}"
if [ -n "$PACKAGES" ]; then
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive chroot "$TARGET" apt-get update -qq
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive chroot "$TARGET" apt-get install -y -qq --no-install-recommends $PACKAGES
fi

echo "[3/6] pip 安装: ${PIP_PKGS:-（无）}"
if [ -n "$PIP_PKGS" ]; then
  # shellcheck disable=SC2086
  chroot "$TARGET" /bin/bash -c "pip3 install --no-cache-dir --break-system-packages $PIP_PKGS 2>/dev/null || pip3 install --no-cache-dir $PIP_PKGS"
fi

echo "[4/6] 执行自定义命令"
if [ -s "$COMMANDS_FILE" ]; then
  while IFS= read -r cmd; do
    [ -z "$cmd" ] && continue
    echo "    \$ $cmd"
    chroot "$TARGET" /bin/bash -c "$cmd"
  done < "$COMMANDS_FILE"
fi

echo "[5/6] 清理缓存"
chroot "$TARGET" apt-get clean >/dev/null 2>&1 || true
rm -rf "$TARGET/var/lib/apt/lists"/* "$TARGET/tmp"/* 2>/dev/null || true
INNER
chmod +x "$WORK/body.sh"

# 自定义命令一行一个，写进文件传给 namespace 里的脚本
: > "$WORK/commands.txt"
for cmd in "${COMMANDS[@]}"; do
  printf '%s\n' "$cmd" >> "$WORK/commands.txt"
done

echo "[1/6] 复制 $SOURCE → $TARGET"
rm -rf "$TARGET"
cp -a "$SOURCE" "$TARGET"

# chroot 里禁止服务自启（否则装个包就可能拉起一堆守护进程）
printf '#!/bin/sh\nexit 101\n' > "$TARGET/usr/sbin/policy-rc.d"
chmod +x "$TARGET/usr/sbin/policy-rc.d"
cp /etc/resolv.conf "$TARGET/etc/resolv.conf" 2>/dev/null || true

if [[ $USE_NS -eq 1 ]] && command -v unshare >/dev/null; then
  echo "[0/6] 在 mount/pid/uts/ipc namespace 里构建"
  unshare --mount --pid --uts --ipc --fork --kill-child \
    bash "$WORK/body.sh" "$TARGET" "$PACKAGES" "$PIP_PKGS" "$WORK/commands.txt"
else
  bash "$WORK/body.sh" "$TARGET" "$PACKAGES" "$PIP_PKGS" "$WORK/commands.txt"
fi

echo "[6/6] 完成: $TARGET（$(du -sh "$TARGET" 2>/dev/null | cut -f1)）"
echo "IMAGE_PATH=$TARGET"
