#!/bin/bash
# smoke_recovery：跑脚本 + kill daemon + 重启 + 状态恢复（对应交付物演示）
set -e
API=http://localhost:9000
LOG=/tmp/opencode/berth.log

say() { echo; echo "── $* ──"; }

start_daemon() {
  cd "$(dirname "$0")/.."
  setsid nohup python3 -m uvicorn server:app --host 0.0.0.0 --port 9000 > "$LOG" 2>&1 < /dev/null &
  for i in $(seq 1 30); do curl -sf "$API/health" >/dev/null 2>&1 && return 0; sleep 0.5; done
  echo "daemon 起不来"; exit 1
}

say "0. 确保 daemon 在跑"
curl -sf "$API/health" >/dev/null || start_daemon

say "1. 跑脚本：/workspace 写文件 + 起一个 redis 服务"
curl -s -X POST $API/scripts/run -H "Content-Type: application/json" \
  -d '{"code": "open(\"/workspace/hello.txt\",\"w\").write(\"hi from sandbox\"); print(\"written\")", "task_id": "smoke-script"}' | python3 -m json.tool
RESP=$(curl -s -X PUT $API/services/smoke-persist -H "Content-Type: application/json" -d '{"template": "redis"}')
SID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['service_id'])")
PORT=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['port'])")
echo "redis: $SID @ $PORT"; redis-cli -h localhost -p $PORT ping

say "2. kill -9 daemon（模拟崩溃）"
kill -9 $(pgrep -f '^python3 -m uvicorn server:app') 2>/dev/null || true
sleep 1
echo "daemon 已杀（客户端后面对 API 的请求会失败）"

say "3. 服务没 daemon 也活着：redis-cli ping"
redis-cli -h localhost -p $PORT ping

say "4. 重启 daemon"
start_daemon
sleep 1

say "5. 状态恢复：GET /services + ping"
curl -s $API/services | python3 -c "
import json,sys
for s in json.load(sys.stdin):
    if s['service_id'] == '$SID': print('  ', s['service_id'], s['status'], f\"{s['host']}:{s['port']}\")"
redis-cli -h localhost -p $PORT ping

say "6. 执行历史还在：GET /scripts/smoke-script"
curl -s $API/scripts/smoke-script | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('  exit_code:', d['exit_code'], ' artifacts:', d['artifacts'])"

say "7. workspace 也还在：再跑一次读文件"
curl -s -X POST $API/scripts/run -H "Content-Type: application/json" \
  -d '{"code": "print(open(\"/workspace/hello.txt\").read())", "task_id": "smoke-script"}' | python3 -c "import json,sys; print('  stdout:', json.load(sys.stdin)['stdout'].strip())"

say "8. 收尾"
curl -s -X DELETE "$API/services/$SID?volumes=true" >/dev/null
echo "redis 已停止"

echo; echo "Smoke 完成 ✔"
