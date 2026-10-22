"""API token：身份（user）+ 有效期，取代旧版的"标签域 scope"。

模型（单租户自托管，别搞 OAuth）：
- **静态 token**：默认不过期；泄露了手动换（`BERTH_TOKEN` 可挂多个 → 零停机轮换）
- **身份**：令牌绑定一个 **user**（= 租户/项目/团队）。授权判定 = **owner 相等**，
  不再靠标签匹配（标签拼错/多标签歧义/同名即同域都会出问题）
- **有效期**：可选 ttl（秒）→ 到期自动失效（动态 worker 场景必需）
- **哈希存储**：库里只存 sha256，明文只在创建时返回一次（跟密码一样）

两种身份：
| 来源 | 身份 | 能做什么 |
|---|---|---|
| `BERTH_TOKEN`（环境变量 / 只读配置文件，可逗号分隔多个） | **超级管理员**（独一份） | 跨 user + 宿主级操作（造/删镜像、管 user、签发令牌） |
| `berth_tokens` 表签发 | 某个 **user** | 只能看到/操作自己 user 的资源 + 全局共享资源（只读） |

资源归属：
- `owner = <user 名>` → 该 user 私有
- `owner IS NULL` → **全局共享资源**（比如公共 mysql/redis）：所有 user 可见，**只读**
"""
from __future__ import annotations

import hashlib
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
    user: str | None = None        # 归属的 user（非 admin 令牌）
    scope: dict = field(default_factory=dict)   # 旧字段，保留兼容读取
    source: str = "none"           # env / file / db

    @property
    def actor(self) -> str:
        """审计里的标识（不落 token 明文）。"""
        if self.admin and self.source == "env":
            return "admin:env"
        if self.admin and self.source == "file":
            return "admin:local"
        if self.user:
            return f"user:{self.user}"
        if self.token_id:
            return f"token:{self.token_id[:12]}"
        return "anonymous"

    @property
    def scope_labels(self) -> dict:
        """旧接口兼容：标签域（不再用于授权判定）。"""
        return dict((self.scope or {}).get("labels") or {})

    def to_dict(self) -> dict:
        return {
            "admin": self.admin,
            "name": self.name,
            "token_id": self.token_id,
            "user": self.user,
            "scope": self.scope,
            "actor": self.actor,
        }


def hash_token(raw: str) -> str:
    """token 的存储形式（sha256 十六进制）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ─────────────────────────── 解析 ───────────────────────────


def resolve_token(raw: str | None) -> Principal | None:
    """把明文 token 解析成 Principal；无效/过期/已撤销 → None。"""
    if not raw:
        return None
    raw = raw.strip()

    # 1) 超级管理员令牌（环境变量，可逗号分隔多个 → 轮换时同时挂新旧）
    for candidate in config.env_tokens():
        if secrets.compare_digest(candidate, raw):
            return Principal(admin=True, name="admin", source="env")

    # 1b) 本地令牌文件里的那个（自动生成的同机 token）
    from_file = config.file_token()
    if from_file and secrets.compare_digest(from_file, raw):
        return Principal(admin=True, name="local", source="file")

    # 2) 库里的令牌（绑定 user）
    from state.db import get_token_by_hash, touch_token

    row = get_token_by_hash(hash_token(raw))
    if not row:
        return None
    if row.get("revoked_at"):
        return None
    expires_at = row.get("expires_at")
    if expires_at and str(expires_at) <= _now():
        return None
    touch_token(row["id"])

    user = row.get("user")
    if not user:
        # 没有归属 user 的库令牌（理论上迁移时已作废）→ 不给权限
        return None
    return Principal(
        admin=False,
        name=row.get("name") or str(user),
        token_id=row.get("id"),
        user=str(user),
        source="db",
    )


# ─────────────────────────── 授权判定 ───────────────────────────


def is_shared(owner: str | None) -> bool:
    """是不是**全局共享资源**（owner IS NULL）。

    公共基础设施（比如所有项目共用的 mysql / redis）：
    - 所有 user 都能看到（agent 要能找到它、拿 host:port 去连）
    - **只读**（除了超级管理员，谁都不能停/删/改）
    """
    return owner is None or str(owner) == ""


def can_view(principal: Principal | None, owner: str | None) -> bool:
    """可见性。

    - 超级管理员：全都能看
    - 全局共享资源（owner IS NULL）：人人可见
    - 自己的 user 的资源：可见
    - 别人的：看不见
    """
    if principal is None:
        return False
    if principal.admin:
        return True
    if is_shared(owner):
        return True
    return bool(principal.user) and str(owner) == str(principal.user)


def can_modify(principal: Principal | None, owner: str | None) -> bool:
    """可修改性（停/删/改）。

    - 超级管理员：全都能
    - 全局共享资源：只读 —— 只有超级管理员能动
    - 自己的 user 的资源：随便
    - 别人的：不行
    """
    if principal is None:
        return False
    if principal.admin:
        return True
    if is_shared(owner):
        return False
    return bool(principal.user) and str(owner) == str(principal.user)


def owner_for_create(principal: Principal | None, requested: str | None = None) -> str | None:
    """创建资源时的归属。

    - 超级管理员：可以显式指定 owner；不指定 = 全局共享资源（owner=NULL）
    - 普通令牌：强制用自己的 user（不能把资源建到别人名下）
    """
    if principal is not None and not principal.admin:
        return principal.user
    if requested is not None and str(requested).strip():
        return str(requested).strip()
    return None


# ─────────────────────────── 管理（CRUD） ───────────────────────────


def create_token(name: str, user: str, ttl: int = 0) -> tuple[dict, str]:
    """新建一个令牌（绑定 user）。返回 (记录, 明文 token) —— **明文只出现一次**。

    user 不存在会自动建。
    """
    from state.db import ensure_user, insert_token

    user = str(user or "").strip()
    if not user:
        raise ValueError("令牌必须绑定一个 user（授权靠它）")
    ensure_user(user)
    raw = "bt_" + secrets.token_urlsafe(32)
    record = insert_token(
        token_id="tk-" + uuid.uuid4().hex[:10],
        name=str(name or "token"),
        token_hash=hash_token(raw),
        user=user,
        ttl=int(ttl or 0),
    )
    return record, raw


def list_tokens() -> list[dict]:
    from state.db import list_token_rows

    return list_token_rows()


def revoke_token(token_id: str) -> dict | None:
    from state.db import revoke_token_row

    return revoke_token_row(token_id)


# ─────────────────────────── 本地令牌文件（A） ───────────────────────────


def ensure_token_file() -> tuple[str | None, str | None]:
    """确保本地令牌文件存在；不存在就生成一个（0600，目录 0700）。

    返回 (路径, token)。没配 `BERTH_TOKEN_FILE` → (None, None)。

    这样同机跑的 agent 直接读这个文件就能拿到超级管理员令牌 —— 不用把长期凭证
    复制到各个地方，也不用改代码。
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

# 引导码失败的限流：{client: [失败次数, 锁定到的时间戳]}
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
