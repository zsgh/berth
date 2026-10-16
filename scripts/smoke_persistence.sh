#!/bin/bash
# 阶段三验收：daemon 重启后状态恢复 + 孤儿清理
set -e
cd "$(dirname "$0")/.."
API=http://localhost:9000
LOG=/tmp/opencode/berth.log

start_daemon() {
  nohup python3 -m uvicorn server:app --host 0.0.0.0 --port 9000 > "$LOG" 2>&1 &
  for i in $(seq 1 30); do
    curl -sf "$API/health" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  echo "daemon 起不来"; exit 1
}

stop_daemon_kill9() {
  local pids
  pids=$(pgrep -f '^python3 -m uvicorn server:app' || true)
  [ -n "$pids" ] && kill -9 $pids
  sleep 1
}

echo "════ 0. 确保 daemon 在跑 ════"
curl -sf "$API/health" >/dev/null 2>&1 || start_daemon
echo "daemon ok"

echo "════ 1. 启动 redis 服务 ════"
RESP=$(curl -s -X PUT $API/services/persist-redis -H "Content-Type: application/json" -d '{"template": "redis"}')
SID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['service_id'])")
PORT=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['port'])")
echo "service_id=$SID port=$PORT"
redis-cli -h localhost -p $PORT ping

echo "════ 2. 制造孤儿 unit ════"
sudo systemd-run --unit=berth-svc-orphan-test --collect sleep 1000 >/dev/null 2>&1
systemctl is-active berth-svc-orphan-test.service && echo "孤儿已创建"

echo "════ 3. 杀掉 daemon（SIGKILL 模拟崩溃） ════"
stop_daemon_kill9
echo "daemon 已杀"

echo "════ 4. 服务没 daemon 也还活着 ════"
redis-cli -h localhost -p $PORT ping

echo "════ 5. 重启 daemon ════"
start_daemon

echo "════ 6. 状态恢复检查 ════"
STATUS=$(curl -s $API/services/$SID | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
echo "服务状态: $STATUS"
[ "$STATUS" = "running" ] || { echo "❌ 状态未恢复"; exit 1; }
redis-cli -h localhost -p $PORT ping

echo "════ 7. 孤儿清理检查 ════"
if systemctl status berth-svc-orphan-test.service >/dev/null 2>&1; then
  echo "❌ 孤儿 unit 还在"; systemctl status berth-svc-orphan-test.service --no-pager | head -3; exit 1
else
  echo "孤儿 unit 已被清理（not found）"
fi

echo "════ 8. 端口记录一致 ════"
curl -s $API/services | python3 -c "
import json, sys
svcs = json.load(sys.stdin)
running = [s for s in svcs if s['status'] == 'running']
print('running:', [(s['service_id'], s['port']) for s in running])
assert any(s['service_id'] == '$SID' and s['port'] == $PORT for s in running), '端口不一致'
print('OK')
"

echo "════ 9. 清理 ════"
curl -s -X DELETE "$API/services/$SID?volumes=true" >/dev/null
redis-cli -h localhost -p $PORT ping 2>&1 | head -1
echo "阶段三验收全部通过 ✔"
