#!/bin/bash
# smoke_langgraph：LangGraph agent 断点恢复（kill-and-resume）
#
# 注意：agent_langgraph.py 是 agent 侧脚本，不在 berth 仓库里。
# 默认在仓库上一级目录找它，也可以用 AGENT_DIR 指定：
#   AGENT_DIR=/path/to/agent-dir berth/scripts/smoke_langgraph.sh
#
# 依赖：berth daemon 已在 :9000 运行；agent 侧已装 langgraph（如 berth/.venv）。
set -e

BERTH_DIR=$(cd "$(dirname "$0")/.." && pwd)
AGENT_DIR="${AGENT_DIR:-$(cd "$BERTH_DIR/.." && pwd)}"

if [[ ! -f "$AGENT_DIR/agent_langgraph.py" ]]; then
  echo "找不到 $AGENT_DIR/agent_langgraph.py" >&2
  echo "这是 agent 侧脚本（不在 berth 仓库内）；请设置 AGENT_DIR 或把它放到 $BERTH_DIR/.." >&2
  exit 1
fi

if [[ -x "$BERTH_DIR/.venv/bin/python" ]]; then
  PY="$BERTH_DIR/.venv/bin/python"
else
  PY=$(command -v python3)
fi

cd "$AGENT_DIR"
TASK="在 /workspace 里创建 3 个文件 a.txt b.txt c.txt，每个写入一个 1-100 的随机整数，最后打印文件列表和内容。"

echo "── 0. 清空该 thread 的 workspace 和 checkpoint ──"
curl -s -X POST http://localhost:9000/scripts/lg-smoke/clear > /dev/null
"$PY" - <<'EOF' 2>/dev/null
import sqlite3
conn = sqlite3.connect("langgraph_state.db")
conn.execute("DELETE FROM checkpoints WHERE thread_id='lg-smoke'")
conn.execute("DELETE FROM writes WHERE thread_id='lg-smoke'")
conn.commit(); conn.close()
print("  cleaned")
EOF

echo
echo "── 1. 第一次运行：planner 之后进程被 kill（模拟） ──"
LG_CRASH_AFTER=-1 "$PY" -u agent_langgraph.py lg-smoke "$TASK" 2>&1 | grep -E "planner|executor|崩溃" || true
echo "  （进程已崩溃，checkpoint 留在 langgraph_state.db）"

echo
echo "── 2. 重新执行同一条命令 → 从断点恢复 ──"
"$PY" -u agent_langgraph.py lg-smoke 2>&1 | grep -vE "^\||WARNING|Phoenix|SpanProcessor|Transport|register|Using|disable|^$" | tail -20

echo
echo "Smoke 完成 ✔"
