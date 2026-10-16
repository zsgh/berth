"""任务 2.3 验证：服务生命周期（redis）。

自隔离运行：独立 BERTH_HOME（DB/目录）+ 独立端口段（26379+），
不干扰正在运行的 daemon 和服务。

运行：python3 tests/test_service.py
需要：/opt/berth/rootfs-redis 已构建；宿主有 redis-cli。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── 隔离：独立运行数据目录 + 独立模板（端口段错开在跑的服务）──
TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TPL_DIR = TEST_HOME / "templates"
TPL_DIR.mkdir(parents=True)
src = (Path(__file__).resolve().parent.parent / "templates" / "redis.yaml").read_text()
(TPL_DIR / "redis.yaml").write_text(src.replace("[16379, 16479]", "[26379, 26479]"))
# 第二个模板（内容相同、文件名不同）用于测「同名不同模板 → 冲突」
(TPL_DIR / "redis-alt.yaml").write_text(src.replace("[16379, 16479]", "[26479, 26579]"))
os.environ["BERTH_TEMPLATES_DIR"] = str(TPL_DIR)

from sandbox.network import get_port  # noqa: E402
from sandbox.service import (  # noqa: E402
    delete_service,
    get_service,
    list_services,
    list_volumes,
    start_existing_service,
    start_service,
    stop_service,
)


def redis_ping(port: int) -> str:
    res = subprocess.run(
        ["redis-cli", "-h", "localhost", "-p", str(port), "ping"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return res.stdout.strip()


def main():
    print("隔离目录:", TEST_HOME)
    print("=== 启动 redis 服务 ===")
    svc = start_service("redis", "my-redis")
    print(svc["service_id"], svc["port"], svc["status"])
    assert svc["status"] == "running", svc
    assert svc["host"] == "localhost"
    port = svc["port"]
    assert port == 26379, port
    service_id = svc["service_id"]

    print("=== 外部连接（redis-cli） ===")
    assert redis_ping(port) == "PONG", "redis-cli ping 失败"
    print(f"redis-cli -h localhost -p {port} ping → PONG")

    print("=== 第二个实例（端口不冲突） ===")
    subprocess.run(
        ["redis-cli", "-h", "localhost", "-p", str(port), "set", "k", "v"], check=True, timeout=10
    )
    svc2 = start_service("redis", "my-redis-2")
    assert svc2["port"] == 26380, svc2
    assert get_port(svc2["service_id"]) == 26380

    print("=== 列表（默认只列活跃） ===")
    assert len(list_services()) >= 2
    assert len(list_services(include_inactive=True)) >= 2

    print("=== 停止 ===")
    stopped = stop_service(service_id)
    assert stopped["status"] == "stopped"
    assert get_service(service_id)["status"] == "stopped"

    # unit 应该消失（--collect）
    res = subprocess.run(["systemctl", "status", stopped["unit"]], capture_output=True, text=True)
    assert res.returncode != 0, "unit 应该已被回收"
    print("unit 已回收")

    # 端口应该已释放
    assert redis_ping(port) != "PONG", "端口应该已释放"

    # 原样再起：端口优先复用，数据还在
    again = start_existing_service(service_id)
    print("再起:", again["service_id"], again["port"], again["status"])
    assert again["status"] == "running"
    assert again["port"] == 26379, again
    assert redis_ping(again["port"]) == "PONG"
    assert subprocess.run(
        ["redis-cli", "-h", "localhost", "-p", str(again["port"]), "get", "k"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip() == "v", "数据应该保留（appendonly）"
    stop_service(service_id)

    # 端口复用：停掉 svc2 后新区间第一个端口仍是 26379
    stop_service(svc2["service_id"])
    time.sleep(0.5)
    svc3 = start_service("redis", "my-redis-3")
    print("复用后的 port:", svc3["port"])
    assert svc3["port"] == 26379, svc3
    assert redis_ping(26379) == "PONG"
    stop_service(svc3["service_id"])

    print("=== 同名幂等（ensure 语义，防止重复创建） ===")
    a = start_service("redis", "ensure-me")
    b = start_service("redis", "ensure-me")
    print("两次同名请求 →", a["service_id"], "/", b["service_id"], "| reused:", b["reused"])
    assert a["service_id"] == b["service_id"], "同名请求不应重复创建"
    assert b["reused"] is True and b["status"] == "running"
    subprocess.run(
        ["redis-cli", "-h", "localhost", "-p", str(b["port"]), "set", "keep", "yes"],
        check=True, timeout=10,
    )

    # 停止后再同名请求 → 复用同一个 id，数据卷保留
    stop_service(a["service_id"])
    c = start_service("redis", "ensure-me")
    assert c["service_id"] == a["service_id"] and c["reused"] is True, c
    got = subprocess.run(
        ["redis-cli", "-h", "localhost", "-p", str(c["port"]), "get", "keep"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    assert got == "yes", "复用实例后数据应保留"
    stop_service(c["service_id"])

    # 同名 + 不同模板 → 冲突（409 语义）
    from sandbox.service import ServiceConflictError

    try:
        start_service("redis-alt", "ensure-me")
        raise AssertionError("应当报同名冲突")
    except ServiceConflictError as e:
        print("同名冲突 OK:", str(e)[:70], "...")
    print("✔ ensure 语义通过")

    print("=== 用量 / 数据卷 / 连卷删除 ===")
    d = start_service("redis", "usage-check")
    detail = get_service("usage-check")
    u = detail["usage"]
    print("usage:", u)
    assert u["memory_bytes"] and u["memory_bytes"] > 0, "运行中应有内存用量"
    assert u["memory_limit_bytes"] and u["disk_bytes"] is not None
    vols = [v for v in list_volumes() if v["service_id"] == d["service_id"]]
    assert vols and vols[0]["orphan"] is False, vols
    print("卷:", vols[0]["bytes"], "bytes")

    deleted = delete_service("usage-check", volumes=True)
    assert deleted["deleted"] and deleted["volumes_deleted"]
    assert not any(v["service_id"] == d["service_id"] for v in list_volumes())
    assert get_service("usage-check") is None
    print("✔ 用量/卷/删除通过")

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        # 收尾：停掉本测试创建的所有服务（防止残留 systemd unit/端口）
        try:
            from sandbox.service import delete_service, list_service_rows

            for row in list_service_rows():
                try:
                    delete_service(row["service_id"], volumes=True)
                except Exception:
                    pass
        except Exception:
            pass
        # 沙箱产物（uid 1000）用 root 清
        subprocess.run(["sudo", "-n", "rm", "-rf", str(TEST_HOME)], check=False)
        shutil.rmtree(TEST_HOME, ignore_errors=True)
