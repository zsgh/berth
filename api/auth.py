"""Bearer token 鉴权 + user 归属判定。

设计：
- `BERTH_TOKEN`（环境变量 / 只读配置文件，可逗号分隔多个）→ **超级管理员**（独一份）
- SQLite `berth_tokens` 表里的 token → 绑定一个 **user**（租户/项目），可选有效期
- 库里只存 token 的 sha256；明文只在创建时返回一次
- 审计 actor：`admin:env` / `user:<name>`，不落 token 明文

授权判定 = **owner 相等**（不是标签匹配）：
- 资源 `owner = <user>` → 该 user 私有
- 资源 `owner IS NULL` → **全局共享资源**（公共 mysql/redis）：人人可见，**只读**
- 作用域外 → **404**（不泄露存在性）；可见但不可改 → **403**
"""
from __future__ import annotations

import hashlib

from fastapi import HTTPException, Request

from sandbox import config
from sandbox.tokens import (
    Principal,
    can_modify,
    can_view,
    owner_for_create,
    resolve_token,
)

# 免鉴权的路径前缀（探活 / UI 静态资源）
PUBLIC_PREFIXES = ("/health", "/ui")


def bearer_of(request: Request) -> str | None:
    """从请求头取 Bearer token。"""
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def actor_of(request: Request | None) -> str:
    """审计里的调用方标识（不落 token 明文）。"""
    if request is None:
        return "anonymous"
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return principal.actor
    raw = bearer_of(request) if request is not None else None
    if raw:
        return "token:" + hashlib.sha256(raw.encode()).hexdigest()[:12]
    return "anonymous"


def require_auth(request: Request) -> Principal:
    """FastAPI 依赖：校验 Bearer token，并把主语挂到 `request.state.principal`。

    - 完全没配置任何 token（环境变量空 + 库里没有有效令牌）→ 放行（兼容旧的本机自用模式）
    - token 无效 / 已撤销 / 已过期 → 401
    """
    raw = bearer_of(request)
    if raw:
        principal = resolve_token(raw)
        if principal is not None:
            request.state.principal = principal
            return principal
        raise HTTPException(
            status_code=401,
            detail="unauthorized: token 无效、已过期或已撤销",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 没带 token
    if config.env_tokens() or config.file_token() or _has_db_tokens():
        raise HTTPException(
            status_code=401,
            detail="unauthorized: 需要 Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # 完全没配置 token → 不鉴权（兼容旧行为），按超级管理员处理
    principal = Principal(admin=True, name="anonymous", source="none")
    request.state.principal = principal
    return principal


def _has_db_tokens() -> bool:
    """库里有没有（未撤销未过期的）令牌 —— 决定要不要鉴权。"""
    try:
        from state.db import list_token_rows
        from sandbox.tokens import _now

        now = _now()
        for t in list_token_rows():
            if t.get("revoked_at"):
                continue
            expires = t.get("expires_at")
            if expires and str(expires) <= now:
                continue
            return True
    except Exception:
        pass
    return False


def principal_of(request: Request) -> Principal:
    """取当前请求的主语（没鉴权时是超级管理员兼容模式）。"""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return Principal(admin=True, name="anonymous", source="none")
    return principal


def require_admin(request: Request) -> Principal:
    """依赖：只允许超级管理员（宿主级操作，比如造/删镜像、管 user、签发令牌）。"""
    principal = principal_of(request)
    if not principal.admin:
        raise HTTPException(
            status_code=403,
            detail=f"forbidden: 该操作需要超级管理员令牌（当前：{principal.actor}）",
        )
    return principal


def require_self_or_admin(request: Request, name: str) -> Principal:
    """依赖：超级管理员，或令牌所属的 user 自己。"""
    principal = principal_of(request)
    if principal.admin:
        return principal
    if principal.user and str(principal.user) == str(name):
        return principal
    raise HTTPException(status_code=403, detail="forbidden: 只能操作自己的 user")


def can_touch(request: Request, owner: str | None) -> bool:
    """当前令牌能不能**看到**这个 owner 的资源。"""
    return can_view(principal_of(request), owner)


def forbid_out_of_scope(
    request: Request, owner: str | None, what: str = "资源", modify: bool = False
) -> None:
    """归属检查。

    - 看不见（不是自己的 user）→ **404**（不泄露存在性）
    - 看得见但不能改（全局共享资源）→ **403**
    """
    principal = principal_of(request)
    if not can_view(principal, owner):
        raise HTTPException(status_code=404, detail=f"没有这个{what}")
    if modify and not can_modify(principal, owner):
        raise HTTPException(
            status_code=403,
            detail=f"{what}是全局共享资源（owner 为空）：当前令牌只能读，不能停/删/改",
        )


def scoped_labels(request: Request, labels: dict | None) -> dict:
    """创建资源时的标签（**只是元数据**，不再参与授权判定）。"""
    return dict(labels or {})


def owner_for(request: Request, requested: str | None = None) -> str | None:
    """创建资源时的归属。

    - 普通令牌：强制用自己的 user
    - 超级管理员：可显式指定；不指定 = 全局共享资源
    """
    return owner_for_create(principal_of(request), requested)
