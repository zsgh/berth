#!/usr/bin/env python3
"""回填老执行记录里缺失的元信息（升级到 0.3.0 之后用一次）。

背景：`runs.image` / `runs.network` / `runs.labels` 这几列是 0.3.0 才加的，
通过 `ALTER TABLE ADD COLUMN` 迁移过来的 —— **老记录在这几列上是 NULL**
（老版本根本没记这些字段），所以 UI 的「镜像」列看着是空的。

这里按保守的启发式回填：

- `image`：默认 `base`（旧版 `run_script` 的默认值）；如果记录的 code/argv 里
  出现了某个已���镜像的名字（比如 `/usr/bin/opencode`），就填那个镜像名
- `network`：旧版没有网络策略的概念，一律是 host 网络 → 填 `{"mode": "host"}`
- `labels`：老记录没有标签，保持 `{}`（不动）

用法：
    python3 scripts/backfill_run_meta.py              # 干跑，只报告
    python3 scripts/backfill_run_meta.py --apply      # 真写
    BERTH_DB=/var/lib/berth/daemon.db python3 scripts/backfill_run_meta.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="回填老执行记录的 image / network")
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只报告）")
    args = parser.parse_args()

    from sandbox.images import available_images
    from state.db import connect, init_db, list_runs

    init_db()
    images = available_images()
    # 镜像名（去掉 base）→ 关键词
    keywords = {name: name for name in images if name != "base"}

    rows = list_runs(limit=100000)
    todo = [r for r in rows if not r.get("image")]
    print(f"执行记录共 {len(rows)} 条，其中 {len(todo)} 条缺 image")
    if not todo:
        print("没有需要回填的记录")
        return 0

    plan = []
    for row in todo:
        blob = " ".join(str(row.get(k) or "") for k in ("code", "stdout", "stderr"))
        image = "base"
        for name in keywords:
            if name in blob:
                image = name
                break
        plan.append((row["task_id"], image, row.get("created_at")))

    for task_id, image, created_at in plan:
        print(f"  {task_id:36s} → image={image:10s} ({created_at})")

    if not args.apply:
        print("\n（干跑模式，没写库；加 --apply 真写）")
        return 0

    conn = connect()
    with conn:
        for task_id, image, _ in plan:
            conn.execute(
                "UPDATE runs SET image=?, network=COALESCE(network, ?) WHERE task_id=?",
                (image, json.dumps({"mode": "host", "allow": []}), task_id),
            )
    conn.close()
    print(f"\n已回填 {len(plan)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
