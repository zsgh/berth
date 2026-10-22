"""沙箱 uid 池（M3）：每个 user 一个 uid。

原来所有沙箱都跑在固定 uid 1000 上，两个后果：

1. 宿主上**任何 uid 1000 的真实用户**都能读写沙箱产物（workspace / 数据卷）
2. 无法用 nftables 的 `owner` 匹配做端口级隔离（都长一样）

改成按 user 分配 uid（默认从 60000 起，避开系统号段）：

- 沙箱内降权到该 uid（`setpriv --reuid=<uid> --regid=<gid>`）
- workspace / 数据卷归该 uid → 别的 user 读不到
- 宿主上**同名 uid 的真实用户**要避开（分配时查 /etc/passwd）
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

UID_BASE = 60000        # 系统号段之上，一般没有真实用户
UID_MAX = 61000
DEFAULT_UID = 1000      # 没有 user 时的老行为（固定 1000）


def host_used_uids() -> set[int]:
    """宿主上已被占用的 uid（/etc/passwd + /etc/group）。"""
    used: set[int] = set()
    for path in ("/etc/passwd", "/etc/group"):
        try:
            for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and parts[2].isdigit():
                    used.add(int(parts[2]))
        except OSError:
            continue
    return used


def allocate_uid(taken: set[int] | None = None) -> int:
    """分配一个没被占用的 uid。"""
    used = set(taken or ()) | host_used_uids()
    for uid in range(UID_BASE, UID_MAX):
        if uid not in used:
            return uid
    raise RuntimeError(f"uid 池耗尽（{UID_BASE}-{UID_MAX}）")


def uid_for_user(user: str | None) -> int:
    """user 的沙箱 uid（没有 user → 固定 1000，保持老行为）。"""
    if not user:
        return DEFAULT_UID
    from state.db import ensure_user, get_user, list_users, update_user

    row = get_user(user) or ensure_user(user)
    if row.get("uid"):
        return int(row["uid"])
    taken = {int(u["uid"]) for u in list_users() if u.get("uid")}
    uid = allocate_uid(taken)
    update_user(user, uid=uid)
    log.info("给 user %s 分配沙箱 uid: %d", user, uid)
    return uid


def gid_for_user(user: str | None) -> int:
    """沙箱的 gid（跟 uid 一样，简单点）。"""
    return uid_for_user(user)
