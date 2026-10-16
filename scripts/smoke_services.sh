#!/bin/bash
# smoke_services：起 redis → 外部连上 → 用量/卷 → 停止 → 删除（含数据卷）
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

say "1. PUT /services/smoke-redis（ensure 语义，重复调用也只有一个）"
RESP=$(curl -s -X PUT $API/services/smoke-redis -H "Content-Type: application/json" \
  -d '{"template": "redis"}')
echo "$RESP" | python3 -m json.tool | head -20
PORT=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['port'])")

RESP2=$(curl -s -X PUT $API/services/smoke-redis -H "Content-Type: application/json" \
  -d '{"template": "redis"}')
echo "二次 ensure: $(echo "$RESP2" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['service_id'], 'reused=', d['reused'])")"

say "2. 外部连接：redis-cli -h localhost -p $PORT"
redis-cli -h localhost -p $PORT ping
redis-cli -h localhost -p $PORT set smoke_key "hello from host"
redis-cli -h localhost -p $PORT get smoke_key

say "3. 列表（含用量）"
curl -s $API/services | python3 -c "
import json,sys
for s in json.load(sys.stdin):
    u = s.get('usage') or {}
    mb = (u.get('memory_bytes') or 0) / 1048576
    disk = (u.get('disk_bytes') or 0) / 1048576
    print(f\"  {s['name']:<12} {s['host']}:{s['port']}  内存 {mb:.1f}MB / 上限 {(u.get('memory_limit_bytes') or 0)/1048576:.0f}MB  磁盘 {disk:.1f}MB\")"

say "4. 数据卷一览"
curl -s $API/volumes | python3 -m json.tool | head -14

say "5. 停止（POST /stop，数据保留）"
curl -s -X POST $API/services/smoke-redis/stop | python3 -c "import json,sys; d=json.load(sys.stdin); print('status:', d['status'])"
echo "redis-cli ping（应连接失败）:"
redis-cli -h localhost -p $PORT ping 2>&1 | head -1

say "6. 再起（POST /start，数据还在）"
curl -s -X POST $API/services/smoke-redis/start | python3 -c "import json,sys; d=json.load(sys.stdin); print('status:', d['status'], 'port:', d['port'])"
echo "之前写的 key: $(redis-cli -h localhost -p $PORT get smoke_key)"

say "7. DELETE ?volumes=true（记录 + 数据卷一起删）"
curl -s -X DELETE "$API/services/smoke-redis?volumes=true" | python3 -c "import json,sys; d=json.load(sys.stdin); print('deleted:', d['deleted'], 'volumes:', d['volumes_deleted'])"
echo "剩余卷里应没有 smoke-redis:"
curl -s $API/volumes | python3 -c "import json,sys; print([v['name'] for v in json.load(sys.stdin)])"

echo; echo "Smoke 完成 ✔"
