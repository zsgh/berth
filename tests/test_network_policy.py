"""B2 验证：网络策略（host / none / allow）。

不需要 systemd：只测策略归一化、白名单解析、systemd 属性拼装。

运行：python3 tests/test_network_policy.py
"""
import os
import sys
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-netpol-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox.network import (  # noqa: E402
    network_capabilities,
    normalize_policy,
    policy_allows_localhost,
    resolve_allow_entries,
    systemd_network_properties,
)


def main():
    print("=== 策略归一化 ===")
    assert normalize_policy(None) == {"mode": "host", "allow": []}
    assert normalize_policy("none")["mode"] == "none"
    assert normalize_policy({"mode": "allow", "allow": "10.0.0.0/8"})["allow"] == ["10.0.0.0/8"]
    try:
        normalize_policy({"mode": "bogus"})
        raise AssertionError("应当拒绝未知 mode")
    except ValueError as e:
        print("✔ 未知 mode 报错:", str(e)[:60], "...")

    print("=== host 模式：不加任何 systemd 属性（保持现状） ===")
    assert systemd_network_properties({"mode": "host"}) == []
    assert systemd_network_properties(None) == []
    print("✔ host → 无额外属性")

    print("=== none 模式：断网 ===")
    props = systemd_network_properties({"mode": "none"})
    assert props == ["IPAddressDeny=any"], props
    print("✔ none →", props)

    print("=== allow 模式：白名单 ===")
    props = systemd_network_properties({"mode": "allow", "allow": ["10.0.0.0/8", "192.168.1.5"]})
    assert props[0] == "IPAddressDeny=any", props
    assert "IPAddressAllow=10.0.0.0/8" in props, props
    assert "IPAddressAllow=192.168.1.5/32" in props, props
    print("✔ allow →", props)

    print("=== 白名单条目归一化 ===")
    assert resolve_allow_entries(["10.0.0.5"]) == ["10.0.0.5/32"]
    assert resolve_allow_entries(["10.0.0.0/8"]) == ["10.0.0.0/8"]
    assert resolve_allow_entries(["192.168.0.0/16", "192.168.0.0/16"]) == ["192.168.0.0/16"]
    assert resolve_allow_entries(["::1"]) == ["::1/128"]
    print("✔ IP / CIDR 归一化 OK")

    print("=== loopback 判断（沙箱不能访问 berth API）===")
    assert policy_allows_localhost({"mode": "host"}) is True
    assert policy_allows_localhost({"mode": "none"}) is False
    assert policy_allows_localhost({"mode": "allow", "allow": ["10.0.0.0/8"]}) is False
    assert policy_allows_localhost({"mode": "allow", "allow": ["127.0.0.0/8"]}) is True
    print("✔ loopback 判定 OK")

    print("=== 能力探测 ===")
    caps = network_capabilities()
    print("  ", caps)
    assert set(caps["modes"]) == {"host", "none", "allow"}

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
