#!/bin/bash
# berth 管理员工具：一条龙配置 / 状态 / 签发令牌 / TLS / 轮换 / 吊销 / 日志
#
# 在**跑 berth 的那台机器**上执行（VM 里）：
#
#   sudo scripts/berth_admin.sh setup                 # 一条龙：配置 + 开机自启 + 自检
#   sudo scripts/berth_admin.sh status                # 看状态
#   sudo scripts/berth_admin.sh token                 # 打印超级管理员令牌
#   sudo scripts/berth_admin.sh tls                   # 生成自签证书并启用 HTTPS
#   sudo scripts/berth_admin.sh sign --name shakedown --user shop --ttl 3600 --out ~/.berth/token
#   sudo scripts/berth_admin.sh list                  # 列出令牌
#   sudo scripts/berth_admin.sh revoke tk-xxxx
#   sudo scripts/berth_admin.sh users                 # 列出 user（租户/项目）
#   sudo scripts/berth_admin.sh adduser acme --quota-runs 10
#   sudo scripts/berth_admin.sh share mysql           # 把服务设为"全局共享"（人人可见、只读）
#   sudo scripts/berth_admin.sh rotate                # 轮换超级管理员令牌（旧的保留 5 分钟）
#   sudo scripts/berth_admin.sh logs                  # 看 daemon 日志
#
# setup 会做这些事（都幂等）：
#   1. /etc/berth.env      —— 放 BERTH_TOKEN / BERTH_HOST / BERTH_PORT（权限 600）
#   2. /etc/systemd/system/berth.service —— systemd 单元（开机自启 + 崩了自动拉起）
#   3. 停掉手动 nohup 起的 daemon（避免抢端口），改由 systemd 托管
#   4. 等健康检查通过，把访问方式和"怎么给 agent 发令牌"打印出来
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SVC="${BERTH_SERVICE:-berth}"
ENV_FILE="${BERTH_ENV_FILE:-/etc/berth.env}"
UNIT_FILE="/etc/systemd/system/${SVC}.service"
TLS_DIR="${BERTH_TLS_DIR:-/etc/berth}"
TLS_CRT="$TLS_DIR/tls.crt"
TLS_KEY="$TLS_DIR/tls.key"
TOKEN_FILE="${BERTH_TOKEN_FILE:-/var/lib/berth/token}"
BERTH_HOST="${BERTH_HOST:-127.0.0.1}"
BERTH_PORT="${BERTH_PORT:-9000}"
# 注意：sudo 下 `id -un` 是 root，要用 SUDO_USER（真正的主人）
# —— venv 常常建在某个用户下（依赖在 ~/.local 里），用错用户会 import 到系统的老版本
RUN_USER="${BERTH_USER:-${SUDO_USER:-$(id -un)}}"
PY="${BERTH_PYTHON:-$REPO_DIR/.venv/bin/python}"

say()  { echo; echo "── $* ──"; }
info() { echo "   $*"; }
die()  { echo "错误: $*" >&2; exit 1; }

need_root() {
  [[ $EUID -eq 0 ]] || die "需要 root：sudo $0 $*"
}

read_token() {
  if [[ -f "$ENV_FILE" ]]; then
    grep -E '^BERTH_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-
  elif [[ -f "$TOKEN_FILE" ]]; then
    cat "$TOKEN_FILE"
  fi
}

# ─────────────────────────── unit ───────────────────────────

write_unit() {
  local tls_args=""
  if [[ -f "$TLS_CRT" && -f "$TLS_KEY" ]]; then
    tls_args="--ssl-certfile $TLS_CRT --ssl-keyfile $TLS_KEY"
  fi
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=berth sandbox daemon
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PY -m uvicorn server:app --host \$BERTH_HOST --port \$BERTH_PORT $tls_args
Restart=on-failure
RestartSec=2
TimeoutStopSec=30
# 管理用：berth 自己要调 systemd-run / chown（免密 sudo）
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOF
  chmod 644 "$UNIT_FILE"
  systemctl daemon-reload
}

# ─────────────────────────── setup ───────────────────────────

cmd_setup() {
  need_root setup

  say "1/5 准备 /etc/berth.env（token 不会凭空消失，已有就保留）"
  local token=""
  if [[ -f "$ENV_FILE" ]]; then
    token=$(grep -E '^BERTH_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)
    info "已有 env 文件，沿用里面的 token"
  fi
  if [[ -z "$token" ]]; then
    token="bt_$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
    info "生成新的超级管理员令牌"
  fi
  cat > "$ENV_FILE" <<EOF
# berth 运行配置（超级管理员凭证；别给 agent 这份文件）
BERTH_TOKEN=$token
BERTH_HOST=$BERTH_HOST
BERTH_PORT=$BERTH_PORT
BERTH_HOME=/var/lib/berth
BERTH_TOKEN_FILE=$TOKEN_FILE
EOF
  chmod 600 "$ENV_FILE"
  chown root:root "$ENV_FILE" 2>/dev/null || true
  info "$ENV_FILE（600 root:root）"

  say "1b/5 自检：$RUN_USER 能不能用这个 Python 跑起来"
  if ! su -s /bin/bash "$RUN_USER" -c "$PY -c 'import fastapi, uvicorn, pydantic; assert pydantic.VERSION.startswith(\"2\"), pydantic.VERSION'" 2>/dev/null; then
    die "$RUN_USER 用不了 $PY（依赖装在别处了？比如 venv 是 --system-site-packages 建的，依赖在 ~/.local）。
       换用户：BERTH_USER=<用户> sudo $0 setup
       或先修 venv：sudo -u $RUN_USER $PY -m pip install -r requirements.txt"
  fi
  info "OK"

  say "2/5 写 systemd 单元（开机自启 + 崩了自动拉起）"
  write_unit
  info "$UNIT_FILE"

  say "3/5 停掉手动起的 daemon（免得抢端口）"
  pkill -f "[u]vicorn server:app" 2>/dev/null && info "已停掉 nohup 起的 daemon" || info "没有手动起的 daemon"

  say "4/5 启动（systemd 托管）"
  systemctl enable "$SVC" >/dev/null 2>&1 || true
  systemctl restart "$SVC"
  sleep 2
  local scheme="http"
  [[ -f "$TLS_CRT" ]] && scheme="https"
  for _ in $(seq 1 30); do
    if curl -sfk -m 2 "$scheme://127.0.0.1:$BERTH_PORT/health" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  if ! curl -sfk -m 2 "$scheme://127.0.0.1:$BERTH_PORT/health" >/dev/null 2>&1; then
    echo "   健康检查没过，日志：" >&2
    journalctl -u "$SVC" -n 30 --no-pager >&2
    exit 1
  fi
  systemctl is-active "$SVC" | sed 's/^/   unit: /'

  say "5/5 收尾：把状态库权限收紧（只有运行用户能读写）"
  chmod 600 /var/lib/berth/daemon.db 2>/dev/null || true
  info "daemon.db 600"

  say "完成"
  info "API      : $scheme://$BERTH_HOST:$BERTH_PORT"
  info "管理界面 : $scheme://$BERTH_HOST:$BERTH_PORT/ui/"
  info "超级管理员令牌（自己留着；给 agent 用下面的 sign 签受限的）:"
  info "  $token"
  echo
  info "给 agent / Shakedown 发一个受限令牌（绑定到它的项目）："
  info "  sudo $0 sign --name <agent> --user <项目名> --ttl 3600 --out <agent 自己的目录>/.berth/token"
  info "公网暴露的话再跑一次：sudo $0 tls   （生成自签证书并启用 HTTPS）"
}

# ─────────────────────────── TLS ───────────────────────────

cmd_tls() {
  need_root tls
  command -v openssl >/dev/null || die "需要 openssl"

  say "1/3 生成自签证书"
  mkdir -p "$TLS_DIR"
  chmod 700 "$TLS_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$TLS_KEY" -out "$TLS_CRT" -days 3650 \
    -subj "/CN=berth" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
  chmod 600 "$TLS_KEY"
  chmod 644 "$TLS_CRT"
  info "$TLS_CRT / $TLS_KEY"

  say "2/3 写进 systemd 单元并重启"
  write_unit
  systemctl restart "$SVC"
  sleep 3

  say "3/3 自检"
  if curl -sfk -m 3 "https://127.0.0.1:$BERTH_PORT/health" >/dev/null 2>&1; then
    info "HTTPS 正常：https://127.0.0.1:$BERTH_PORT"
  else
    info "（没验过：可能端口或 host 改了）"
  fi

  echo
  info "把这段 CA 给客户端（客户端用 --ca / BERTH_CA_FILE 校验）："
  cat "$TLS_CRT"
  echo
  info "客户端用法："
  info "  curl --cacert $TLS_CRT -H 'Authorization: Bearer <token>' https://<berth>:$BERTH_PORT/system/info"
  info "  python3 scripts/berth_client.py --ca $TLS_CRT get /system/info"
}

# ─────────────────────────── status ───────────────────────────

cmd_status() {
  say "daemon"
  systemctl is-active "$SVC" 2>/dev/null | sed 's/^/   unit: /' || echo "   unit: 未注册（还没跑 setup？）"
  if [[ -f "$ENV_FILE" ]]; then
    info "env 文件: $ENV_FILE"
    grep -E '^BERTH_(HOST|PORT)=' "$ENV_FILE" | sed 's/^/   /'
  fi
  if [[ -f "$TOKEN_FILE" ]]; then
    info "本机令牌文件: $TOKEN_FILE（$(stat -c '%a %U:%G' "$TOKEN_FILE" 2>/dev/null || echo '?')）"
  fi
  if [[ -f "$TLS_CRT" ]]; then
    info "TLS: 已启用（$TLS_CRT）"
  else
    info "TLS: 未启用（公网暴露建议跑 $0 tls）"
  fi

  say "健康"
  local scheme="http"
  [[ -f "$TLS_CRT" ]] && scheme="https"
  curl -sfk -m 3 "$scheme://127.0.0.1:$BERTH_PORT/health" 2>/dev/null | sed 's/^/   /' || echo "   连不上（$scheme://127.0.0.1:$BERTH_PORT）"

  say "服务"
  local token
  token=$(read_token 2>/dev/null || true)
  if [[ -n "$token" ]]; then
    curl -sfk -m 5 -H "Authorization: Bearer $token" "$scheme://127.0.0.1:$BERTH_PORT/services" 2>/dev/null \
      | python3 -c '
import json,sys
try:
    rows = json.load(sys.stdin)
except Exception:
    print("   （拿不到）"); raise SystemExit
if not rows:
    print("   （没有活跃服务）")
for s in rows:
    print("   %-18s %-10s %-8s %s:%s  owner=%s" % (s.get("name"), s.get("template"), s.get("status"),
                                               s.get("host"), s.get("port"), s.get("owner")))
'
  fi

  say "user（租户/项目）"
  "$PY" "$REPO_DIR/scripts/berth_token.py" users 2>/dev/null | sed 's/^/   /' || true
}

# ─────────────────────────── 令牌 / user ───────────────────────────

cmd_token() {
  local token
  token=$(read_token)
  [[ -n "$token" ]] || die "找不到 token（先跑 setup）"
  echo "$token"
}

cmd_sign()    { "$PY" "$REPO_DIR/scripts/berth_token.py" create "$@"; }
cmd_list()    { "$PY" "$REPO_DIR/scripts/berth_token.py" list "$@"; }
cmd_revoke()  { "$PY" "$REPO_DIR/scripts/berth_token.py" revoke "$@"; }
cmd_bootstrap() { "$PY" "$REPO_DIR/scripts/berth_token.py" bootstrap "$@"; }
cmd_users()   { "$PY" "$REPO_DIR/scripts/berth_token.py" users "$@"; }
cmd_adduser() { "$PY" "$REPO_DIR/scripts/berth_token.py" adduser "$@"; }

# ─────────────────────────── 共享资源 ───────────────────────────

# 把服务设为"全局共享"（owner 为空）：所有 user 可见、只读
# 或者 --owner <user> 把它划给某个 user
cmd_share() {
  local name="${1:?用法: berth_admin.sh share <服务名> [--owner <user>]}"
  shift || true
  local owner=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --owner) owner="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  local token
  token=$(read_token) || die "找不到超级管理员令牌"
  local scheme="http"
  [[ -f "$TLS_CRT" ]] && scheme="https"
  local body
  body=$(python3 -c "import json,sys; print(json.dumps({'owner': sys.argv[1]}))" "$owner")
  echo "把 $name 的归属设为 ${owner:-（全局共享）}…"
  curl -sfk -X PUT "$scheme://127.0.0.1:$BERTH_PORT/services/$name" \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -d "$body" | python3 -c '
import json,sys
d = json.load(sys.stdin)
print("   ", d.get("name"), "owner =", d.get("owner"), "(空 = 全局共享：人人可见、只读)")
' || die "设置失败（服务不存在？）"
}

# ─────────────────────────── rotate ───────────────────────────

cmd_rotate() {
  need_root rotate
  [[ -f "$ENV_FILE" ]] || die "没有 $ENV_FILE，先跑 setup"
  local old new
  old=$(read_token)
  new="bt_$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
  # 旧的先留着（5 分钟内两个都认，方便无缝切换）
  python3 - "$ENV_FILE" "$old" "$new" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).read().splitlines()
out = []
for line in lines:
    if line.startswith("BERTH_TOKEN="):
        out.append(f"BERTH_TOKEN={old},{new}")
    else:
        out.append(line)
open(path, "w").write("\n".join(out) + "\n")
PY
  chmod 600 "$ENV_FILE"
  systemctl restart "$SVC"
  sleep 2
  say "已轮换"
  info "新 token: $new"
  info "旧 token 在 5 分钟内仍然有效（方便调用方切换）"
  info "确认调用方都换成新的后，跑这个把旧的摘掉："
  info "  sudo sed -i 's/^BERTH_TOKEN=.*/BERTH_TOKEN=$new/' $ENV_FILE && sudo systemctl restart $SVC"
}

# ─────────────────────────── logs ───────────────────────────

cmd_logs() {
  journalctl -u "$SVC" -f -n 50 --no-pager
}

usage() {
  sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

case "${1:-}" in
  setup) shift; cmd_setup "$@" ;;
  status) shift; cmd_status "$@" ;;
  token) shift; cmd_token "$@" ;;
  tls) shift; cmd_tls "$@" ;;
  sign) shift; cmd_sign "$@" ;;
  list) shift; cmd_list "$@" ;;
  revoke) shift; cmd_revoke "$@" ;;
  bootstrap) shift; cmd_bootstrap "$@" ;;
  users) shift; cmd_users "$@" ;;
  adduser) shift; cmd_adduser "$@" ;;
  share) shift; cmd_share "$@" ;;
  rotate) shift; cmd_rotate "$@" ;;
  logs) shift; cmd_logs "$@" ;;
  -h|--help|help|"") usage 0 ;;
  *) echo "未知命令: $1" >&2; usage 1 ;;
esac
