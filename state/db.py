"""SQLite 元数据存储（任务 1.3 / 2.2 / 3.x）。

记录五类数据：
    runs     —— 每次脚本执行的历史（审计 / 查询）+ 异步执行的实时状态
    services —— 长驻服务的元数据（状态恢复的依据）
    ports    —— 端口分配表（服务停止时释放）
    events   —— 操作审计（谁 / 何时 / 对什么做了什么）
    labels   —— 通过 runs.labels / services.labels（JSON）内嵌，不单独建表

数据库默认在 /var/lib/berth/daemon.db（可用 BERTH_DB 覆盖）。

表结构是**懒建 + 增量迁移**的：老库启动时自动补齐新列（`ALTER TABLE ADD COLUMN`），
不需要手工迁移。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sandbox.paths import BASE_DIR, ensure_base_dir

DB_PATH = Path(os.environ.get("BERTH_DB", str(BASE_DIR / "daemon.db")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────── 表结构 ───────────────────────────

# 老版本 → 新版本补的列（列名 → 列定义）
MIGRATIONS: dict[str, dict[str, str]] = {
    "runs": {
        "owner": "TEXT",        # 归属的 user；NULL = 全局共享
        "memory_mb": "INTEGER", # 内存限额（配额统计用）
        "status": "TEXT DEFAULT 'finished'",   # running/finished/timeout/killed/error
        "image": "TEXT",
        "network": "TEXT",                      # JSON：网络策略
        "labels": "TEXT",                       # JSON：标签
        "timeout": "INTEGER",
        "started_at": "TEXT",
        "updated_at": "TEXT",
    },
    "services": {
        "owner": "TEXT",       # 归属的 user；NULL = 全局共享
        "labels": "TEXT",                       # JSON：标签
        "overrides": "TEXT",                    # JSON：请求里的 overrides
        "spec": "TEXT",                         # JSON：生效的完整模板
        "init_spec": "TEXT",                    # JSON：初始化定义
        "init_result": "TEXT",                  # JSON：初始化执行结果
        "network": "TEXT",                      # JSON：网络策略
        "task_id": "TEXT",                      # 关联的任务（共享 workspace）
    },
    "berth_tokens": {
        "user": "TEXT",                         # 归属的 user（= 租户/项目）
        "ttl": "INTEGER DEFAULT 0",            # 有效期（秒），0 = 永不过期
        "expires_at": "TEXT",                  # 到期时间
    },
}


def _create_tables(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                task_id      TEXT PRIMARY KEY,
                code         TEXT NOT NULL,
                stdout       TEXT,
                stderr       TEXT,
                exit_code    INTEGER,
                duration_ms  INTEGER,
                artifacts    TEXT,           -- JSON 数组
                created_at   TEXT NOT NULL,
                finished_at  TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS services (
                service_id    TEXT PRIMARY KEY,
                name          TEXT,
                template      TEXT NOT NULL,
                unit          TEXT NOT NULL,
                host          TEXT,
                port          INTEGER,
                internal_port INTEGER,
                status        TEXT NOT NULL,   -- starting/running/stopped/crashed
                dir           TEXT,            -- overlay 根目录
                persist       INTEGER DEFAULT 0,
                created_at    TEXT NOT NULL,
                updated_at    TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ports (
                service_id    TEXT PRIMARY KEY,
                internal_port INTEGER NOT NULL,
                external_port INTEGER NOT NULL UNIQUE,
                allocated_at  TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT NOT NULL,
                actor   TEXT,               -- token 标识（短哈希），匿名时为 'anonymous'
                action  TEXT NOT NULL,      -- 动词（create/delete/...）
                target  TEXT,               -- 操作对象（服务名 / task_id / ...）
                method  TEXT,
                path    TEXT,
                status  INTEGER,
                detail  TEXT                -- JSON
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_services_name ON services(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS berth_tokens (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                token_hash  TEXT NOT NULL UNIQUE,   -- sha256，明文不落库
                scope       TEXT,                    -- 旧版残留（0.3.x 的 scope 标签域），已废弃
                user        TEXT,                    -- 归属的 user（= 租户/项目）
                ttl         INTEGER DEFAULT 0,       -- 有效期（秒），0 = 永不过期
                expires_at  TEXT,                    -- 到期时间（算出来的）
                created_at  TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at  TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id               TEXT PRIMARY KEY,
                name             TEXT NOT NULL UNIQUE,  -- 业务标识（租户/项目/团队）
                netns            TEXT,                  -- Phase B：网络命名空间名
                cidr             TEXT,                  -- Phase B：网段
                uid              INTEGER,               -- Phase C：sandbox uid
                quota_runs       INTEGER DEFAULT 0,     -- 0 = 不限
                quota_memory_mb  INTEGER DEFAULT 0,
                quota_services   INTEGER DEFAULT 0,
                created_at       TEXT NOT NULL,
                notes            TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS image_builds (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                status     TEXT NOT NULL,   -- building/success/failed/cancelled
                spec       TEXT,            -- JSON：构建参数
                log        TEXT,            -- 构建输出（尾部）
                image_path TEXT,
                error      TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )


def _migrate(conn: sqlite3.Connection) -> None:
    """给老库补新增的列（幂等）。"""
    with conn:
        for table, cols in MIGRATIONS.items():
            try:
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.DatabaseError:  # pragma: no cover - 表还没建
                continue
            for name, decl in cols.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        _migrate_data(conn)


def _migrate_data(conn: sqlite3.Connection) -> None:
    """一次性数据迁移（0.3.x 的"标签域"→ 0.4 的"user 归属"）。

    - 资源：labels.project=X 的 → owner=X（user 不存在就建）；没有 project 的 → owner=NULL（全局共享）
    - 令牌：老的 scope 令牌**直接作废**（scope 语义不再支持），调用方需重新签发
    """
    # 资源归属：从 labels 推导一次
    for table in ("services", "runs"):
        try:
            rows = conn.execute(
                f"SELECT rowid AS rid, labels FROM {table} WHERE owner IS NULL AND labels IS NOT NULL"
            ).fetchall()
        except sqlite3.DatabaseError:  # pragma: no cover
            continue
        for row in rows:
            try:
                labels = json.loads(row["labels"]) or {}
            except (TypeError, json.JSONDecodeError):
                labels = {}
            owner = labels.get("project")
            if owner:
                _ensure_user_row(conn, str(owner))
            conn.execute(
                f"UPDATE {table} SET owner=? WHERE rowid=?", (str(owner) if owner else None, row["rid"])
            )

    # 老令牌作废（scope 语义已废弃）
    try:
        conn.execute(
            "UPDATE berth_tokens SET revoked_at=COALESCE(revoked_at, ?) "
            "WHERE user IS NULL AND revoked_at IS NULL",
            (_now(),),
        )
    except sqlite3.DatabaseError:  # pragma: no cover
        pass


def _ensure_user_row(conn: sqlite3.Connection, name: str) -> str:
    """确保有个 user 行（没有就建）。返回 user id。"""
    name = str(name).strip()
    row = conn.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    user_id = "usr-" + uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO users (id, name, created_at) VALUES (?, ?, ?)", (user_id, name, _now())
    )
    return user_id


_SCHEMA_READY = False


def connect() -> sqlite3.Connection:
    """打开连接（行工厂 + WAL，适合多线程/多进程访问）。表结构懒建。"""
    global _SCHEMA_READY
    if not DB_PATH.parent.exists():
        try:
            # 常规情况：数据目录归当前用户
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 首次运行且是「普通用户 + sudo」模式：/var/lib/berth 需要 root 建
            ensure_base_dir()
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    if not _SCHEMA_READY:
        _create_tables(conn)
        _migrate(conn)
        _SCHEMA_READY = True
    return conn


def init_db() -> None:
    """确保表结构存在（可重复调用）。"""
    connect().close()


# ─────────────────────────── JSON 小工具 ───────────────────────────


def _load_json(raw: str | None, default):
    if raw is None or raw == "":
        return default
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default
    return default if value is None else value


def _dump_json(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _labels_from_row(row) -> dict:
    d = dict(row)
    d["labels"] = _load_json(d.get("labels"), {})
    return d


# ─────────────────────────── runs ───────────────────────────


def record_run(task_id: str, code: str, result: dict) -> None:
    """记录一次脚本执行（task_id 已存在则覆盖最新一次）。

    status=running 时 finished_at 留空（异步执行刚提交、还没结束）。
    """
    duration_ms = int(round(float(result.get("duration", 0)) * 1000))
    status = result.get("status") or "finished"
    finished_at = result.get("finished_at") or (_now() if status != "running" else None)
    created_at = result.get("created_at") or _now()
    conn = connect()
    with conn:
        conn.execute(
            """
            INSERT INTO runs (task_id, code, stdout, stderr, exit_code, duration_ms,
                              artifacts, created_at, finished_at,
                              status, image, network, labels, timeout, started_at, updated_at,
                              owner, memory_mb)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                code=excluded.code, stdout=excluded.stdout, stderr=excluded.stderr,
                exit_code=excluded.exit_code, duration_ms=excluded.duration_ms,
                artifacts=excluded.artifacts, created_at=excluded.created_at,
                finished_at=excluded.finished_at,
                status=excluded.status, image=excluded.image, network=excluded.network,
                labels=excluded.labels, timeout=excluded.timeout,
                started_at=excluded.started_at, updated_at=excluded.updated_at,
                owner=excluded.owner, memory_mb=excluded.memory_mb
            """,
            (
                task_id,
                code,
                result.get("stdout"),
                result.get("stderr"),
                result.get("exit_code"),
                duration_ms,
                _dump_json(result.get("artifacts", [])) or "[]",
                created_at,
                finished_at,
                result.get("status") or "finished",
                result.get("image"),                _dump_json(result.get("network")),
                _dump_json(result.get("labels")) or "{}",
                result.get("timeout"),
                result.get("started_at") or created_at,
                _now(),
                result.get("owner"),
                result.get("memory_mb"),
            ),
        )
    conn.close()


def update_run(task_id: str, **fields) -> None:
    """更新一条执行记录（异步执行的状态流转用）。"""
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE runs SET {cols} WHERE task_id=?",
            (*fields.values(), task_id),
        )
    conn.close()


def _run_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["artifacts"] = _load_json(d.get("artifacts"), [])
    d["labels"] = _load_json(d.get("labels"), {})
    d["network"] = _load_json(d.get("network"), None)
    return d


def get_run(task_id: str) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM runs WHERE task_id=?", (task_id,)).fetchone()
    conn.close()
    return _run_row_to_dict(row) if row else None


def list_run_rows() -> list[dict]:
    """所有执行记录（不分页）。"""
    conn = connect()
    rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
    conn.close()
    return [_run_row_to_dict(r) for r in rows]


def list_runs(
    limit: int = 100,
    status: str | None = None,
    image: str | None = None,
    since: str | None = None,
    labels: dict | None = None,
    cursor: str | None = None,
) -> list[dict]:
    """执行列表：支持状态 / 镜像 / 时间 / 标签过滤 + 游标分页。

    cursor = 上一页最后一条的 created_at（ISO 字符串），返回比它更新的记录。
    """
    conn = connect()
    sql = "SELECT * FROM runs"
    where: list[str] = []
    args: list = []
    if status:
        where.append("status=?")
        args.append(status)
    if image:
        where.append("image=?")
        args.append(image)
    if since:
        where.append("created_at>=?")
        args.append(since)
    if cursor:
        where.append("created_at<?")
        args.append(cursor)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    rows = conn.execute(sql, tuple(args)).fetchall()
    conn.close()
    out = [_run_row_to_dict(r) for r in rows]
    if labels:
        out = [r for r in out if match_labels(r.get("labels") or {}, labels)]
    return out


def match_labels(have: dict, want: dict) -> bool:
    """标签匹配：want 的每个 k=v 都必须在 have 里存在且相等。

    want 的值如果是 None，只要求 key 存在。
    """
    for key, value in (want or {}).items():
        if key not in have:
            return False
        if value is None:
            continue
        if str(have.get(key)) != str(value):
            return False
    return True


def parse_label_filters(values: list[str] | None) -> dict:
    """把 `?label=project=foo` 解析成 {"project": "foo"}。

    - `label=key=value` → 精确匹配
    - `label=key`      → 只要有这个 key 就算
    """
    out: dict[str, str | None] = {}
    for raw in values or []:
        for part in str(raw).split(","):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                key, _, value = part.partition("=")
                out[key.strip()] = value.strip()
            else:
                out[part] = None
    return out


# ─────────────────────────── services ───────────────────────────


def record_service(**fields) -> None:
    """插入/更新一条服务记录（按 service_id upsert）。"""
    fields.setdefault("created_at", _now())
    fields["updated_at"] = _now()
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields if k != "service_id")
    conn = connect()
    with conn:
        conn.execute(
            f"INSERT INTO services ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(service_id) DO UPDATE SET {updates}",
            tuple(fields.values()),
        )
    conn.close()


def update_service(service_id: str, **fields) -> None:
    """更新服务记录的部分字段。"""
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE services SET {cols} WHERE service_id=?",
            (*fields.values(), service_id),
        )
    conn.close()


def get_service_row(service_id: str) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM services WHERE service_id=?", (service_id,)).fetchone()
    conn.close()
    return _labels_from_row(row) if row else None


def find_service_by_name(name: str) -> dict | None:
    """按名字找最近的实例（同名多历史时取最新一条）。"""
    conn = connect()
    row = conn.execute(
        "SELECT * FROM services WHERE name=? ORDER BY created_at DESC LIMIT 1", (name,)
    ).fetchone()
    conn.close()
    return _labels_from_row(row) if row else None


def list_service_rows(status: str | None = None) -> list[dict]:
    conn = connect()
    if status:
        rows = conn.execute(
            "SELECT * FROM services WHERE status=? ORDER BY created_at", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM services ORDER BY created_at").fetchall()
    conn.close()
    return [_labels_from_row(r) for r in rows]


def update_service_status(service_id: str, status: str) -> None:
    update_service(service_id, status=status)


def delete_service_row(service_id: str) -> None:
    """彻底删除一条服务记录（审计历史清理用）。"""
    conn = connect()
    with conn:
        conn.execute("DELETE FROM services WHERE service_id=?", (service_id,))
    conn.close()


# ─────────────────────────── ports ───────────────────────────


def list_port_rows() -> list[dict]:
    conn = connect()
    rows = conn.execute("SELECT * FROM ports ORDER BY external_port").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────── events（操作审计） ───────────────────────────


def record_event(
    action: str,
    target: str | None = None,
    *,
    actor: str | None = None,
    method: str | None = None,
    path: str | None = None,
    status: int | None = None,
    detail: dict | None = None,
) -> None:
    """写一条操作审计。"""
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO events (ts, actor, action, target, method, path, status, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(),
                actor or "anonymous",
                action,
                target,
                method,
                path,
                status,
                _dump_json(detail),
            ),
        )
    conn.close()


def list_events(limit: int = 100, action: str | None = None, target: str | None = None) -> list[dict]:
    conn = connect()
    sql = "SELECT * FROM events"
    where: list[str] = []
    args: list = []
    if action:
        where.append("action=?")
        args.append(action)
    if target:
        where.append("target=?")
        args.append(target)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    rows = conn.execute(sql, tuple(args)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = _load_json(d.get("detail"), None)
        out.append(d)
    return out


# ─────────────────────────── users（租户/项目） ───────────────────────────


def ensure_user(name: str, notes: str | None = None) -> dict:
    """确保 user 存在（不存在就建）。返回 user 记录。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("user 名字不能为空")
    conn = connect()
    with conn:
        row = conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        if row is None:
            user_id = "usr-" + uuid.uuid4().hex[:10]
            conn.execute(
                "INSERT INTO users (id, name, created_at, notes) VALUES (?, ?, ?, ?)",
                (user_id, name, _now(), notes),
            )
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row)


def get_user(name: str) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE name=?", (str(name),)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users() -> list[dict]:
    conn = connect()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_user(name: str, **fields) -> dict | None:
    allowed = {"notes", "quota_runs", "quota_memory_mb", "quota_services", "netns", "cidr", "uid"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return get_user(name)
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = connect()
    with conn:
        conn.execute(f"UPDATE users SET {cols} WHERE name=?", (*fields.values(), str(name)))
    conn.close()
    return get_user(name)


def delete_user(name: str) -> dict | None:
    row = get_user(name)
    if not row:
        return None
    conn = connect()
    with conn:
        conn.execute("DELETE FROM users WHERE name=?", (str(name),))
    conn.close()
    return row


def user_resource_counts(name: str) -> dict:
    """某个 user 名下的资源数量。"""
    conn = connect()
    out = {
        "services": conn.execute(
            "SELECT COUNT(*) FROM services WHERE owner=?", (name,)
        ).fetchone()[0],
        "runs": conn.execute("SELECT COUNT(*) FROM runs WHERE owner=?", (name,)).fetchone()[0],
    }
    conn.close()
    return out


# ─────────────────────────── berth_tokens（访问令牌） ───────────────────────────


def insert_token(
    *, token_id: str, name: str, token_hash: str, user: str | None, ttl: int = 0
) -> dict:
    """登记一个 token（只存哈希）。ttl 秒，0 = 永不过期。"""
    expires_at = None
    if ttl and int(ttl) > 0:
        from datetime import timedelta

        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(ttl))
        ).isoformat(timespec="seconds")
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO berth_tokens (id, name, token_hash, user, ttl, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token_id, name, token_hash, user, int(ttl or 0), expires_at, _now()),
        )
    conn.close()
    return {
        "id": token_id,
        "name": name,
        "user": user,
        "ttl": int(ttl or 0),
        "expires_at": expires_at,
        "created_at": _now(),
        "last_used_at": None,
        "revoked_at": None,
    }


def get_token_by_hash(token_hash: str) -> dict | None:
    conn = connect()
    row = conn.execute(
        "SELECT * FROM berth_tokens WHERE token_hash=?", (token_hash,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def touch_token(token_id: str) -> None:
    """记录"最近一次使用"（审计用）。"""
    conn = connect()
    with conn:
        conn.execute(
            "UPDATE berth_tokens SET last_used_at=? WHERE id=?", (_now(), token_id)
        )
    conn.close()


def list_token_rows() -> list[dict]:
    conn = connect()
    rows = conn.execute("SELECT * FROM berth_tokens ORDER BY created_at DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("token_hash", None)  # 永远不返回哈希
        d.pop("scope", None)       # 旧字段已废弃
        out.append(d)
    return out


def revoke_token_row(token_id: str) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM berth_tokens WHERE id=?", (token_id,)).fetchone()
    if not row:
        conn.close()
        return None
    with conn:
        conn.execute(
            "UPDATE berth_tokens SET revoked_at=? WHERE id=?", (_now(), token_id)
        )
    conn.close()
    d = dict(row)
    d["revoked_at"] = _now()
    d.pop("token_hash", None)
    return d


# ─────────────────────────── image_builds（镜像制作） ───────────────────────────


def record_image_build(build_id: str, name: str, spec: dict) -> None:
    """登记一次镜像构建（status=building）。"""
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO image_builds (id, name, status, spec, log, created_at) "
            "VALUES (?, ?, 'building', ?, '', ?)",
            (build_id, name, _dump_json(spec), _now()),
        )
    conn.close()


def update_image_build(build_id: str, **fields) -> None:
    """更新构建状态 / 日志。"""
    if not fields:
        return
    for key in ("spec",):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = _dump_json(fields[key])
    if fields.get("status") in ("success", "failed", "cancelled"):
        fields.setdefault("finished_at", _now())
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE image_builds SET {cols} WHERE id=?", (*fields.values(), build_id)
        )
    conn.close()


def get_image_build(build_id: str) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM image_builds WHERE id=?", (build_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["spec"] = _load_json(d.get("spec"), {})
    return d


def list_image_builds(limit: int = 50) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM image_builds ORDER BY created_at DESC LIMIT ?", (max(1, int(limit)),)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["spec"] = _load_json(d.get("spec"), {})
        out.append(d)
    return out


if __name__ == "__main__":
    init_db()
    print(f"db ready: {DB_PATH}")