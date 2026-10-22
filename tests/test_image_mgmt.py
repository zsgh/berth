"""镜像管理验证：制作（build）/ 删除（delete）/ 安全边界。

不需要 root：构建用假的构建脚本（只 echo + 建目录），删除只测"该拒绝的"。

运行：python3 tests/test_image_mgmt.py
"""
import os
import sys
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-imgmgmt-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")
# 假的镜像根目录：<root>/rootfs、<root>/rootfs-redis
FAKE_OPT = TEST_HOME / "opt"
FAKE_OPT.mkdir(parents=True, exist_ok=True)
(FAKE_OPT / "rootfs").mkdir(exist_ok=True)
(FAKE_OPT / "rootfs-redis").mkdir(exist_ok=True)
os.environ["BERTH_ROOTFS"] = str(FAKE_OPT / "rootfs")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sandbox.images as images  # noqa: E402
from state.db import init_db, list_image_builds, record_service  # noqa: E402

FAKE_SCRIPT = TEST_HOME / "fake_build.sh"
FAKE_SCRIPT.write_text(
    "#!/bin/bash\n"
    'echo "[fake] building $2"\n'
    'mkdir -p "$4"\n'
    'echo "IMAGE_PATH=$4"\n'
    "exit 0\n",
    encoding="utf-8",
)
FAKE_SCRIPT.chmod(0o755)


def main():
    init_db()

    print("=== 镜像名校验 ===")
    for bad in ("../etc", "MyImg", "a" * 40, "", "a/b", ".hidden"):
        try:
            images.validate_image_name(bad)
            raise AssertionError(f"应当拒绝: {bad!r}")
        except ValueError:
            pass
    assert images.validate_image_name("my-img_2") == "my-img_2"
    print("✔ 非法名被拒")

    print("=== 受管镜像集合 ===")
    managed = images.managed_images()
    print("  ", managed)
    assert "redis" in managed, managed
    assert "base" not in managed, "base 不是 rootfs-* 目录，不该在可删集合里"
    print("✔ managed 只含 rootfs-* 目录")

    print("=== 删除的边界 ===")
    # base 不能删
    try:
        images.delete_image("base")
        raise AssertionError("base 不该能删")
    except ValueError as e:
        print("✔ base 拒绝:", str(e)[:40], "...")

    # 不在受管集合里的不能删
    try:
        images.delete_image("nope")
        raise AssertionError("不存在的镜像不该能删")
    except ValueError as e:
        print("✔ 未受管镜像拒绝:", str(e)[:40], "...")

    # 被服务用着的不能删
    record_service(
        service_id="svc-imgtest",
        name="uses-redis",
        template="redis",
        unit="berth-svc-imgtest",
        host="localhost",
        port=16379,
        internal_port=6379,
        status="stopped",
        dir=str(TEST_HOME / "services" / "svc-imgtest"),
        persist=1,
        spec='{"name": "redis", "image": "redis", "command": ["/bin/true"], "ports": [{"internal": 6379}]}',
    )
    try:
        images.delete_image("redis")
        raise AssertionError("被占用的镜像不该能删")
    except ValueError as e:
        print("✔ 使用中拒绝:", str(e)[:50], "...")

    # 老记录（0.3.0 之前建的，没有 spec 列）也要认得出占用 —— 踩过的坑
    record_service(
        service_id="svc-old",
        name="old-style-redis",
        template="redis",
        unit="berth-svc-old",
        host="localhost",
        port=16380,
        internal_port=6379,
        status="stopped",
        dir=str(TEST_HOME / "services" / "svc-old"),
        persist=1,
    )
    used = images.images_in_use()
    assert "redis" in used, used
    assert any("old-style-redis" in v for v in used["redis"]), used
    print("✔ 老记录（无 spec）也能识别占用:", used["redis"])
    try:
        images.delete_image("redis")
        raise AssertionError("老记录占用的镜像不该能删")
    except ValueError as e:
        print("✔ 老记录占用也拒绝删除")

    print("=== 制作镜像：默认关闭（宿主 root 级操作）===")
    os.environ.pop("BERTH_ALLOW_IMAGE_BUILD", None)
    try:
        images.start_image_build(name="myimg")
        raise AssertionError("默认不该允许制作")
    except PermissionError as e:
        print("✔ 默认拒绝:", str(e)[:50], "...")

    print("=== 制作镜像：显式打开 ===")
    os.environ["BERTH_ALLOW_IMAGE_BUILD"] = "1"
    images.BUILD_SCRIPT = FAKE_SCRIPT
    # 测试环境不是 root：跳过 sudo（真机上构建脚本需要 root）
    images._build_command = lambda spec: [
        str(FAKE_SCRIPT),
        "--name", spec["name"],
        "--source", spec["source"],
    ]
    res = images.start_image_build(
        name="myimg", base="base", packages=["jq"], commands=["echo hi"]
    )
    print("  ", {k: res[k] for k in ("build_id", "name", "status")})
    assert res["status"] == "building"

    # 等构建线程结束
    import time

    for _ in range(50):
        builds = list_image_builds()
        if builds and builds[0]["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    build = list_image_builds()[0]
    print("  构建结果:", build["status"], "|", (build.get("log") or "").strip()[:60])
    assert build["status"] == "success", build
    assert build["name"] == "myimg"
    assert build["spec"]["packages"] == ["jq"], build["spec"]

    print("=== 目标路径不能越界 ===")
    target = images.target_path_for("ok-name")
    assert target.parent == images.images_root().resolve(), target
    assert target.name == "rootfs-ok-name", target
    print("  ", target)

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
