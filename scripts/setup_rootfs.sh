#!/bin/bash
# 构建 berth 的沙箱镜像（rootfs）
#
# 用法：
#   sudo scripts/setup_rootfs.sh                 # 基础镜像（含 python3）→ /opt/berth/rootfs
#   sudo scripts/setup_rootfs.sh --with-redis    # 顺带构建 redis 镜像 → /opt/berth/rootfs-redis
#   sudo scripts/setup_rootfs.sh --with-mysql    # 顺带构建 MariaDB 镜像 → /opt/berth/rootfs-mysql
#   sudo scripts/setup_rootfs.sh --with-postgres # 顺带构建 PostgreSQL 镜像 → /opt/berth/rootfs-postgres
#   sudo scripts/setup_rootfs.sh --with-runner   # 顺带构建 runner 镜像 → /opt/berth/rootfs-runner
#   sudo scripts/setup_rootfs.sh --stack node,go # 按需构建被测运行时 → /opt/berth/rootfs-node …
#   sudo scripts/setup_rootfs.sh --with-redis --with-mysql --with-postgres --with-runner
#
# 可覆盖的环境变量：
#   PREFIX=/opt/berth     安装前缀（镜像放在 $PREFIX/rootfs*）
#   SUITE=bookworm        Debian 发行版代号（Ubuntu 可传 jammy/noble 等）
#   MIRROR=http://deb.debian.org/debian   软件源
#   PG_VERSION=15         PostgreSQL 主版本（bookworm 默认 15 = LTS）
#
# 说明：镜像是普通目录（debootstrap 产物），berth 用 overlayfs 把它当只读下层。
set -euo pipefail

PREFIX="${PREFIX:-/opt/berth}"
SUITE="${SUITE:-bookworm}"
MIRROR="${MIRROR:-http://deb.debian.org/debian}"
PG_VERSION="${PG_VERSION:-15}"

WITH_REDIS=0
WITH_MYSQL=0
WITH_POSTGRES=0
WITH_RUNNER=0
STACKS=""

usage() {
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

for arg in "$@"; do
  case "$arg" in
    --with-redis) WITH_REDIS=1 ;;
    --with-mysql) WITH_MYSQL=1 ;;
    --with-postgres) WITH_POSTGRES=1 ;;
    --with-runner) WITH_RUNNER=1 ;;
    --stack) shift; STACKS="${STACKS},${1:-}" ;;
    --stack=*) STACKS="${STACKS},${arg#--stack=}" ;;
    -h|--help) usage 0 ;;
    *) echo "未知参数: $arg（支持 --with-redis/--with-mysql/--with-postgres/--with-runner/--stack <逗号分隔>）" >&2; exit 1 ;;
  esac
done
STACKS="${STACKS#,}"

BASE="$PREFIX/rootfs"
REDIS="$PREFIX/rootfs-redis"
MYSQL="$PREFIX/rootfs-mysql"
POSTGRES="$PREFIX/rootfs-postgres"
RUNNER="$PREFIX/rootfs-runner"

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

# 从基础镜像复制出一个新镜像（并禁掉 chroot 里的服务自启）
clone_from_base() {
  local dest="$1"
  rm -rf "$dest"
  cp -a "$BASE" "$dest"
  printf '#!/bin/sh\nexit 101\n' > "$dest/usr/sbin/policy-rc.d"
  chmod +x "$dest/usr/sbin/policy-rc.d"
  cp /etc/resolv.conf "$dest/etc/resolv.conf"
}

# chroot 里装包
apt_in() {
  local root="$1"; shift
  DEBIAN_FRONTEND=noninteractive chroot "$root" apt-get update -qq
  DEBIAN_FRONTEND=noninteractive chroot "$root" apt-get install -y -qq --no-install-recommends "$@"
  chroot "$root" apt-get clean >/dev/null 2>&1 || true
  rm -rf "$root/var/lib/apt/lists"/* 2>/dev/null || true
}

# ─────────────────────────── redis 镜像 ───────────────────────────
build_redis() {
  if [[ -x "$REDIS/usr/bin/redis-server" ]]; then
    echo "[skip] redis 镜像已存在: $REDIS"
    return
  fi
  echo "== [redis] 从基础镜像复制 → $REDIS =="
  clone_from_base "$REDIS"
  echo "== [redis] chroot 安装 redis-server + redis-tools =="
  apt_in "$REDIS" redis-server redis-tools
  echo "   完成: $REDIS（$(chroot "$REDIS" /usr/bin/redis-server --version | head -1)）"
}

# ─────────────────────────── MariaDB（mysql 模板）镜像 ───────────────────────────
build_mysql() {
  if [[ -x "$MYSQL/usr/sbin/mariadbd" ]]; then
    echo "[skip] mysql 镜像已存在: $MYSQL"
    return
  fi
  echo "== [mysql] 从基础镜像复制 → $MYSQL =="
  clone_from_base "$MYSQL"
  echo "== [mysql] chroot 安装 mariadb-server + mariadb-client =="
  apt_in "$MYSQL" mariadb-server mariadb-client
  echo "   完成: $MYSQL（$(chroot "$MYSQL" /usr/sbin/mariadbd --version | head -1)）"
}

# ─────────────────────────── PostgreSQL 镜像 ───────────────────────────
build_postgres() {
  if [[ -x "$POSTGRES/usr/lib/postgresql/$PG_VERSION/bin/postgres" ]]; then
    echo "[skip] postgres 镜像已存在: $POSTGRES（PG $PG_VERSION）"
    return
  fi
  echo "== [postgres] 从基础镜像复制 → $POSTGRES =="
  clone_from_base "$POSTGRES"
  echo "== [postgres] chroot 安装 postgresql-$PG_VERSION + client =="
  apt_in "$POSTGRES" "postgresql-$PG_VERSION" "postgresql-client-$PG_VERSION"
  # Debian 的 postgresql-common 会建集群，这里用不到，清掉省空间
  rm -rf "$POSTGRES/var/lib/postgresql/$PG_VERSION/main" 2>/dev/null || true
  echo "   完成: $POSTGRES（$(chroot "$POSTGRES" /usr/lib/postgresql/$PG_VERSION/bin/postgres --version)）"
}

# ─────────────────────────── runner 镜像（B7） ───────────────────────────
build_runner() {
  if [[ -x "$RUNNER/usr/bin/pytest" || -x "$RUNNER/usr/local/bin/pytest" ]]; then
    echo "[skip] runner 镜像已存在: $RUNNER"
    return
  fi
  echo "== [runner] 从基础镜像复制 → $RUNNER =="
  clone_from_base "$RUNNER"
  echo "== [runner] chroot 安装 python3 / pip / pytest / curl =="
  apt_in "$RUNNER" python3 python3-pip python3-venv python3-pytest curl unzip
  echo "== [runner] 安装 httpx / pytest / playwright（构建期装好，运行期不依赖外网） =="
  chroot "$RUNNER" /bin/bash -c '
    set -e
    export PIP_DISABLE_PIP_VERSION_CHECK=1
    export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
    pip3 install --no-cache-dir --break-system-packages \
      httpx pytest pytest-playwright playwright 2>/dev/null \
      || pip3 install --no-cache-dir httpx pytest pytest-playwright playwright
  '
  echo "== [runner] 下载 Chromium（构建期一次性，运行期离线可用） =="
  chroot "$RUNNER" /bin/bash -c '
    set -e
    export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
    playwright install --with-deps chromium
    chmod -R a+rX /opt/ms-playwright
  '
  # Chromium 在没有 user namespace 的沙箱里必须 --no-sandbox；/dev/shm 由 berth 挂 1G tmpfs
  mkdir -p "$RUNNER/etc/profile.d"
  cat > "$RUNNER/etc/profile.d/berth-runner.sh" <<'EOF'
export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1
EOF
  echo "   完成: $RUNNER（$(du -sh "$RUNNER" | cut -f1)）"
  echo "   提示：Chromium 需要 --no-sandbox（沙箱内没有 user namespace 映射）"
}

# ─────────────────────────── 被测运行时（B11） ───────────────────────────
build_stack() {
  local stack="$1"
  local dest="$PREFIX/rootfs-$stack"
  case "$stack" in
    node)
      if [[ -x "$dest/usr/bin/node" ]]; then echo "[skip] node 镜像已存在"; return; fi
      clone_from_base "$dest"
      apt_in "$dest" nodejs npm
      ;;
    go)
      if [[ -x "$dest/usr/lib/go/bin/go" ]]; then echo "[skip] go 镜像已存在"; return; fi
      clone_from_base "$dest"
      apt_in "$dest" golang-go
      ;;
    java)
      if [[ -x "$dest/usr/bin/java" ]]; then echo "[skip] java 镜像已存在"; return; fi
      clone_from_base "$dest"
      apt_in "$dest" default-jdk-headless
      ;;
    *)
      echo "未知的运行时: $stack（支持 node / go / java）" >&2
      return 1
      ;;
  esac
  echo "   完成: $dest（$(du -sh "$dest" | cut -f1)）"
}

build_base
[[ $WITH_REDIS -eq 1 ]] && build_redis
[[ $WITH_MYSQL -eq 1 ]] && build_mysql
[[ $WITH_POSTGRES -eq 1 ]] && build_postgres
[[ $WITH_RUNNER -eq 1 ]] && build_runner
for stack in ${STACKS//,/ }; do
  [[ -z "$stack" ]] && continue
  build_stack "$stack"
done

echo
echo "全部完成。镜像位置："
echo "  基础镜像: $BASE"
[[ $WITH_REDIS -eq 1 ]] && echo "  redis   : $REDIS"
[[ $WITH_MYSQL -eq 1 ]] && echo "  mysql   : $MYSQL"
[[ $WITH_POSTGRES -eq 1 ]] && echo "  postgres: $POSTGRES"
[[ $WITH_RUNNER -eq 1 ]] && echo "  runner  : $RUNNER"
for stack in ${STACKS//,/ }; do
  [[ -z "$stack" ]] && continue
  echo "  $stack: $PREFIX/rootfs-$stack"
done
echo
echo "下一步：uvicorn server:app --host 127.0.0.1 --port 9000"
echo "（对外监听请显式设置 BERTH_HOST，并配置 BERTH_TOKEN）"
