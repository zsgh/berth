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
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sandbox import config

log = logging.getLogger(__name__)

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
        if self.admin and self.source == "file":
            return "admin:local"
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
    for candidate in config.env_tokens():
        if secrets.compare_digest(candidate, raw):
            return Principal(admin=True, name="admin", source="env")

    # 1b) 本地令牌文件里的那个（自动生成的同机 token）
    from_file = config.file_token()
    if from_file and secrets.compare_digest(from_file, raw):
        return Principal(admin=True, name="local", source="file")

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


# ─────────────────────────── 本地令牌文件（A） ───────────────────────────


def ensure_token_file() -> tuple[str | None, str | None]:
    """确保本地令牌文件存在；不存在就生成一个（0600，目录 0700）。

    返回 (路径, token)。没配 `BERTH_TOKEN_FILE` → (None, None)。

    这样同机跑的 agent 直接读这个文件就能拿到 token —— 不用把长期凭证
    复制到每个地方，也不用改代码。
    """
    path = config.token_file()
    if path is None:
        return None, None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip()
            if raw:
                return str(path), raw
        raw = "bt_" + secrets.token_urlsafe(32)
        path.write_text(raw + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return str(path), raw
    except OSError as e:
        log.warning("本地令牌文件创建失败: %s", e)
        return None, None


def token_file_path() -> str | None:
    path = config.token_file()
    return str(path) if path else None


# ─────────────────────────── 一次性引导码（B） ───────────────────────────

# 引导码失败的限流：{ip: (失败次数, 锁定到的时间戳)}
_BOOTSTRAP_FAILS: dict[str, list] = {}
_BOOTSTRAP_MAX_FAILS = 5
_BOOTSTRAP_LOCK_SEC = 300


def bootstrap_code_path():
    """引导码文件（和令牌文件同目录）。"""
    path = config.token_file()
    if path is None:
        return None
    return path.parent / "bootstrap.code"


def ensure_bootstrap_code() -> tuple[str | None, str | None]:
    """生成/读取一次性引导码（写文件 0600，30 分钟内有效，用过即废）。"""
    path = bootstrap_code_path()
    if path is None:
        return None, None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            age = time.time() - path.stat().st_mtime
            if age <= config.bootstrap_ttl():
                return str(path), path.read_text(encoding="utf-8").strip()
            path.unlink(missing_ok=True)
        # 无歧义字符集（去掉 0/O/1/I/L），方便手输
        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        path.write_text(code + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return str(path), code
    except OSError as e:
        log.warning("引导码生成失败: %s", e)
        return None, None


def bootstrap_locked(client: str) -> bool:
    """这个客户端是不是被限流锁住了（失败次数到阈值才会锁）。"""
    entry = _BOOTSTRAP_FAILS.get(client)
    if not entry:
        return False
    if entry[1] > time.time():
        return True
    if entry[0] >= _BOOTSTRAP_MAX_FAILS:
        _BOOTSTRAP_FAILS.pop(client, None)
    return False


def note_bootstrap_fail(client: str) -> None:
    """记一次失败；累计到阈值（5 次）才锁 5 分钟。"""
    entry = _BOOTSTRAP_FAILS.get(client)
    now = time.time()
    if not entry or (entry[1] > now):
        entry = [entry[0] + 1 if entry else 1, 0.0]
        _BOOTSTRAP_FAILS[client] = entry
    else:
        entry[0] += 1
    if entry[0] >= _BOOTSTRAP_MAX_FAILS:
        entry[1] = now + _BOOTSTRAP_LOCK_SEC


def consume_bootstrap_code(code: str, client: str = "") -> bool:
    """校验并消费引导码（一次性）。成功返回 True。"""
    path = bootstrap_code_path()
    if path is None or not path.is_file():
        return False
    try:
        if time.time() - path.stat().st_mtime > config.bootstrap_ttl():
            return False
        stored = path.read_text(encoding="utf-8").strip()
        if not stored or not secrets.compare_digest(stored, str(code or "").strip().upper()):
            return False
        path.unlink(missing_ok=True)  # 一次性
        return True
    except OSError:
        return False
