"""模板校验验证：未知字段报错 + docker/compose 迁移提示。

自隔离：用临时模板目录（BERTH_TEMPLATES_DIR），不碰真实模板。

运行：python3 tests/test_template.py
"""
import os
import sys
import tempfile
from pathlib import Path

TEST_TPL = Path(tempfile.mkdtemp(prefix="berth-tpl-test-"))
os.environ["BERTH_TEMPLATES_DIR"] = str(TEST_TPL)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sandbox.service import load_template  # noqa: E402

BASE = {
    "name": "t",
    "image": "base",
    "command": ["/bin/true"],
    "ports": [{"internal": 6379, "external_range": [16379, 16479]}],
}


def write_tpl(content: str) -> None:
    (TEST_TPL / "t.yaml").write_text(content, encoding="utf-8")


def expect_error(fragment: str, yaml_text: str) -> str:
    write_tpl(yaml_text)
    try:
        load_template("t")
    except ValueError as e:
        msg = str(e)
        assert fragment in msg, f"报错里应包含 {fragment!r}，实际: {msg}"
        return msg
    raise AssertionError(f"应当报错（{fragment}）")


def yaml_of(**overrides) -> str:
    import yaml as pyyaml

    data = dict(BASE)
    data.update(overrides)
    return pyyaml.safe_dump(data, allow_unicode=True)


def main():
    # 1) 合法模板能过
    #    真实模板也顺便验一遍
    import yaml as pyyaml

    for name in ("redis", "mysql"):
        tpl = pyyaml.safe_load((ROOT / "templates" / f"{name}.yaml").read_text())
        assert tpl["command"] and tpl["ports"]
    write_tpl(yaml_of())
    assert load_template("t")["name"] == "t"
    print("✔ 合法模板通过")

    # 2) docker/compose 习惯字段 → 迁移提示
    msg = expect_error("command", yaml_of(entrypoint=["/usr/bin/redis-server"]))
    print("✔ entrypoint 提示:", msg[:80], "...")

    msg = expect_error("image: <名字>", yaml_of(image_rootfs="/opt/berth/rootfs"))
    print("✔ image_rootfs 旧字段提示:", msg[:70], "...")

    expect_error("env", yaml_of(environment={"A": "1"}))
    expect_error("resources.memory_mb", yaml_of(mem_limit="256m"))
    expect_error("resources.cpu_percent", yaml_of(cpus=0.5))
    print("✔ environment / mem_limit / cpus 提示 OK")

    # 3) 未知字段（无提示）
    expect_error("未知字段", yaml_of(foo_bar=1))
    print("✔ 未知字段直接报错")

    # 4) ports 字符串形式（compose 的 "6379:6379"）
    msg = expect_error("external_range", yaml_of(ports=["6379:6379"]))
    print("✔ ports 字符串形式提示:", msg[:80], "...")

    # 5) 类型错误
    expect_error("command 必须是数组", yaml_of(command="/bin/true"))
    expect_error("args 必须是数组", yaml_of(args="--port 6379"))
    expect_error("resources", yaml_of(resources={"memory": 512}))
    print("✔ 类型/子字段校验 OK")

    print("全部通过 ✔")


if __name__ == "__main__":
    main()
