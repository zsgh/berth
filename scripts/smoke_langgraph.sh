#!/bin/bash
# smoke_langgraph：LangGraph agent 断点恢复（kill-and-resume）
# 需要在 ~/stage3 下运行（依赖 agent_langgraph.py 和 .venv）
set -e
cd ~/stage3
PY=berth/.venv/bin/python

TASK="在 /workspace 里创建 3 个文件 a.txt b.txt c.txt，每个写入一个 1-100 的随机整数，最后打印文件列表和内容。"

echo "── 0. 清空该 thread 的 workspace 和 checkpoint ──"
curl -s -X POST http://localhost:9000/scripts/lg-smoke/clear > /dev/null
$PY - <<'EOF' 2>/dev/null
import sqlite3
conn = sqlite3.connect("langgraph_state.db")
conn.execute("DELETE FROM checkpoints WHERE thread_id='lg-smoke'")
conn.execute("DELETE FROM writes WHERE thread_id='lg-smoke'")
conn.commit(); conn.close()
print("  cleaned")
EOF

echo
echo "── 1. 第一次运行：planner 之后进程被 kill（模拟） ──"
LG_CRASH_AFTER=-1 $PY -u agent_langgraph.py lg-smoke "$TASK" 2>&1 | grep -E "planner|executor|崩溃" || true
echo "  （进程已崩溃，checkpoint 留在 langgraph_state.db）"

echo
echo "── 2. 重新执行同一条命令 → 从断点恢复 ──"
$PY -u agent_langgraph.py lg-smoke 2>&1 | grep -vE "^\||WARNING|Phoenix|SpanProcessor|Transport|register|Using|disable|^$" | tail -20

echo
echo "Smoke 完成 ✔"
