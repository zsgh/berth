"""HTTP 层验证（B3 / B4 / B12 + 系统接口）。

不需要 systemd / root：用 FastAPI 的 TestClient 打真实路由
（跑脚本 / 起服务这类要 systemd 的功能不在这里测）。

运行：python3 tests/test_api.py
"""
import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-api-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")
os.environ["BERTH_TOKEN"] = "t0ken"
os.environ["BERTH_MOUNT_ROOTS"] = str(TEST_HOME / "mounts")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from fastapi.testclient import TestClient
except Exception as e:  # pragma: no cover - 没装 httpx 就跳过
    print("需要 httpx 才能跑 HTTP 层测试：", e)
    raise SystemExit(0)

import server  # noqa: E402
from state.db import record_service  # noqa: E402

AUTH = {"Authorization": "Bearer t0ken"}


def main():
    with TestClient(server.app) as client:
        print("=== 鉴权 ===")
        assert client.get("/health").status_code == 200
        assert client.get("/system/info").status_code == 401
        assert client.get("/system/info", headers=AUTH).status_code == 200
        print("✔ 401 / 200")

        print("=== /system/info ===")
        info = client.get("/system/info", headers=AUTH).json()
        assert info["limits"]["timeout_seconds"] >= 600
        assert "base" in info["images"]
        assert set(info["network"]["modes"]) == {"host", "none", "allow"}
        assert "postgres" in info["templates"]
        print("  ", json.dumps(info["limits"], ensure_ascii=False))

        print("=== /system/stats ===")
        stats = client.get("/system/stats", headers=AUTH).json()
        assert "disk" in stats and "services" in stats and "runs" in stats
        print("  ", json.dumps(stats["services"], ensure_ascii=False))

        print("=== 资源上限（B4）===")
        r = client.post(
            "/scripts/run",
            headers=AUTH,
            json={"code": "print(1)", "timeout": 10 ** 6},
        )
        assert r.status_code == 400, r.text
        assert "上限" in r.json()["detail"], r.text
        print("✔ 超上限被拒:", r.json()["detail"])

        print("=== 文件上传 / 下载 / 文件树（B3）===")
        r = client.put(
            "/scripts/demo/files",
            headers=AUTH,
            files=[
                ("files", ("tests/test_x.py", b"def test_x():\n    pass\n", "text/x-python")),
                ("files", ("report.html", b"<h1>hi</h1>", "text/html")),
            ],
        )
        assert r.status_code == 200, r.text
        uploaded = r.json()["files"]
        assert "tests/test_x.py" in uploaded, uploaded
        print("  上传:", uploaded)

        r = client.get("/scripts/demo/tree", headers=AUTH)
        assert r.status_code == 200, r.text
        tree = r.json()["files"]
        assert any(e["path"] == "tests/test_x.py" for e in tree), tree
        print("  文件树:", [e["path"] for e in tree])

        r = client.get("/scripts/demo/files/tests/test_x.py", headers=AUTH)
        assert r.status_code == 200, r.text
        assert b"def test_x" in r.content
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "attachment" in r.headers.get("content-disposition", "")
        print("✔ 下载单文件（nosniff + attachment）")

        r = client.get("/scripts/demo/files/report.html?preview=1", headers=AUTH)
        assert r.status_code == 200
        assert "sandbox" in r.headers.get("content-security-policy", "")
        print("✔ HTML 预览带严格 CSP:", r.headers.get("content-security-policy"))

        r = client.get("/scripts/demo/files", headers=AUTH)
        assert r.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(r.content)) as tar:
            names = tar.getnames()
        assert any(n.endswith("test_x.py") for n in names), names
        print("✔ 打包下载:", names)

        print("=== 路径穿越防护 ===")
        r = client.get("/scripts/demo/files/..%2F..%2Fetc%2Fpasswd", headers=AUTH)
        assert r.status_code in (400, 404), r.status_code
        print("✔ 穿越被拒:", r.status_code)

        print("=== 标签过滤（B12）===")
        from state.db import record_run

        record_run(
            "labeled",
            "print(1)",
            {
                "stdout": "1\n",
                "stderr": "",
                "exit_code": 0,
                "duration": 0.1,
                "artifacts": [],
                "status": "finished",
                "image": "base",
                "labels": {"project": "alpha"},
            },
        )
        record_run(
            "unlabeled",
            "print(2)",
            {
                "stdout": "2\n",
                "stderr": "",
                "exit_code": 0,
                "duration": 0.1,
                "artifacts": [],
                "status": "finished",
                "image": "base",
            },
        )
        r = client.get("/scripts?label=project=alpha", headers=AUTH)
        ids = [row["task_id"] for row in r.json()["runs"]]
        assert "labeled" in ids and "unlabeled" not in ids, ids
        print("✔ 标签过滤:", ids)

        r = client.get("/scripts", headers=AUTH)
        assert len(r.json()["runs"]) >= 2
        print("✔ 不带标签过滤 = 全部")

        print("=== 模板接口 ===")
        r = client.get("/templates", headers=AUTH)
        assert "postgres" in r.json()
        r = client.get("/templates/postgres", headers=AUTH)
        assert r.status_code == 200 and r.json()["image"] == "postgres"

        r = client.post(
            "/templates/validate",
            headers={**AUTH, "Content-Type": "application/json"},
            json={"name": "x", "image": "base", "command": ["/bin/true"], "ports": [{"internal": 1}]},
        )
        assert r.status_code == 200 and r.json()["valid"] is True, r.text

        r = client.post(
            "/templates/validate",
            headers={**AUTH, "Content-Type": "application/json"},
            json={"name": "x", "image": "base", "command": ["/bin/true"], "ports": [{"internal": 1}], "entrypoint": ["/x"]},
        )
        assert r.status_code == 400, r.text
        assert "command" in r.json()["detail"]
        print("✔ 模板校验接口 OK")

        print("=== 孤儿卷清理 ===")
        orphan_dir = TEST_HOME / "services" / "svc-orphan" / "volumes"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        (orphan_dir / "data.bin").write_bytes(b"x" * 10)
        r = client.get("/volumes", headers=AUTH)
        assert any(v["service_id"] == "svc-orphan" and v["orphan"] for v in r.json()), r.text

        r = client.delete("/volumes/svc-orphan", headers=AUTH)
        assert r.status_code == 200, r.text
        assert not orphan_dir.exists()
        print("✔ 孤儿卷可清理")

        # 非孤儿卷 → 409
        record_service(
            service_id="svc-live",
            name="live",
            template="redis",
            unit="berth-svc-live",
            host="localhost",
            port=16379,
            internal_port=6379,
            status="stopped",
            dir=str(TEST_HOME / "services" / "svc-live"),
            persist=1,
        )
        live_dir = TEST_HOME / "services" / "svc-live" / "volumes"
        live_dir.mkdir(parents=True, exist_ok=True)
        r = client.delete("/volumes/svc-live", headers=AUTH)
        assert r.status_code == 409, r.status_code
        assert live_dir.exists()
        print("✔ 非孤儿卷 → 409")

        print("=== 操作审计（P1）===")
        r = client.get("/events", headers=AUTH)
        assert r.status_code == 200
        events = r.json()
        assert any(e["method"] == "PUT" for e in events), events
        assert all("t0ken" not in json.dumps(e) for e in events), "审计里不能落 token 明文"
        print("✔ 审计记录:", [e["action"] for e in events][:4])

        print("=== UI 静态资源 ===")
        r = client.get("/ui/")
        assert r.status_code == 200
        assert "alpine" in r.text.lower()
        r = client.get("/ui/vendor/alpine.min.js")
        assert r.status_code == 200
        r = client.get("/ui/app.js")
        assert r.status_code == 200
        print("✔ /ui 可访问（无 CDN 依赖）")

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
