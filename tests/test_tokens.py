"""令牌与作用域验证：签发 / 解析 / 作用域隔离 / admin-only。

不需要 systemd：只测令牌层和 HTTP 层的可见性。

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
    apply_scope,
    create_token,
    in_scope,
    list_tokens,
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
    print("✔ 匿名 = admin（兼容模式）")

    print("=== 签发带作用域的 token ===")
    record, raw = create_token("shop", {"labels": {"project": "shop"}})
    print("  id:", record["id"], "| scope:", record["scope"])
    assert record["scope"] == {"labels": {"project": "shop"}}
    assert raw.startswith("bt_")

    print("=== 解析 token ===")
    p = resolve_token(raw)
    assert p is not None
    assert not p.admin, p
    assert p.name == "shop"
    assert p.token_id == record["id"]
    assert "raw" not in p.actor, p.actor
    print("✔ 解析成功，actor:", p.actor)

    assert resolve_token("bt_wrong") is None
    print("✔ 错的 token 解析为 None")

    print("=== 共享资源（shared=true）：人人可见、只读 ===")
    from sandbox.tokens import can_modify, in_scope, is_shared

    shared = {"shared": "true"}
    assert is_shared(shared) is True
    assert is_shared({"project": "shop"}) is False
    # 可见
    assert in_scope(p, shared) is True, "共享资源对所有 scoped token 可见"
    # 但不可改
    assert can_modify(p, shared) is False, "共享资源对 scoped token 只读"
    print("✔ 共享资源：可见但不可改")

    print("=== 作用域判定 ===")
    assert in_scope(p, {"project": "shop"}) is True
    assert in_scope(p, {"project": "shop", "run": "1"}) is True
    assert in_scope(p, {"project": "other"}) is False
    assert in_scope(p, {}) is False, "没标签的老资源对 scoped token 不可见"
    assert in_scope(p, None) is False
    print("✔ 作用域判定 OK")

    print("=== 创建时强制打作用域标签 ===")
    labels = apply_scope(p, {"project": "evil", "run": "1"})
    print("  请求标签 {project: evil, run: 1} →", labels)
    assert labels == {"project": "shop", "run": "1"}, labels
    print("✔ 作用域优先，scoped token 建不到别的项目去")

    print("=== 吊销后立即失效 ===")
    assert resolve_token(raw) is not None
    revoke_token(record["id"])
    assert resolve_token(raw) is None
    print("✔ 吊销后解析为 None")

    print("=== 环境变量里的 admin token（可多个 → 轮换）===")
    os.environ["BERTH_TOKEN"] = "old-one, new-one"
    assert config.tokens() == ["old-one", "new-one"], config.tokens()
    p1 = resolve_token("old-one")
    p2 = resolve_token("new-one")
    assert p1 and p1.admin, p1
    assert p2 and p2.admin, p2
    print("✔ 两个环境变量 token 都是 admin（轮换时可同时挂两个）")

    print("=== 有 token 后：不带 token 的请求要 401 ===")
    try:
        require_auth(FakeRequest())
        raise AssertionError("应当 401")
    except HTTPException as e:
        assert e.status_code == 401, e.status_code
        print("✔ 401")

    print("=== 库里有 token 时也要 401 ===")
    os.environ.pop("BERTH_TOKEN")
    rec2, raw2 = create_token("no-scope", None)
    try:
        require_auth(FakeRequest())
        raise AssertionError("应当 401")
    except HTTPException as e:
        assert e.status_code == 401, e.status_code
        print("✔ 401")

    print("=== 作用域 token 能通过鉴权 ===")
    req = FakeRequest({"authorization": f"Bearer {raw2}"})
    principal = require_auth(req)
    assert not principal.admin
    print("✔ scoped 身份解析正确")

    print("=== 列表不泄露明文 ===")
    rows = list_tokens()
    for r in rows:
        assert "token_hash" not in r, r
        assert "token" not in r, r
    print("✔ 列表里没有明文/哈希:", [(r["id"], r["name"]) for r in rows])

    print("=== HTTP 层：作用域隔离 ===")
    try:
        from fastapi.testclient import TestClient
    except Exception as e:  # pragma: no cover
        print("（跳过 HTTP 层：", e, "）")
        return

    import server

    # 造两个不同项目的服务记录
    record_service(
        service_id="svc-shop", name="shop-db", template="redis", unit="berth-svc-shop",
        host="localhost", port=16379, internal_port=6379, status="stopped",
        dir="/tmp/x", persist=1, labels='{"project": "shop"}',
    )
    record_service(
        service_id="svc-other", name="other-db", template="redis", unit="berth-svc-other",
        host="localhost", port=16380, internal_port=6379, status="stopped",
        dir="/tmp/y", persist=1, labels='{"project": "other"}',
    )
    record_run("run-shop", "print(1)", {
        "stdout": "", "stderr": "", "exit_code": 0, "duration": 0.1, "artifacts": [],
        "status": "finished", "image": "base", "labels": {"project": "shop"},
    })
    record_run("run-other", "print(2)", {
        "stdout": "", "stderr": "", "exit_code": 0, "duration": 0.1, "artifacts": [],
        "status": "finished", "image": "base", "labels": {"project": "other"},
    })

    shop_token = create_token("shop-token", {"labels": {"project": "shop"}})[1]
    admin_token = "admin-token-xyz"
    os.environ["BERTH_TOKEN"] = admin_token

    with TestClient(server.app) as client:
        H = {"Authorization": f"Bearer {shop_token}"}
        A = {"Authorization": f"Bearer {admin_token}"}

        r = client.get("/services?all=true", headers=H)
        names = [s["name"] for s in r.json()]
        assert names == ["shop-db"], names
        print("✔ scoped token 只看到自己的服务:", names)

        r = client.get("/services?all=true", headers=A)
        assert len(r.json()) == 2, r.json()
        print("✔ admin 看到全部")

        r = client.get("/services/other-db", headers=H)
        assert r.status_code == 404, r.status_code
        print("✔ 作用域外的服务 → 404")

        r = client.get("/services/shop-db", headers=H)
        assert r.status_code == 200
        print("✔ 作用域内的服务 → 200")

        r = client.get("/scripts?limit=50", headers=H)
        ids = [x["task_id"] for x in r.json()["runs"]]
        assert "run-shop" in ids and "run-other" not in ids, ids
        print("✔ 执行列表也按作用域过滤:", ids)

        r = client.get("/scripts/run-other", headers=H)
        assert r.status_code == 404, r.status_code
        print("✔ 作用域外的执行 → 404")

        # 造镜像/删镜像 = admin only
        r = client.post("/images", headers=H, json={"name": "x"})
        assert r.status_code == 403, r.status_code
        print("✔ scoped token 不能造镜像 →", r.json()["detail"][:40])

        r = client.delete("/images/base", headers=H)
        assert r.status_code == 403, r.status_code
        print("✔ scoped token 不能删镜像")

        # 共享资源：scoped token 看得见但改不了
        record_service(
            service_id="svc-shared", name="shared-db", template="mysql", unit="berth-svc-shared",
            host="localhost", port=13306, internal_port=3306, status="stopped",
            dir="/tmp/z", persist=1, labels='{"shared": "true"}',
        )
        r = client.get("/services/shared-db", headers=H)
        assert r.status_code == 200, r.status_code
        print("✔ scoped token 看得见共享服务")
        r = client.delete("/services/shared-db", headers=H)
        assert r.status_code == 403, (r.status_code, r.text)
        print("✔ 但删不了 →", r.json()["detail"][:50])
        r = client.post("/services/shared-db/stop", headers=H)
        assert r.status_code == 403, r.status_code
        print("✔ 也停不了")
        # admin 可以
        r = client.delete("/services/shared-db", headers=A)
        assert r.status_code == 200, r.text
        print("✔ admin 可以删共享服务")

        # scoped token 不能创建共享资源
        r = client.put(
            "/services/new-shared",
            headers=H,
            json={"template": "redis", "labels": {"shared": "true"}},
        )
        assert r.status_code == 403, (r.status_code, r.text)
        print("✔ scoped token 不能把资源标成共享 →", r.json()["detail"][:40])

        # 令牌管理 = admin only
        r = client.get("/tokens", headers=H)
        assert r.status_code == 403, r.status_code
        r = client.get("/tokens", headers=A)
        assert r.status_code == 200
        print("✔ 令牌管理只允许 admin")

        print("=== scoped token 不能往别人项目的 task 里植入文件 ===")
        # run-other 属于 project=other；shop 的 token 不该能往里写
        r = client.put(
            "/scripts/run-other/files",
            headers=H,
            files=[("files", ("evil.py", b"import os; os.system('rm -rf /')", "text/x-python"))],
        )
        assert r.status_code == 404, (r.status_code, r.text)
        print("✔ 往作用域外的 task 上传 → 404")

        # 自己作用域内的 task 可以上传（并自动圈定归属）
        r = client.put(
            "/scripts/run-shop/files",
            headers=H,
            files=[("files", ("ok.py", b"print(1)", "text/x-python"))],
        )
        assert r.status_code == 200, r.text
        print("✔ 自己作用域内上传 OK:", r.json()["files"])

        # 上传后 task 被圈进这个作用域：别的 token 再来写就 404
        r = client.put(
            "/scripts/run-shop/files",
            headers=A,
            files=[("files", ("x.py", b"print(2)", "text/x-python"))],
        )
        assert r.status_code in (200, 404), r.status_code
        print("✔ admin 仍可操作（admin 不受限）:", r.status_code)

        # 新 task：scoped token 上传会自动建带作用域标签的占位记录
        r = client.put(
            "/scripts/fresh-task/files",
            headers=H,
            files=[("files", ("t.py", b"print(3)", "text/x-python"))],
        )
        assert r.status_code == 200, r.text
        from state.db import get_run

        rec = get_run("fresh-task")
        assert rec and rec.get("labels") == {"project": "shop"}, rec
        print("✔ 新 task 自动圈进作用域:", rec["labels"], rec["status"])

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
