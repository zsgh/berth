"""任务 2.2 验证：端口分配。

运行：python3 tests/test_network.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox.network import allocate_port, release_port  # noqa: E402


def main():
    for sid in ("svc-1", "svc-2", "svc-3"):
        release_port(sid)

    p1 = allocate_port("svc-1", 6379)
    p2 = allocate_port("svc-2", 6379)
    print("p1 =", p1, "p2 =", p2)
    assert p1 == 16379, p1
    assert p2 == 16380, p2

    # 幂等：同一服务重复分配返回同一个端口
    assert allocate_port("svc-1", 6379) == p1

    release_port("svc-1")
    p3 = allocate_port("svc-3", 6379)
    print("p3 =", p3)
    assert p3 == 16379, p3

    # 区间满时抛错
    release_port("svc-2")
    release_port("svc-3")
    allocated = []
    try:
        for i in range(101):  # 区间只有 100 个
            allocated.append(allocate_port(f"flood-{i}", 6379))
        raise AssertionError("应当抛出区间已满")
    except RuntimeError as e:
        print("区间满 OK:", e)
    finally:
        for sid in [f"flood-{i}" for i in range(101)] + ["svc-1", "svc-2", "svc-3"]:
            release_port(sid)

    print("全部通过 ✔")


if __name__ == "__main__":
    main()
