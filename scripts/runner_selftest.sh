#!/bin/bash
# runner 镜像自检（B7）：在沙箱里用 Playwright 打开一个**本地页面**并截图。
#
# 用法：
#   ./scripts/runner_selftest.sh                      # 默认 http://127.0.0.1:9000
#   ./scripts/runner_selftest.sh http://host:9000 $TOKEN
#
# 前置：sudo scripts/setup_rootfs.sh --with-runner
set -euo pipefail

BASE="${1:-http://127.0.0.1:9000}"
TOKEN="${2:-${BERTH_TOKEN:-}}"

AUTH=()
[[ -n "$TOKEN" ]] && AUTH=(-H "Authorization: Bearer $TOKEN")

cat > /tmp/berth-runner-selftest.py <<'PY'
# 沙箱内跑：起一个本地 HTTP 服务 → Playwright 打开它 → 截图到 /workspace
import http.server
import os
import socketserver
import threading

PAGE = b"<html><body><h1>berth runner ok</h1><p>playwright works</p></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


httpd = socketserver.TCPServer(("127.0.0.1", 8765), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    # 沙箱里没有 user namespace 映射 → Chromium 必须 --no-sandbox
    browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page(viewport={"width": 800, "height": 400})
    page.goto("http://127.0.0.1:8765/", wait_until="load")
    title = page.title()
    page.screenshot(path="/workspace/shot.png")
    browser.close()

httpd.shutdown()
print("screenshot:", os.path.getsize("/workspace/shot.png"), "bytes")
print("OK")
PY

CODE=$(python3 - <<'PY'
import json
code = open("/tmp/berth-runner-selftest.py").read()
print(json.dumps({
    "image": "runner",
    "timeout": 180,
    "memory_mb": 1536,
    "cpu_percent": 100,
    "code": code,
}))
PY
)

echo "== 提交 runner 自检任务 =="
RESP=$(curl -sS -X POST "$BASE/scripts/run" \
  -H 'Content-Type: application/json' "${AUTH[@]}" -d "$CODE")
echo "$RESP" | python3 -m json.tool | head -40

OUT=$(echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("exit_code"), d.get("status"), (d.get("stdout") or "").strip())')
echo "== 结果: $OUT =="

TASK=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("task_id",""))')
if [[ -n "$TASK" ]]; then
  echo "== 下载截图 =="
  curl -sS "${AUTH[@]}" "$BASE/scripts/$TASK/files/shot.png" -o shot.png
  ls -l shot.png
  file shot.png 2>/dev/null || true
fi

rm -f /tmp/berth-runner-selftest.py
