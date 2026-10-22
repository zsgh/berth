#!/bin/bash
# berth macOS 一键引导：在本机 Lima VM 里把 berth 装成 systemd 服务
#
# 做什么：
#   1. 检查/安装 limactl（Lima）
#   2. 创建（或复用）名为 $BERTH_VM 的 VM（不影响 default/colima 等其他实例）
#   3. 装系统依赖 → git clone 代码进 VM → 建 Python 环境 → 构建沙箱镜像
#   4. 注册 systemd 服务并启动（开机随 VM 自启）
#   5. 验证并在 Mac 上打印访问地址
#
# 用法：
#   scripts/macos-bootstrap.sh              # 一条龙（幂等，可重复执行）
#   scripts/macos-bootstrap.sh --check      # 只体检，不改动
#   scripts/macos-bootstrap.sh --status     # 看 VM / daemon / 服务状态
#   scripts/macos-bootstrap.sh --shell      # 进 VM
#   scripts/macos-bootstrap.sh --recreate   # 删掉重建（会确认）
#   scripts/macos-bootstrap.sh --destroy    # 删除 VM（会确认）
#
# 可选环境变量：
#   BERTH_VM=lima           VM 实例名
#   BERTH_REPO=<url>        代码仓库（默认 GitHub；可换内网/本地路径）
#   BERTH_VM_DIR=~/berth    VM 内代码目录
#   BERTH_PORT=9000         daemon 端口
#   BERTH_SERVICE=berth     systemd 单元名
#   BERTH_IMAGES=all|base|full  沙箱镜像范围
#                                  all  = base+redis+mysql+postgres
#                                  full = all + runner（pytest/Playwright，较慢）
#   BERTH_TOKEN=<token>     API token（VM 内 daemon 会启用鉴权；不设 = 不鉴权）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTANCE="${BERTH_VM:-lima}"
REPO="${BERTH_REPO:-https://github.com/zsgh/berth.git}"
VDIR="${BERTH_VM_DIR:-~/berth}"
PORT="${BERTH_PORT:-9000}"
SVC="${BERTH_SERVICE:-berth}"
IMAGES="${BERTH_IMAGES:-all}"
TOKEN="${BERTH_TOKEN:-}"

ACTION=bootstrap
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)    ACTION=check ;;
    --status)   ACTION=status ;;
    --shell)    ACTION=shell ;;
    --destroy)  ACTION=destroy ;;
    --recreate) ACTION=recreate ;;
    --instance) INSTANCE="${2:?--instance 需要一个名字}"; shift ;;
    --bare)     IMAGES=base ;;
    --help|-h)  ACTION=help ;;
    *) echo "未知参数: $1（--help 看用法）" >&2; exit 1 ;;
  esac
  shift
done

# ─────────────────────────── 小工具 ───────────────────────────

say()  { echo; echo "── $* ──"; }
info() { echo "   $*"; }
die()  { echo "错误: $*" >&2; exit 1; }

confirm() {  # $1 = 问题文本
  if [ -r /dev/tty ]; then
    read -r -p "$1 [y/N] " ans < /dev/tty || true
    [[ "${ans:-}" =~ ^[Yy]$ ]]
  else
    echo "$1 （非交互环境，默认否）" >&2
    return 1
  fi
}

usage() {
  cat <<'EOF'
用法: macos-bootstrap.sh [选项]

  （无选项）    创建/复用 VM → 安装 berth → 注册 systemd 服务 → 验证
  --check       只体检，不改动
  --status      查看 VM / daemon / 服务状态
  --shell       进入 VM
  --recreate    删除 VM 后重建（会确认）
  --destroy     删除 VM（会确认）
  --instance N  指定 VM 实例名（默认 lima）
  --bare        只构建基础镜像（跳过 redis/mysql）

环境变量：BERTH_VM / BERTH_REPO / BERTH_VM_DIR / BERTH_PORT / BERTH_SERVICE / BERTH_IMAGES
EOF
}

ensure_limactl() {
  command -v limactl >/dev/null && return 0
  echo "未找到 limactl（Lima 未安装）"
  command -v brew >/dev/null || die "请先安装 Lima：https://lima-vm.io （或安装 Homebrew 后 brew install lima）"
  confirm "要用 Homebrew 安装 lima 吗？" || die "已取消"
  brew install lima
}

instance_exists() { limactl list -q 2>/dev/null | grep -qx "$INSTANCE"; }
vm_ready()        { limactl shell "$INSTANCE" -- true >/dev/null 2>&1; }

# 在 VM 里执行一段 bash；VDIR/REPO/PORT/SVC/IMAGES 会以环境变量注入
lima_run() {
  limactl shell "$INSTANCE" -- env \
    VDIR="$VDIR" REPO="$REPO" PORT="$PORT" SVC="$SVC" IMAGES="$IMAGES" \
    bash -lc "$1"
}

ensure_vm() {
  if vm_ready; then info "VM '$INSTANCE' 已在运行"; return 0; fi
  if instance_exists; then
    say "启动已有 VM（$INSTANCE）"
    limactl start "$INSTANCE" --tty=false
  else
    [ -f "$SCRIPT_DIR/lima.yaml" ] || die "找不到 $SCRIPT_DIR/lima.yaml（创建 VM 需要它）"
    say "创建 VM（$INSTANCE）——首次要下载镜像，视网速几分钟"
    limactl start --name="$INSTANCE" --tty=false "$SCRIPT_DIR/lima.yaml"
  fi
  say "等待 VM 就绪"
  for _ in $(seq 1 90); do vm_ready && break; sleep 2; done
  vm_ready || die "VM 没起来；检查：limactl list / limactl start $INSTANCE"
  info "VM 就绪"
}

# ─────────────────────────── 安装步骤（幂等） ───────────────────────────

provision_deps() {
  say "1/5 系统依赖（git / python3-venv / debootstrap / redis-tools）"
  lima_run '
set -e
if ! dpkg -s git python3-venv debootstrap redis-tools ca-certificates >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    git python3-venv debootstrap redis-tools ca-certificates
fi
echo "   deps ok"
'
}

provision_code() {
  say "2/5 获取代码（$REPO → $VDIR）"
  lima_run '
set -e
VDIR="${VDIR/#\~/$HOME}"
if [ -d "$VDIR/.git" ]; then
  git -C "$VDIR" pull --ff-only || echo "   （pull 跳过：有本地改动）"
else
  git clone "$REPO" "$VDIR"
fi
echo "   code ok"
'
}

provision_venv() {
  say "3/5 Python 环境（venv + 依赖）"
  lima_run '
set -e
VDIR="${VDIR/#\~/$HOME}"
cd "$VDIR"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "   venv ok"
'
}

provision_images() {
  say "4/5 沙箱镜像（debootstrap；首次约 5-15 分钟）"
  lima_run '
set -e
VDIR="${VDIR/#\~/$HOME}"
if [ "$IMAGES" = full ]; then
  FLAGS="--with-redis --with-mysql --with-postgres --with-runner"
elif [ "$IMAGES" = all ]; then
  FLAGS="--with-redis --with-mysql --with-postgres"
else
  FLAGS=""
fi
sudo "$VDIR/scripts/setup_rootfs.sh" $FLAGS
'
}

provision_unit() {
  say "5/5 注册 systemd 服务（$SVC.service）"
  lima_run '
set -e
VDIR="${VDIR/#\~/$HOME}"
sudo tee /etc/systemd/system/"$SVC".service >/dev/null <<UNIT
[Unit]
Description=berth sandbox daemon
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$VDIR
ExecStart=$VDIR/.venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port $PORT
Environment=BERTH_TOKEN=$TOKEN
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable "$SVC" >/dev/null 2>&1
sudo systemctl restart "$SVC"
echo "   unit ok: $SVC.service"
'
}

provision() {
  provision_deps
  provision_code
  provision_venv
  provision_images
  provision_unit
}

verify() {
  say "验证"
  local ok=0
  for _ in $(seq 1 20); do
    if lima_run 'curl -sf "http://localhost:$PORT/health" >/dev/null'; then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" != 1 ]; then
    die "daemon 健康检查失败：limactl shell $INSTANCE -- journalctl -u $SVC -n 50"
  fi
  info "VM 内 daemon: ok"
  if curl -sf -m 3 "http://localhost:$PORT/health" >/dev/null 2>&1; then
    info "Mac 上 http://localhost:$PORT 已可达（Lima 端口转发生效）"
  else
    info "Mac 上还没转发过来：确认 VM 在跑（limactl list）；必要时手动隧道："
    info "  ssh -N -L $PORT:localhost:$PORT lima@localhost -p <VM 的 ssh 端口>（见 limactl list）"
  fi
}

summary() {
  cat <<EOF

════════════════════════════════════════════
 berth 就绪 ✔（VM 实例: $INSTANCE）

 Mac 上直接用：
   curl http://localhost:$PORT/health
   curl -X POST http://localhost:$PORT/scripts/run \\
        -H 'Content-Type: application/json' \\
        -d '{"code": "print(\"hello\")"}'
   curl -X POST http://localhost:$PORT/services \\
        -H 'Content-Type: application/json' -d '{"template": "redis"}'

 管理：
   $0 --status            看状态
   $0 --shell             进 VM
   limactl stop $INSTANCE 关 VM（daemon 随 VM 停机）
   limactl start $INSTANCE 再开（berth 自动随 VM 启动）

 更新代码：
   $0                     （幂等重跑：pull + 重启服务）
════════════════════════════════════════════
EOF
}

# ─────────────────────────── 其他动作 ───────────────────────────

do_status() {
  ensure_limactl
  say "Lima 实例"
  limactl list || true
  vm_ready || { info "（VM 未运行）"; return 0; }
  say "systemd 服务"
  lima_run 'systemctl --no-pager status "$SVC" 2>&1 | head -12' || true
  say "API"
  lima_run 'curl -s "http://localhost:$PORT/health"; echo; curl -s ${TOKEN:+-H "Authorization: Bearer $TOKEN"} "http://localhost:$PORT/services"' || true
  echo
}

do_check() {
  echo "== 本地（Mac）=="
  command -v limactl >/dev/null && echo "✔ limactl $(limactl --version 2>/dev/null | head -1)" || echo "✘ limactl 未安装"
  instance_exists && echo "✔ VM 实例 '$INSTANCE' 存在" || echo "✘ VM 实例 '$INSTANCE' 不存在（跑一次 $0 创建）"
  if vm_ready; then
    echo "✔ VM 可访问"
    echo "== VM 内部 =="
    lima_run '
VDIR="${VDIR/#\~/$HOME}"
[ -d "$VDIR/.git" ] && echo "✔ 代码 $VDIR" || echo "✘ 代码缺失 $VDIR"
[ -x "$VDIR/.venv/bin/python" ] && echo "✔ venv" || echo "✘ venv 缺失"
[ -d /opt/berth/rootfs/usr ] && echo "✔ 基础镜像" || echo "✘ 基础镜像缺失"
[ -d /opt/berth/rootfs-redis ] && echo "✔ redis 镜像" || echo "△ redis 镜像未构建"
[ -d /opt/berth/rootfs-mysql ] && echo "✔ mysql 镜像" || echo "△ mysql 镜像未构建"
systemctl is-enabled "$SVC" >/dev/null 2>&1 && echo "✔ 服务已 enable" || echo "✘ 服务未注册"
systemctl is-active  "$SVC" >/dev/null 2>&1 && echo "✔ daemon 在跑" || echo "✘ daemon 没跑"
curl -sf "http://localhost:$PORT/health" >/dev/null && echo "✔ 健康检查" || echo "✘ 健康检查失败"
' || true
  else
    echo "△ VM 未运行"
  fi
  echo "== Mac 侧端口转发 =="
  curl -sf -m 3 "http://localhost:$PORT/health" >/dev/null 2>&1 && echo "✔ localhost:$PORT 可达" || echo "△ localhost:$PORT 暂不可达"
}

do_destroy() {
  ensure_limactl
  instance_exists || die "实例 '$INSTANCE' 不存在"
  confirm "删除 VM '$INSTANCE'？berth 的数据（含数据库）会一起删掉！" || die "已取消"
  limactl delete -f "$INSTANCE"
  echo "已删除 '$INSTANCE'"
}

case "$ACTION" in
  help) usage; exit 0 ;;
  shell)
    ensure_limactl
    vm_ready || ensure_vm
    exec limactl shell "$INSTANCE"
    ;;
  destroy) do_destroy ;;
  recreate)
    do_destroy
    ensure_vm
    provision
    verify
    summary
    ;;
  check)   do_check ;;
  status)  do_status ;;
  bootstrap)
    ensure_limactl
    ensure_vm
    provision
    verify
    summary
    ;;
esac
