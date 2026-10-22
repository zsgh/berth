#!/usr/bin/env python3
"""管理 berth 的访问令牌（签发 / 列出 / 吊销）和 user（租户/项目）。

令牌明文**只在签发时显示一次**（库里只存 sha256）—— 所以签发走这个 CLI，
而不是让调用方从 API 里"再查一次"。

用法：
    # 签发一个绑定到 user 的令牌（user 不存在会自动建）
    python3 scripts/berth_token.py create --name shakedown --user shop
    python3 scripts/berth_token.py create --name w-1 --user shop --ttl 3600 --out ~/.berth/token

    # 列出（不显示明文）
    python3 scripts/berth_token.py list

    # 吊销
    python3 scripts/berth_token.py revoke tk-xxxxxxxxxx

    # user（租户/项目）
    python3 scripts/berth_token.py users
    python3 scripts/berth_token.py adduser acme --quota-runs 10

    # 一次性引导码（跨机接入用）
    python3 scripts/berth_token.py bootstrap

    # 我是谁
    python3 scripts/berth_token.py whoami

DB 路径：BERTH_DB 或 $BERTH_HOME/daemon.db（默认 /var/lib/berth/daemon.db）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _write_out(path: str | None, text: str) -> None:
    """把明文 token 写到指定文件（0600）。"""
    if not path:
        return
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    print(f"  已写入: {p}（0600）")


def main() -> int:
    parser = argparse.ArgumentParser(description="berth 访问令牌 / user 管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", aliases=["sign"], help="签发新令牌")
    p_create.add_argument("--name", required=True, help="令牌名（审计里显示）")
    p_create.add_argument("--user", required=True, help="令牌归属的 user（不存在会自动建）")
    p_create.add_argument(
        "--ttl", type=int, default=0, help="有效期（秒），0 = 永不过期（动态 worker 用 3600）"
    )
    p_create.add_argument("--out", default=None, help="把明文 token 写到这个文件（0600）")

    sub.add_parser("list", help="列出令牌")

    p_show = sub.add_parser("show", help="看某个令牌")
    p_show.add_argument("token_id")

    p_revoke = sub.add_parser("revoke", help="吊销令牌")
    p_revoke.add_argument("token_id")

    p_boot = sub.add_parser("bootstrap", help="生成/查看一次性引导码")
    p_boot.add_argument("--show", action="store_true", help="只显示当前的，不重新生成")

    sub.add_parser("whoami", help="看当前 token 的身份（调 /system/info）")
    sub.add_parser("users", help="列出 user（租户/项目）")

    p_adduser = sub.add_parser("adduser", help="建一个 user")
    p_adduser.add_argument("name")
    p_adduser.add_argument("--quota-runs", type=int, default=0)
    p_adduser.add_argument("--quota-memory-mb", type=int, default=0)
    p_adduser.add_argument("--quota-services", type=int, default=0)
    p_adduser.add_argument("--notes", default=None)

    args = parser.parse_args()

    from state.db import init_db

    init_db()

    if args.cmd in ("create", "sign"):
        from sandbox.tokens import create_token

        try:
            record, raw = create_token(args.name, args.user, args.ttl)
        except ValueError as e:
            print(f"错误: {e}", file=sys.stderr)
            return 1
        print("令牌已签发（明文只显示这一次，请立刻保存）")
        print()
        print(f"  id    : {record['id']}")
        print(f"  name  : {record['name']}")
        print(f"  user  : {record['user']}")
        print(f"  ttl   : {record['ttl'] or '永不过期'}"
              + (f"（到期 {record['expires_at']}）" if record.get("expires_at") else ""))
        print(f"  token : {raw}")
        _write_out(args.out, raw)
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
        print(f"{'id':<16}{'name':<20}{'user':<16}{'到期':<22}最近使用")
        for r in rows:
            expires = r.get("expires_at") or "永不过期"
            state = "（已吊销）" if r.get("revoked_at") else ""
            print(
                f"{r['id']:<16}{r.get('name', ''):<20}{r.get('user') or '-':<16}"
                f"{expires:<22}{r.get('last_used_at') or '-'}{state}"
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

    if args.cmd == "revoke":
        from sandbox.tokens import revoke_token

        record = revoke_token(args.token_id)
        if not record:
            print("没有这个令牌", file=sys.stderr)
            return 1
        print(f"已吊销: {record['id']}（{record.get('name')}）")
        return 0

    if args.cmd == "bootstrap":
        from sandbox.tokens import ensure_bootstrap_code

        path, code = ensure_bootstrap_code()
        if not path:
            print("未配置 BERTH_TOKEN_FILE（没有引导码这个概念）", file=sys.stderr)
            return 1
        print(f"引导码文件: {path}（0600）")
        print(f"引导码    : {code}")
        print()
        print("跨机接入（换一个受限令牌，不用给超级管理员凭证）：")
        print(f"""  curl -X POST http://<berth>:9000/tokens/bootstrap \\\\
    -H 'Content-Type: application/json' \\\\
    -d '{{"code": "{code}", "name": "shakedown", "user": "<项目名>", "ttl": 3600}}'""")
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

    if args.cmd == "users":
        from state.db import list_users, user_resource_counts

        rows = list_users()
        if not rows:
            print("（还没有 user）")
            return 0
        print(f"{'name':<20}{'资源(服务/执行)':<20}{'配额(任务/内存MB/服务)':<28}创建时间")
        for u in rows:
            c = user_resource_counts(u["name"])
            quota = f"{u.get('quota_runs') or '∞'}/{u.get('quota_memory_mb') or '∞'}/{u.get('quota_services') or '∞'}"
            print(
                f"{u['name']:<20}{str(c['services']) + '/' + str(c['runs']):<20}"
                f"{quota:<28}{u.get('created_at')}"
            )
        return 0

    if args.cmd == "adduser":
        from state.db import ensure_user, update_user

        row = ensure_user(args.name, args.notes)
        row = update_user(
            args.name,
            quota_runs=args.quota_runs,
            quota_memory_mb=args.quota_memory_mb,
            quota_services=args.quota_services,
        ) or row
        print(f"user 就绪: {row['name']}（{row['id']}）")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
