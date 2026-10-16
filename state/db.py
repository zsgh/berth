"""SQLite 元数据存储（任务 1.3 / 2.2 / 3.x）。

记录三类数据：
    runs     —— 每次脚本执行的历史（审计 / 查询）
    services —— 长驻服务的元数据（状态恢复的依据）
    ports    —— 端口分配表（服务停止时释放）

数据库默认在 /var/lib/berth/daemon.db（可用 BERTH_DB 覆盖）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sandbox.paths import BASE_DIR, ensure_base_dir

DB_PATH = Path(os.environ.get("BERTH_DB", str(BASE_DIR / "daemon.db")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        _SCHEMA_READY = True
    return conn


def init_db() -> None:
    """确保表结构存在（可重复调用）。"""
    connect().close()


# ─────────────────────────── runs ───────────────────────────


def record_run(task_id: str, code: str, result: dict) -> None:
    """记录一次脚本执行（task_id 已存在则覆盖最新一次）。"""
    duration_ms = int(round(float(result.get("duration", 0)) * 1000))
    finished_at = _now()
    created_at = result.get("created_at") or finished_at
    conn = connect()
    with conn:
        conn.execute(
            """
            INSERT INTO runs (task_id, code, stdout, stderr, exit_code, duration_ms,
                              artifacts, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                code=excluded.code, stdout=excluded.stdout, stderr=excluded.stderr,
                exit_code=excluded.exit_code, duration_ms=excluded.duration_ms,
                artifacts=excluded.artifacts, created_at=excluded.created_at,
                finished_at=excluded.finished_at
            """,
            (
                task_id,
                code,
                result.get("stdout"),
                result.get("stderr"),
                result.get("exit_code"),
                duration_ms,
                json.dumps(result.get("artifacts", []), ensure_ascii=False),
                created_at,
                finished_at,
            ),
        )
    conn.close()


def _run_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["artifacts"] = json.loads(d.get("artifacts") or "[]")
    except (TypeError, json.JSONDecodeError):
        d["artifacts"] = []
    return d


def get_run(task_id: str) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM runs WHERE task_id=?", (task_id,)).fetchone()
    conn.close()
    return _run_row_to_dict(row) if row else None


def list_runs(limit: int = 100) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY finished_at DESC LIMIT ?", (max(1, int(limit)),)
    ).fetchall()
    conn.close()
    return [_run_row_to_dict(r) for r in rows]


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


def get_service_row(service_id: str) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT * FROM services WHERE service_id=?", (service_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_service_rows(status: str | None = None) -> list[dict]:
    conn = connect()
    if status:
        rows = conn.execute(
            "SELECT * FROM services WHERE status=? ORDER BY created_at", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM services ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_service_status(service_id: str, status: str) -> None:
    conn = connect()
    with conn:
        conn.execute(
            "UPDATE services SET status=?, updated_at=? WHERE service_id=?",
            (status, _now(), service_id),
        )
    conn.close()


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


if __name__ == "__main__":
    init_db()
    print(f"db ready: {DB_PATH}")
