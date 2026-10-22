"""B5 / B10 / B11 验证：模板覆盖（overrides）、内联 spec（spec）、init 段、http 健康检查。

不需要 systemd / root：只测纯函数。

运行：python3 tests/test_service_spec.py
"""
import os
import sys
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-spec-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox.service import (  # noqa: E402
    apply_overrides,
    load_template,
    render,
    resolve_spec,
    validate_template,
)

BASE = {
    "name": "t",
    "image": "base",
    "command": ["/bin/true"],
    "ports": [{"internal": 6379, "external_range": [16379, 16479]}],
}


def expect_error(fragment: str, tpl: dict):
    try:
        validate_template("t", tpl)
    except ValueError as e:
        assert fragment in str(e), f"报错里应包含 {fragment!r}，实际: {e}"
        return str(e)
    raise AssertionError(f"应当报错（{fragment}）")


def main():
    print("=== 真实模板都能过校验 ===")
    for name in ("redis", "mysql", "postgres"):
        tpl = load_template(name)
        assert tpl["command"] and tpl["ports"], name
        print(f"✔ {name}: ports={tpl['ports'][0]['internal']}")

    print("=== 内联 spec（起被测服务用）===")
    inline = {
        "name": "my-app",
        "image": "node",
        "command": ["/usr/bin/node", "/app/server.js"],
        "ports": [{"internal": 3000, "external_range": [23000, 23100]}],
        "mounts": [{"source": "/srv/berth-mounts/app", "target": "/app", "read_only": True}],
        "healthcheck": {"type": "http", "port": 3000, "path": "/healthz", "expect_status": 200},
    }
    name, tpl = resolve_spec(spec=inline)
    assert name == "my-app"
    assert tpl["mounts"][0]["target"] == "/app"
    print("✔ 内联 spec 解析:", name)

    print("=== overrides 合并规则 ===")
    tpl = {
        **BASE,
        "env": {"A": "1", "B": "2"},
        "args": ["--x"],
        "resources": {"memory_mb": 512, "cpu_percent": 100},
    }
    out = apply_overrides(tpl, {"env": {"B": "9", "C": "3"}, "resources": {"memory_mb": 1024}})
    assert out["env"] == {"A": "1", "B": "9", "C": "3"}, out["env"]
    assert out["resources"] == {"memory_mb": 1024, "cpu_percent": 100}, out["resources"]
    assert out["args"] == ["--x"]
    print("✔ env 合并 / resources 合并 / args 保留")

    out = apply_overrides(tpl, {"args": ["--y"], "ports": [{"internal": 6380}]})
    assert out["args"] == ["--y"]
    assert out["ports"] == [{"internal": 6380}]
    print("✔ args / ports 覆盖")

    print("=== 占位符渲染（未知大括号原样保留）===")
    assert render("{port}", {"port": 1234}) == "1234"
    assert render("{a}-{b}", {"a": "x", "b": "y"}) == "x-y"
    assert render("${VAR}", {"port": 1}) == "${VAR}"
    assert render("{unknown}", {}) == "{unknown}"
    assert render("SELECT {json}", {"json": "{}"}) == "SELECT {}"
    print("✔ render OK")

    print("=== 模板校验（新增字段）===")
    expect_error("healthcheck.type", {**BASE, "healthcheck": {"type": "grpc"}})
    expect_error("path", {**BASE, "healthcheck": {"type": "http"}})
    expect_error("init", {**BASE, "init": {"image": "x"}})
    expect_error("network.mode", {**BASE, "network": {"mode": "bogus"}})
    expect_error("mount 需要 source 和 target", {**BASE, "mounts": [{"source": "/a"}]})
    expect_error("未知字段", {**BASE, "whatever": 1})
    print("✔ 校验拦截 OK")

    print("=== http 健康检查字段被接受 ===")
    tpl = validate_template("t", {**BASE, "healthcheck": {"type": "http", "port": 80, "path": "/healthz", "expect_status": [200, 204]}})
    assert tpl["healthcheck"]["expect_status"] == [200, 204]
    print("✔ http 健康检查 OK")

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
