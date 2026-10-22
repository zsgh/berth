"""令牌与 user 归属验证：签发 / 身份 / 隔离 / 超级管理员专属。

授权判定 = owner 相等（不是标签匹配）：
- 自己的 user 的资源：可看可改
- 全局共享资源（owner 为空）：可看，**只读**
- 别人的 user 的资源：看不见（404）

运行：python3 tests/test_tokens.py
"""
import os
import sys
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-token-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")
os.environ.pop("BERTH_TOKEN", None)  # 先不配环境变量 token

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.auth import require_auth  # noqa: E402
from sandbox import config  # noqa: E402
from sandbox.tokens import (  # noqa: E402
    can_modify,
    can_view,
    create_token,
    is_shared,
    list_tokens,
    owner_for_create,
    resolve_token,
    revoke_token,
)


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.scope = {"path_params": {}}
        self.state = type("S", (), {})()


def main():
    from fastapi import HTTPException

    from state.db import init_db, record_run, record_service

    init_db()

    print("=== 没配任何 token：不鉴权（兼容旧行为）===")
    req = FakeRequest()
    principal = require_auth(req)
    assert principal.admin, principal
    print("✔ 匿名 = 超级管理员（兼容模式）")

    print("=== 签发带 user 的令牌 ===")
    record, raw = create_token("shop-agent", "shop", ttl=3600)
    print("  id:", record["id"], "| user:", record["user"], "| 到期:", record["expires_at"])
    assert record["user"] == "shop"
    assert record["expires_at"], record

    print("=== 解析 token ===")
    p = resolve_token(raw)
    assert p is not None
    assert not p.admin, p
    assert p.user == "shop", p
    assert p.actor == "user:shop", p.actor
    assert resolve_token("bt_wrong") is None
    print("✔ 解析成功，actor:", p.actor)

    print("=== 授权判定（owner 相等）===")
    assert can_view(p, "shop") is True
    assert can_view(p, "other") is False
    assert can_view(p, None) is True, "全局共享资源对所有 user 可见"
    assert can_modify(p, "shop") is True
    assert can_modify(p, "other") is False
    assert can_modify(p, None) is False, "全局共享资源对普通 user 只读"
    assert is_shared(None) is True
    print("✔ 可见 / 可改 判定 OK")

    print("=== 创建时归属强制 ===")
    assert owner_for_create(p, "evil") == "shop", "普通令牌不能把资源建到别人名下"
    from sandbox.tokens import Principal

    adm = Principal(admin=True, name="admin", source="env")
    assert owner_for_create(adm, "acme") == "acme"
    assert owner_for_create(adm, None) is None, "超管不指定 = 全局共享"
    print("✔ 归属强制 OK")

    print("=== 吊销后立即失效 ===")
    assert resolve_token(raw) is not None
    revoke_token(record["id"])
    assert resolve_token(raw) is None
    print("✔ 吊销后解析为 None")

    print("=== 过期自动失效 ===")
    rec2, raw2 = create_token("expired", "shop", ttl=60)
    assert resolve_token(raw2) is not None
    # 把到期时间拨到过去（模拟过期，不用真等）
    from state.db import connect

    conn = connect()
    with conn:
        conn.execute(
            "UPDATE berth_tokens SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (rec2["id"],),
        )
    conn.close()
    assert resolve_token(raw2) is None, "过期令牌不应可用"
    print("✔ 过期令牌不可用")

    print("=== 环境变量里的超级管理员（可多个 → 轮换）===")
    os.environ["BERTH_TOKEN"] = "old-one, new-one"
    assert config.env_tokens() == ["old-one", "new-one"], config.env_tokens()
    p1 = resolve_token("old-one")
    p2 = resolve_token("new-one")
    assert p1 and p1.admin, p1
    assert p2 and p2.admin, p2
    print("✔ 两个环境变量 token 都是超级管理员")

    print("=== 有 token 后：不带 token 的请求要 401 ===")
    try:
        require_auth(FakeRequest())
        raise AssertionError("应当 401")
    except HTTPException as e:
        assert e.status_code == 401, e.status_code
        print("✔ 401")

    print("=== 库里也有 token 时也要 401 ===")
    os.environ.pop("BERTH_TOKEN")
    rec3, raw3 = create_token("w", "acme")
    try:
        require_auth(FakeRequest())
        raise AssertionError("应当 401")
    except HTTPException as e:
        assert e.status_code == 401, e.status_code
        print("✔ 401")

    print("=== 列表不泄露明文 ===")
    for r in list_tokens():
        assert "token_hash" not in r, r
        assert "token" not in r, r
    print("✔ 列表里没有明文/哈希")

    # ── HTTP 层 ──
    print("=== HTTP 层：user 隔离 ===")
    try:
        from fastapi.testclient import TestClient
    except Exception as e:  # pragma: no cover
        print("（跳过 HTTP 层：", e, "）")
        return

    import server

    record_service(
        service_id="svc-shop", name="shop-db", template="redis", unit="berth-svc-shop",
        host="localhost", port=16379, internal_port=6379, status="stopped",
        dir="/tmp/x", persist=1, owner="shop",
    )
    record_service(
        service_id="svc-other", name="other-db", template="redis", unit="berth-svc-other",
        host="localhost", port=16380, internal_port=6379, status="stopped",
        dir="/tmp/y", persist=1, owner="other",
    )
    record_service(
        service_id="svc-shared", name="shared-db", template="mysql", unit="berth-svc-shared",
        host="localhost", port=13306, internal_port=3306, status="stopped",
        dir="/tmp/z", persist=1, owner=None,   # 全局共享
    )
    record_run("run-shop", "print(1)", {
        "stdout": "", "stderr": "", "exit_code": 0, "duration": 0.1, "artifacts": [],
        "status": "finished", "image": "base", "owner": "shop",
    })
    record_run("run-other", "print(2)", {
        "stdout": "", "stderr": "", "exit_code": 0, "duration": 0.1, "artifacts": [],
        "status": "finished", "image": "base", "owner": "other",
    })

    shop_token = create_token("shop-token", "shop")[1]
    create_token("other-token", "other")
    admin_token = "admin-token-xyz"
    os.environ["BERTH_TOKEN"] = admin_token

    with TestClient(server.app) as client:
        H = {"Authorization": f"Bearer {shop_token}"}
        A = {"Authorization": f"Bearer {admin_token}"}

        r = client.get("/services?all=true", headers=H)
        names = sorted(s["name"] for s in r.json())
        assert names == ["shared-db", "shop-db"], names
        print("✔ shop 令牌看到自己的 + 全局共享的:", names)

        r = client.get("/services/other-db", headers=H)
        assert r.status_code == 404, r.status_code
        print("✔ 别的 user 的服务 → 404")

        r = client.get("/services/shop-db", headers=H)
        assert r.status_code == 200
        print("✔ 自己的服务 → 200")

        r = client.delete("/services/shared-db", headers=H)
        assert r.status_code == 403, (r.status_code, r.text)
        print("✔ 全局共享资源删不了 →", r.json()["detail"][:44])

        r = client.get("/scripts?limit=50", headers=H)
        ids = [x["task_id"] for x in r.json()["runs"]]
        assert "run-shop" in ids and "run-other" not in ids, ids
        print("✔ 执行列表也按 user 过滤:", ids)

        r = client.get("/scripts/run-other", headers=H)
        assert r.status_code == 404, r.status_code
        print("✔ 别的 user 的执行 → 404")

        # 超管专属
        r = client.post("/images", headers=H, json={"name": "x"})
        assert r.status_code == 403, r.status_code
        print("✔ 普通令牌不能造镜像 →", r.json()["detail"][:40])

        r = client.get("/tokens", headers=H)
        assert r.status_code == 403, r.status_code
        r = client.get("/users", headers=H)
        assert r.status_code == 403, r.status_code
        print("✔ 令牌/用户管理只允许超管")

        r = client.get("/users", headers=A)
        assert r.status_code == 200
        print("✔ 超管看到所有 user:", [u["name"] for u in r.json()])

        # 建 user（超管）
        r = client.post("/users", headers=A, json={"name": "acme", "quota_runs": 5})
        assert r.status_code == 201, r.text
        assert r.json()["name"] == "acme"
        print("✔ 建 user:", r.json()["name"], "quota_runs=", r.json()["quota_runs"])

        # 令牌引用不存在的 user → 自动建
        r = client.post("/tokens", headers=A, json={"name": "auto", "user": "brand-new"})
        assert r.status_code == 201, r.text
        assert r.json()["user"] == "brand-new"
        r = client.get("/users/brand-new", headers=A)
        assert r.status_code == 200, r.text
        print("✔ 令牌引用不存在的 user → 自动建:", r.json()["name"])

        # TTL
        r = client.post("/tokens", headers=A, json={"name": "short", "user": "acme", "ttl": 60})
        assert r.status_code == 201 and r.json()["expires_at"], r.text
        print("✔ TTL 令牌带到期时间:", r.json()["expires_at"])

        # 普通令牌建服务 → owner 强制为自己的 user
        r = client.put(
            "/services/made-by-shop",
            headers=H,
            json={"template": "redis", "owner": "other"},
        )
        assert r.status_code in (200, 500, 400), r.status_code  # redis 镜像在测试环境可能没有
        if r.status_code == 200:
            assert r.json()["owner"] == "shop", r.json()
            print("✔ 普通令牌建的资源归属强制为自己的 user")
        else:
            print("（跳过归属断言：", r.json().get("detail", "")[:60], "）")

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
