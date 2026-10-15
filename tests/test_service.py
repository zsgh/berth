"""任务 2.3 验证：服务生命周期（redis）。

运行：python3 tests/test_service.py
需要：/opt/berth/rootfs-redis 已构建；宿主有 redis-cli。
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox.network import get_port  # noqa: E402
from sandbox.service import get_service, list_services, start_service, stop_service  # noqa: E402


def redis_ping(port: int) -> str:
    res = subprocess.run(
        ["redis-cli", "-h", "localhost", "-p", str(port), "ping"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return res.stdout.strip()


def main():
    print("=== 启动 redis 服务 ===")
    svc = start_service("redis", "my-redis")
    print(svc)
    assert svc["status"] == "running", svc
    assert svc["host"] == "localhost"
    port = svc["port"]
    assert port == 16379, port
    service_id = svc["service_id"]

    print("=== 外部连接（redis-cli） ===")
    assert redis_ping(port) == "PONG", "redis-cli ping 失败"
    print(f"redis-cli -h localhost -p {port} ping → PONG")

    print("=== 写入数据（多服务/端口不冲突检查） ===")
    subprocess.run(
        ["redis-cli", "-h", "localhost", "-p", str(port), "set", "k", "v"], check=True, timeout=10
    )
    # 第二个 redis 应该拿到 16380
    svc2 = start_service("redis", "my-redis-2")
    print(svc2)
    assert svc2["port"] == 16380, svc2
    assert get_port(svc2["service_id"]) == 16380

    print("=== 列表 ===")
    services = list_services()
    print(services)
    assert len([s for s in services if s["status"] == "running"]) >= 2

    print("=== 停止 ===")
    stopped = stop_service(service_id)
    print(stopped)
    assert stopped["status"] == "stopped"
    assert get_service(service_id)["status"] == "stopped"

    # unit 应该消失（--collect）
    res = subprocess.run(["systemctl", "status", stopped["unit"]], capture_output=True, text=True)
    assert res.returncode != 0, "unit 应该已被回收"
    print("unit 已回收")

    # 端口应该已释放：ping 不通
    assert redis_ping(port) != "PONG", "端口应该已释放"

    # 端口复用：停掉 svc2 后重新分配
    stop_service(svc2["service_id"])
    time.sleep(0.5)
    svc3 = start_service("redis", "my-redis-3")
    print("复用后的 port:", svc3["port"])
    assert svc3["port"] == 16379, svc3
    assert redis_ping(16379) == "PONG"
    stop_service(svc3["service_id"])

    print("全部通过 ✔")


if __name__ == "__main__":
    main()
