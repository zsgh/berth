"""自动获取 token 验证：本地令牌文件 / 一次性引导码 / 客户端封装。

运行：python3 tests/test_token_bootstrap.py
"""
import os
import sys
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-boot-test-"))
TOKEN_DIR = TEST_HOME / "run"
TOKEN_DIR.mkdir(parents=True, exist_ok=True)
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")
os.environ["BERTH_TOKEN_FILE"] = str(TOKEN_DIR / "token")
os.environ.pop("BERTH_TOKEN", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox import config  # noqa: E402
from sandbox.tokens import (  # noqa: E402
    consume_bootstrap_code,
    ensure_bootstrap_code,
    ensure_token_file,
)


def main():
    from fastapi.testclient import TestClient

    print("=== 本地令牌文件：自动生成 ===")
    path, raw = ensure_token_file()
    print("  ", path)
    assert path and raw, (path, raw)
    assert Path(path).stat().st_mode & 0o777 == 0o600, "令牌文件必须是 0600"
    print("✔ 自动生成，权限 0600")

    print("=== 幂等：再调一次还是同一个 token ===")
    path2, raw2 = ensure_token_file()
    assert raw2 == raw, "重启不应换掉本地 token"
    print("✔ 同一个 token（重启不变，agent 不用重新配）")

    print("=== config.tokens() 认这个文件 ===")
    assert raw in config.tokens(), config.tokens()
    print("✔ 已纳入有效 token 列表")

    print("=== 一次性引导码 ===")
    cpath, code = ensure_bootstrap_code()
    print("  ", cpath, "→", code)
    assert code and len(code) == 8
    # 错的码不行
    assert consume_bootstrap_code("WRONGCODE") is False
    print("✔ 错码被拒")
    # 对的码能用一次
    assert consume_bootstrap_code(code) is True
    print("✔ 正确码通过")
    # 用过即废
    assert consume_bootstrap_code(code) is False
    print("✔ 一次性（用过即废）")

    print("=== HTTP：引导码换 token ===")
    import server

    cpath2, code2 = ensure_bootstrap_code()
    with TestClient(server.app) as client:
        # 错码 → 401
        r = client.post("/tokens/bootstrap", json={"code": "BADCODE1", "name": "x"})
        assert r.status_code == 401, r.text
        print("✔ 错码 → 401")

        # 对码 → 拿到 token（scoped）
        r = client.post(
            "/tokens/bootstrap",
            json={"code": code2, "name": "shakedown", "scope": {"labels": {"project": "shop"}}},
        )
        assert r.status_code == 201, r.text
        data = r.json()
        print("   id:", data["id"], "| scope:", data["scope"])
        assert data["token"].startswith("bt_")
        assert data["scope"] == {"labels": {"project": "shop"}}
        new_token = data["token"]

        # 换来的 token 能用，但只是受限身份
        r = client.get("/system/info", headers={"Authorization": f"Bearer {new_token}"})
        assert r.status_code == 200, r.text
        principal = r.json()["principal"]
        print("   principal:", principal)
        assert principal["admin"] is False
        assert principal["name"] == "shakedown"
        print("✔ 换来的 token 可用，且不是 admin")

        # 再用同一个码 → 不行
        r = client.post("/tokens/bootstrap", json={"code": code2, "name": "y"})
        assert r.status_code == 401, r.text
        print("✔ 码不能重复用")

        # 限流：连续错码会被锁
        codes = []
        for _ in range(6):
            rr = client.post("/tokens/bootstrap", json={"code": "NOPE1234", "name": "z"})
            codes.append(rr.status_code)
        print("   连续错码:", codes)
        assert 429 in codes, codes
        print("✔ 连续错码触发限流（429）")

    print("=== 客户端封装自动找 token ===")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from berth_client import find_token

    assert find_token() == raw, "应该自动找到本地令牌文件里的 token"
    print("✔ 客户端自动找到本地 token")

    os.environ["BERTH_TOKEN"] = "explicit-token"
    assert find_token() == "explicit-token"
    print("✔ 环境变量优先")
    del os.environ["BERTH_TOKEN"]

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
