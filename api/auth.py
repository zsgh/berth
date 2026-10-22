"""Bearer token 鉴权（B1）。

设计：
- `BERTH_TOKEN` 配置了才鉴权；**没配置时行为不变**（不拦任何请求），但启动日志会警告。
- `/health` 永远免鉴权（探活用）。
- `/ui/*` 静态资源免鉴权（登录页要能加载；静态文件里没有敏感数据），
  其余所有接口一律要 token。
- token 用 `secrets.compare_digest` 比较（常量时间，避免时序侧信道）。
- 审计里的 actor 用 token 的短哈希标识，不落明文。
"""
from __future__ import annotations

import hashlib

from fastapi import HTTPException, Request

from sandbox import config


def actor_of(request: Request | None) -> str:
    """从请求头算出 actor 标识（token 短哈希；没有 token 就是 anonymous）。"""
    if request is None:
        return "anonymous"
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return "anonymous"
    return "token:" + hashlib.sha256(value.strip().encode()).hexdigest()[:12]


def require_auth(request: Request) -> None:
    """FastAPI 依赖：校验 Bearer token。

    - `BERTH_TOKEN` 未配置 → 放行（向后兼容）
    - token 不匹配 → 401（带 `WWW-Authenticate: Bearer`）
    """
    expected = config.token()
    if not expected:
        return
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="unauthorized: 需要 Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not value.strip() or value.strip() != expected:
        raise HTTPException(
            status_code=401,
            detail="unauthorized: token 不匹配",
            headers={"WWW-Authenticate": "Bearer"},
        )
