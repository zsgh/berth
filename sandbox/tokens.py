"""API token：解析、作用域、主语（principal）。

模型（单租户自托管，别搞 OAuth）：
- **静态 token**：永不过期；泄露了手动换（支持同时挂多个 → 零停机轮换）
- **可选作用域**：按**标签**限定能碰哪些资源（复用 B12 的 labels 机制）
- **哈希存储**：库里只存 sha256，明文只在创建时返回一次（跟密码一样）

两种来源：
1. 环境变量 `BERTH_TOKEN`（可逗号分隔多个）→ **admin**（全权）
2. SQLite 的 `berth_tokens` 表 → 通常带作用域

作用域语义（标签域）：
- 作用域 `{"project": "shop"}` 的 token：
  - 只能看到 / 操作 **labels 包含 project=shop** 的服务、执行、产物
  - 创建资源时**强制打上**作用域标签（不能创建到别的项目去）
  - 看不到作用域外的资源 → 表现为 404（不泄露存在性）
- admin：不受限

不属于作用域的资源（老记录没标签的）对 scoped token **不可见**（默认拒绝）。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sandbox import config

# ─────────────────────────── 主语 ───────────────────────────


@dataclass
class Principal:
    """一次请求的调用方。"""

    admin: bool
    name: str = "anonymous"
    token_id: str | None = None
    scope: dict = field(default_factory=dict)  # {"labels": {...}}
    source: str = "none"                       # env / db

    @property
    def actor(self) -> str:
        """审计里的标识（不落 token 明文）。"""
        if self.admin and self.source == "env":
            return "admin:env"
        if self.token_id:
            return f"token:{self.token_id[:12]}"
        return "anonymous"

    @property
    def scope_labels(self) -> dict:
        return dict((self.scope or {}).get("labels") or {})

    def to_dict(self) -> dict:
        return {
            "admin": self.admin,
            "name": self.name,
            "token_id": self.token_id,
            "scope": self.scope,
            "actor": self.actor,
        }


ADMIN = Principal(admin=True, name="admin", source="env")


def hash_token(raw: str) -> str:
    """token 的存储形式（sha256 十六进制）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ─────────────────────────── 解析 ───────────────────────────


def resolve_token(raw: str | None) -> Principal | None:
    """把明文 token 解析成 Principal；无效返回 None。"""
    if not raw:
        return None
    raw = raw.strip()

    # 1) 环境变量里的 admin token（可逗号分隔多个 → 轮换时同时挂两个）
    for candidate in config.tokens():
        if secrets.compare_digest(candidate, raw):
            return Principal(admin=True, name="admin", source="env")

    # 2) 库里的 token（可能带作用域）
    from state.db import get_token_by_hash, touch_token

    row = get_token_by_hash(hash_token(raw))
    if not row:
        return None
    if row.get("revoked_at"):
        return None
    touch_token(row["id"])
    scope = row.get("scope") or {}
    if isinstance(scope, str):
        try:
            scope = json.loads(scope)
        except (TypeError, json.JSONDecodeError):
            scope = {}
    return Principal(
        # 库里签发的 token 一律**不是 admin**：
        # - admin（造/删镜像、签发令牌）只属于环境变量里的控制面凭证
        # - 没给 scope 的 token = 能看能改所有服务/执行（"工作令牌"），但碰不到宿主级操作
        admin=False,
        name=row.get("name") or "token",
        token_id=row.get("id"),
        scope=scope,
        source="db",
    )


# ─────────────────────────── 作用域判定 ───────────────────────────


def in_scope(principal: Principal | None, labels: dict | None) -> bool:
    """这个 principal 能不能碰带这些 labels 的资源。

    - admin：都能
    - scoped：资源的 labels 必须**包含**作用域要求的全部 k=v
    - 没标签的老资源：scoped 看不到（默认拒绝）
    """
    if principal is None:
        return False
    if principal.admin:
        return True
    want = principal.scope_labels
    if not want:
        return True
    have = labels or {}
    for key, value in want.items():
        if str(have.get(key)) != str(value):
            return False
    return True


def apply_scope(principal: Principal | None, labels: dict | None) -> dict:
    """创建资源时：把作用域标签**强制**打上去（scoped token 不能建到别的项目去）。"""
    out = dict(labels or {})
    if principal and not principal.admin:
        out.update(principal.scope_labels)
    return out


# ─────────────────────────── 管理（CRUD） ───────────────────────────


def create_token(name: str, scope: dict | None = None) -> tuple[dict, str]:
    """新建一个 token。返回 (记录, 明文 token) —— **明文只在这里出现一次**。"""
    from state.db import insert_token

    raw = "bt_" + secrets.token_urlsafe(32)
    scope = scope or {}
    labels = scope.get("labels") or {}
    if scope and not labels:
        raise ValueError("scope 目前只支持 {\"labels\": {...}} 形式")
    record = insert_token(
        token_id="tk-" + uuid.uuid4().hex[:10],
        name=str(name or "token"),
        token_hash=hash_token(raw),
        scope=json.dumps(scope, ensure_ascii=False),
    )
    return record, raw


def list_tokens() -> list[dict]:
    from state.db import list_token_rows

    return list_token_rows()


def revoke_token(token_id: str) -> dict | None:
    from state.db import revoke_token_row

    return revoke_token_row(token_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
