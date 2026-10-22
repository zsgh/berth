#!/usr/bin/env python3
"""berth 的开箱即用客户端：自动找 token、带上 header。

给 agent / 脚本用 —— 不用自己管 token 从哪来：

    from berth_client import Berth

    b = Berth()                       # 自动读 BERTH_TOKEN / 本地令牌文件
    print(b.get("/services"))
    b.post("/scripts/run", {"code": "print(1)"})

或者直接当命令行用（等价于带 auth 头的 curl）：

    python3 scripts/berth_client.py get /services
    python3 scripts/berth_client.py post /scripts/run '{"code":"print(1)"}'
    python3 scripts/berth_client.py --url http://other:9000 get /system/info

token 的查找顺序：
    1) 显式传入的 token
    2) 环境变量 `BERTH_TOKEN`
    3) 环境变量 `BERTH_TOKEN_FILE` 指向的文件
    4) 同机默认路径：$BERTH_HOME/token、/run/berth/token、~/.berth/token
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:9000"

def _default_token_files() -> list[str]:
    """同机时可能存在的本地令牌文件（按优先级）。"""
    home = os.environ.get("BERTH_HOME") or "/var/lib/berth"
    return [
        f"{home}/token",                    # BERTH_TOKEN_FILE 常见的落点
        "/run/berth/token",
        "/var/run/berth/token",
        str(Path.home() / ".berth" / "token"),
    ]


def find_token(explicit: str | None = None) -> str | None:
    """按上面的顺序找一个可用的 token。"""
    if explicit:
        return explicit.strip()
    env = (os.environ.get("BERTH_TOKEN") or "").strip()
    if env:
        # 可能是逗号分隔的多个 → 用第一个
        return env.replace(",", " ").split()[0]
    path = (os.environ.get("BERTH_TOKEN_FILE") or "").strip()
    candidates = [path] if path else _default_token_files()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            p = Path(candidate).expanduser()
            if p.is_file():
                value = p.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            continue
    return None


class Berth:
    """极简 berth 客户端。"""

    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or os.environ.get("BERTH_URL") or DEFAULT_URL).rstrip("/")
        self.token = find_token(token)

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, path: str, body=None, timeout: float = 300):
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                ctype = resp.headers.get("content-type", "")
                if "json" in ctype:
                    return json.loads(raw or b"null")
                return raw
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"{method} {url} → {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"{method} {url} → 连不上: {e.reason}") from e

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def put(self, path: str, body=None, **kw):
        return self.request("PUT", path, body, **kw)

    def delete(self, path: str, **kw):
        return self.request("DELETE", path, **kw)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="berth 客户端（自动带 token）")
    parser.add_argument("--url", default=None, help="berth 地址（默认 $BERTH_URL 或 127.0.0.1:9000）")
    parser.add_argument("--token", default=None, help="token（默认自动找）")
    parser.add_argument("method", help="get/post/put/delete")
    parser.add_argument("path", help="接口路径，如 /services")
    parser.add_argument("body", nargs="?", default=None, help="JSON 请求体")
    args = parser.parse_args()

    client = Berth(args.url, args.token)
    body = json.loads(args.body) if args.body else None
    try:
        result = client.request(args.method, args.path, body)
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
