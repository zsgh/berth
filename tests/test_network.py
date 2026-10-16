"""任务 2.2 验证：端口分配。

用独立端口段（26379+），不和在跑的服务抢 16379/16380。

运行：python3 tests/test_network.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox.network import allocate_port, release_port  # noqa: E402

RANGE = (26379, 26479)


def main():
    ids = [f"nettest-{i}" for i in range(1, 4)]
    for sid in ids:
        release_port(sid)

    p1 = allocate_port(ids[0], 6379, RANGE)
    p2 = allocate_port(ids[1], 6379, RANGE)
    print("p1 =", p1, "p2 =", p2)
    assert (p1, p2) == (26379, 26380), (p1, p2)

    # 幂等：同一服务重复分配返回同一个端口
    assert allocate_port(ids[0], 6379, RANGE) == p1

    release_port(ids[0])
    p3 = allocate_port(ids[2], 6379, RANGE)
    print("p3 =", p3)
    assert p3 == 26379, p3

    # prefer：优先复用指定端口（服务重启场景）
    release_port(ids[2])
    p4 = allocate_port(ids[2], 6379, RANGE, prefer=26390)
    print("p4(prefer=26390) =", p4)
    assert p4 == 26390, p4

    for sid in ids:
        release_port(sid)

    # 区间满时抛错（用 3 个端口的小区间）
    tiny = (26490, 26493)
    holders = [f"flood-{i}" for i in range(4)]
    try:
        for sid in holders:
            allocate_port(sid, 6379, tiny)
        raise AssertionError("应当抛出区间已满")
    except RuntimeError as e:
        print("区间满 OK:", e)
    finally:
        for sid in holders:
            release_port(sid)

    print("全部通过 ✔")


if __name__ == "__main__":
    main()
