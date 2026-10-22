#!/usr/bin/env python3
"""管理 berth 的访问令牌（签发 / 列出 / 吊销）。

令牌明文**只在签发时显示一次**（库里只存 sha256）—— 所以签发走这个 CLI，
而不是让调用方从 API 里"再查一次"。

用法：
    # 签发一个工作令牌（能操作所有服务/执行，但不能造镜像/签发令牌）
    python3 scripts/berth_token.py create --name shakedown

    # 签发一个只管某个项目的令牌（作用域：标签）
    python3 scripts/berth_token.py create --name shop --project shop
    # 多个作用域标签（都要匹配才算在范围内）
    python3 scripts/berth_token.py create --name shop-run \
        --scope '{"labels": {"project": "shop", "run": "42"}}'

    # 列出（不显示明文）
    python3 scripts/berth_token.py list

    # 吊销
    python3 scripts/berth_token.py revoke tk-xxxxxxxxxx

    # 看某个令牌能干什么
    python3 scripts/berth_token.py show tk-xxxxxxxxxx

DB 路径：BERTH_DB 或 $BERTH_HOME/daemon.db（默认 /var/lib/berth/daemon.db）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="berth 访问令牌管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="签发新令牌")
    p_create.add_argument("--name", required=True, help="令牌名（审计里显示）")
    p_create.add_argument(
        "--project",
        action="append",
        default=[],
        help="作用域：项目标签（可重复，如 --project shop --project run）",
    )
    p_create.add_argument(
        "--scope",
        default=None,
        help='完整作用域 JSON，如 \'{"labels": {"project": "shop"}}\'',
    )

    sub.add_parser("list", help="列出令牌")

    p_show = sub.add_parser("show", help="看某个令牌")
    p_show.add_argument("token_id")

    p_revoke = sub.add_parser("revoke", help="吊销令牌")
    p_revoke.add_argument("token_id")

    p_boot = sub.add_parser("bootstrap", help="生成/查看一次性引导码")
    p_boot.add_argument("--show", action="store_true", help="只显示当前的，不重新生成")

    sub.add_parser("whoami", help="看当前 token 的身份（调 /system/info）")

    args = parser.parse_args()

    from state.db import init_db

    init_db()

    if args.cmd == "create":
        from sandbox.tokens import create_token

        scope = None
        if args.scope:
            scope = json.loads(args.scope)
        elif args.project:
            scope = {"labels": {"project": ",".join(sorted(set(args.project)))}}
        try:
            record, raw = create_token(args.name, scope)
        except ValueError as e:
            print(f"错误: {e}", file=sys.stderr)
            return 1
        print("令牌已签发（明文只显示这一次，请立刻保存）")
        print()
        print(f"  id    : {record['id']}")
        print(f"  name  : {record['name']}")
        print(f"  scope : {json.dumps(record['scope'], ensure_ascii=False) or '（全权 admin）'}")
        print(f"  token : {raw}")
        print()
        print("用法：")
        print(f'  curl -H "Authorization: Bearer {raw}" http://127.0.0.1:9000/system/info')
        return 0

    if args.cmd == "list":
        from sandbox.tokens import list_tokens

        rows = list_tokens()
        if not rows:
            print("（还没有令牌）")
            return 0
        print(f"{'id':<16}{'name':<24}{'scope':<40}{'最近使用':<22}状态")
        for r in rows:
            scope = json.dumps(r.get("scope") or {}, ensure_ascii=False) or "（全权）"
            state = "已吊销" if r.get("revoked_at") else "有效"
            print(
                f"{r['id']:<16}{r.get('name', ''):<24}{scope:<40}"
                f"{r.get('last_used_at') or '-':<22}{state}"
            )
        return 0

    if args.cmd == "show":
        from sandbox.tokens import list_tokens

        for r in list_tokens():
            if r["id"] == args.token_id:
                print(json.dumps(r, ensure_ascii=False, indent=2))
                return 0
        print("没有这个令牌", file=sys.stderr)
        return 1

    if args.cmd == "bootstrap":
        from sandbox.tokens import ensure_bootstrap_code

        path, code = ensure_bootstrap_code()
        if not path:
            print("未配置 BERTH_TOKEN_FILE（没有引导码这个概念）", file=sys.stderr)
            return 1
        print(f"引导码文件: {path}（0600）")
        print(f"引导码    : {code}")
        print()
        print("跨机接入（换一个受限令牌，不用给 admin 凭证）：")
        print(f"""  curl -X POST http://<berth>:9000/tokens/bootstrap \\
    -H 'Content-Type: application/json' \\
    -d '{{"code": "{code}", "name": "shakedown", "scope": {{"labels": {{"project": "shop"}}}}}}'""")
        return 0

    if args.cmd == "whoami":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from berth_client import Berth

        try:
            info = Berth().get("/system/info")
        except Exception as e:
            print(f"错误: {e}", file=sys.stderr)
            return 1
        print(json.dumps(info.get("principal"), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "revoke":
        from sandbox.tokens import revoke_token

        record = revoke_token(args.token_id)
        if not record:
            print("没有这个令牌", file=sys.stderr)
            return 1
        print(f"已吊销: {record['id']}（{record.get('name')}）")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
