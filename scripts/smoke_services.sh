#!/bin/bash
# smoke_services：起 redis → 外部连上 → 停止（对应交付物演示）
set -e
API=http://localhost:9000

say() { echo; echo "── $* ──"; }

say "0. 确保 daemon 在跑"
if ! curl -sf "$API/health" >/dev/null; then
  cd "$(dirname "$0")/.."
  setsid nohup python3 -m uvicorn server:app --host 0.0.0.0 --port 9000 > /tmp/opencode/berth.log 2>&1 < /dev/null &
  for i in $(seq 1 20); do curl -sf "$API/health" >/dev/null && break; sleep 0.5; done
fi
curl -s "$API/health"; echo

say "1. POST /services 启动 redis"
RESP=$(curl -s -X POST $API/services -H "Content-Type: application/json" \
  -d '{"template": "redis", "name": "smoke-redis"}')
echo "$RESP" | python3 -m json.tool
SID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['service_id'])")
PORT=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['port'])")

say "2. 外部连接：redis-cli -h localhost -p $PORT"
redis-cli -h localhost -p $PORT ping
redis-cli -h localhost -p $PORT set smoke_key "hello from host"
redis-cli -h localhost -p $PORT get smoke_key

say "3. GET /services 列表"
curl -s $API/services | python3 -c "
import json,sys
for s in json.load(sys.stdin):
    if s['status'] == 'running': print(f\"  {s['service_id']}  {s['name']}  {s['host']}:{s['port']}  {s['status']}\")"

say "4. systemd 视角"
systemctl status berth-$SID.service --no-pager 2>&1 | head -4 || true

say "5. DELETE /services/$SID 停止并清理"
curl -s -X DELETE $API/services/$SID | python3 -c "import json,sys; d=json.load(sys.stdin); print('status:', d['status'])"
echo "redis-cli ping（应连接失败）:"
redis-cli -h localhost -p $PORT ping 2>&1 | head -1
echo "systemctl status（unit 应已回收）:"
systemctl status berth-$SID.service 2>&1 | head -2 || true

echo; echo "Smoke 完成 ✔"
