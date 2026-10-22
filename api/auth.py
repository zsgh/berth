"""Bearer token 鉴权 + 作用域（B1 + 令牌方案）。

设计：
- `BERTH_TOKEN`（可逗号分隔多个）→ **admin**，全权
- SQLite `berth_tokens` 表里的 token → 通常带**作用域**（按 labels 限定资源）
- 库里只存 token 的 sha256；明文只在创建时返回一次
- 审计里的 actor 是 token 的短 id / `admin:env`，不落明文

作用域怎么用：
- scoped token 只能看到 / 操作 **labels 包含作用域要求**的服务、执行、产物
- 创建资源时**强制**打上作用域标签（不能建到别的项目去）
- 作用域外的资源 → 404（不泄露存在性）
- 镜像管理（造/删镜像）是宿主级操作 → **只允许 admin**
"""
from __future__ import annotations

import hashlib

from fastapi import HTTPException, Request

from sandbox import config
from sandbox.tokens import (
    Principal,
    apply_scope,
    can_modify,
    in_scope,
    is_shared,
    resolve_token,
)

# 免鉴权的路径前缀（探活 / UI 静态资源）
PUBLIC_PREFIXES = ("/health", "/ui")


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PUBLIC_PREFIXES)


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

    - 没配任何 token（环境变量空 + 库里没有）→ 放行（向后兼容的本机自用模式）
    - token 无效/已撤销 → 401
    """
    expected_env = config.tokens()
    raw = bearer_of(request)

    if raw:
        principal = resolve_token(raw)
        if principal is not None:
            request.state.principal = principal
            return principal
        # 库里一个 token 都没有 → 只认环境变量
        if not _has_db_tokens():
            raise HTTPException(
                status_code=401,
                detail="unauthorized: token 无效",
                headers={"WWW-Authenticate": "Bearer"},
            )
        raise HTTPException(
            status_code=401,
            detail="unauthorized: token 无效或已撤销",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 没带 token
    if expected_env or _has_db_tokens():
        raise HTTPException(
            status_code=401,
            detail="unauthorized: 需要 Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # 完全没配置 token → 不鉴权（兼容旧行为），但标记成 admin
    principal = Principal(admin=True, name="anonymous", source="none")
    request.state.principal = principal
    return principal


def _has_db_tokens() -> bool:
    """库里有没有（未撤销的）token —— 决定要不要鉴权。"""
    try:
        from state.db import list_token_rows

        return any(not t.get("revoked_at") for t in list_token_rows())
    except Exception:
        return False


def principal_of(request: Request) -> Principal:
    """取当前请求的主语（没鉴权时是 admin 兼容模式）。"""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return Principal(admin=True, name="anonymous", source="none")
    return principal


def require_admin(request: Request) -> Principal:
    """依赖：只允许 admin（宿主级操作，比如造/删镜像）。"""
    principal = principal_of(request)
    if not principal.admin:
        raise HTTPException(
            status_code=403,
            detail=f"forbidden: 该操作需要 admin token（当前 token: {principal.name}）",
        )
    return principal


def scope_labels(request: Request) -> dict:
    """当前 token 的作用域标签（admin → 空 = 不限）。"""
    return principal_of(request).scope_labels


def can_touch(request: Request, labels: dict | None) -> bool:
    """当前 token 能不能**看到**带这些 labels 的资源。"""
    return in_scope(principal_of(request), labels)


def forbid_out_of_scope(
    request: Request, labels: dict | None, what: str = "资源", modify: bool = False
) -> None:
    """作用域检查。

    - 看不见（不在作用域内）→ **404**（不泄露存在性）
    - 看得见但不能改（共享资源 shared=true）→ **403**
    """
    principal = principal_of(request)
    if not in_scope(principal, labels):
        raise HTTPException(status_code=404, detail=f"没有这个{what}")
    if modify and not can_modify(principal, labels):
        raise HTTPException(
            status_code=403,
            detail=f"{what}是共享资源（shared=true）：当前令牌只能读，不能停/删/改",
        )


def scoped_labels(request: Request, labels: dict | None) -> dict:
    """创建资源时：强制打上作用域标签。"""
    return apply_scope(principal_of(request), labels)


def forbid_shared_create(request: Request, labels: dict | None) -> None:
    """只有 admin 能创建「共享资源」（shared=true）。"""
    principal = principal_of(request)
    if principal.admin:
        return
    if is_shared(labels or {}):
        raise HTTPException(
            status_code=403, detail="只有 admin 能创建共享资源（shared=true）"
        )
