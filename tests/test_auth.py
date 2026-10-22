"""B1 验证：Bearer token 鉴权。

不需要 systemd / root：直接测鉴权依赖 + （装了 httpx 的话）跑一遍 HTTP。

运行：python3 tests/test_auth.py
"""
import os
import sys
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-auth-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")
os.environ["BERTH_TOKEN"] = "secret-token"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from api.auth import actor_of, require_auth  # noqa: E402
from sandbox import config  # noqa: E402


class FakeRequest:
    """最小 Request 替身（只需要 headers / scope）。"""

    def __init__(self, headers=None):
        self.headers = headers or {}
        self.scope = {"path_params": {}}


def expect_401(fn, label):
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == 401, f"{label}: 期望 401，实际 {e.status_code}"
        print(f"✔ {label} → 401")
        return
    raise AssertionError(f"{label}: 应当返回 401")


def main():
    print("=== 单元：鉴权依赖 ===")
    assert config.token() == "secret-token"

    expect_401(lambda: require_auth(FakeRequest()), "没有 Authorization 头")
    expect_401(
        lambda: require_auth(FakeRequest({"authorization": "Bearer wrong"})),
        "token 不匹配",
    )
    expect_401(
        lambda: require_auth(FakeRequest({"authorization": "Basic secret-token"})),
        "不是 Bearer 方案",
    )
    # 正确的 token → 放行
    require_auth(FakeRequest({"authorization": "Bearer secret-token"}))
    print("✔ 正确 token 放行")

    # actor 标识不落明文
    actor = actor_of(FakeRequest({"authorization": "Bearer secret-token"}))
    assert "secret-token" not in actor, actor
    assert actor.startswith("token:"), actor
    print("✔ actor 标识:", actor)

    # 未配置 token → 不拦（向后兼容）
    del os.environ["BERTH_TOKEN"]
    require_auth(FakeRequest())
    print("✔ 未配置 BERTH_TOKEN 时不鉴权（向后兼容）")
    os.environ["BERTH_TOKEN"] = "secret-token"

    # ── HTTP 层（需要 httpx）──
    try:
        from fastapi.testclient import TestClient
    except Exception as e:  # pragma: no cover
        print("（跳过 HTTP 层验证：", e, "）")
        return

    print("=== HTTP 层 ===")
    import server

    with TestClient(server.app) as client:
        r = client.get("/health")
        assert r.status_code == 200, r.text
        print("✔ /health 免鉴权")

        r = client.get("/system/info")
        assert r.status_code == 401, r.status_code
        print("✔ 无 token → 401")

        r = client.get("/system/info", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401, r.status_code
        print("✔ 错 token → 401")

        r = client.get("/system/info", headers={"Authorization": "Bearer secret-token"})
        assert r.status_code == 200, r.text
        assert r.json()["auth"]["enabled"] is True
        print("✔ 正确 token → 200")

        r = client.get("/ui/")
        assert r.status_code == 200, r.status_code
        print("✔ /ui 静态页免鉴权（登录页要能加载）")

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
