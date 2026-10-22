"""M2 验证：per-user netns 的地址分配与规则管理（纯逻辑，不需要真 netns）。

真机上的连通性验证在 VM 里做（见 docs/design.md 第 8 节"验证"清单）。

运行：python3 tests/test_netns.py
"""
import os
import sys
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-netns-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox import netns  # noqa: E402


def main():
    print("=== 命名与地址分配 ===")
    assert netns.netns_name("shop") == "berth-shop"
    assert netns.veth_names(3) == ("v3h", "v3n")
    assert netns.subnet_for(3) == "10.200.3.0/24"
    assert netns.gateway_for(3) == "10.200.3.1"
    assert netns.ip_for(3) == "10.200.3.2"
    print("  ", netns.netns_name("shop"), netns.veth_names(3), netns.subnet_for(3))

    print("=== 网段 → 序号 反推 ===")
    assert netns.index_of("10.200.3.0/24") == 3
    assert netns.index_of("10.200.12.0/24") == 12
    assert netns.index_of(None) is None
    assert netns.index_of("192.168.1.0/24") is None, "不是我们的网段"
    print("✔ 反推 OK")

    print("=== 序号分配（跳过已用的）===")
    assert netns.allocate_index(set()) == 1
    assert netns.allocate_index({1, 2}) == 3
    assert netns.allocate_index({1, 3}) == 2
    print("✔ 分配 OK")

    print("=== 规则参数解析（iptables -S → -D）===")
    line = (
        '-A POSTROUTING -s 10.200.2.0/24 -m comment --comment "berth:shop:nat" '
        "-j MASQUERADE"
    )
    args = netns._rule_args(line)
    print("  ", args)
    assert args[0] == "-s", args
    assert "--comment" in args
    assert "berth:shop:nat" in args, args
    assert netns._comment_of(line) == "berth:shop:nat"
    print("✔ 参数解析 OK")

    print("=== comment 精确匹配（不串味）===")
    a = '-A POSTROUTING -m comment --comment "berth:shop:nat" -j MASQUERADE'
    b = '-A POSTROUTING -m comment --comment "berth:shop:16380:hair" -j MASQUERADE'
    assert netns._comment_of(a) == "berth:shop:nat"
    assert netns._comment_of(b) == "berth:shop:16380:hair"
    assert netns._comment_of(a) != netns._comment_of(b)
    print("✔ 精确匹配 OK")

    print("=== 开关：BERTH_NETNS=0 时退回 host 网络 ===")
    os.environ["BERTH_NETNS"] = "0"
    assert netns.enabled() is False
    assert netns.exec_prefix("shop") == []
    assert netns.netns_for_user("shop") is None
    os.environ.pop("BERTH_NETNS")
    assert netns.enabled() is True
    print("✔ 开关生效")

    print("=== 宿主要挡的端口 ===")
    assert 22 in netns._block_ports()
    assert 9000 in netns._block_ports()
    os.environ["BERTH_HOST_BLOCK_PORTS"] = "22,9000,3306"
    assert netns._block_ports() == [22, 9000, 3306]
    os.environ.pop("BERTH_HOST_BLOCK_PORTS")
    print("✔ 端口列表 OK")

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
