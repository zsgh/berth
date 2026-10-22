"""镜像名解析验证：base / 自动发现 / 绝对路径 / 未知报错 / images.yaml 覆盖。

运行：python3 tests/test_images.py
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sandbox.images as images  # noqa: E402
from sandbox.paths import ROOTFS  # noqa: E402


def main():
    # 1) base = BERTH_ROOTFS
    assert images.resolve_image("base") == Path(ROOTFS)
    print("✔ base →", ROOTFS)

    # 2) 自动发现：setup_rootfs.sh 造的 rootfs-* 兄弟目录
    avail = images.available_images()
    print("可用镜像:", sorted(avail))
    assert "base" in avail
    if Path(str(ROOTFS) + "-redis").is_dir():
        assert "redis" in avail
        assert images.resolve_image("redis") == Path(str(ROOTFS) + "-redis")
        print("✔ 自动发现 redis")

    # 3) 绝对路径直接放行
    assert images.resolve_image(ROOTFS) == Path(ROOTFS)
    print("✔ 绝对路径")

    # 4) 未知名字报错，且列出可用项
    try:
        images.resolve_image("nope")
        raise AssertionError("应当报错")
    except ValueError as e:
        assert "可用" in str(e), e
        print("✔ 未知镜像：", str(e)[:60], "...")

    # 5) images.yaml（BERTH_IMAGES_FILE）覆盖自动发现
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "images.yaml"
        f.write_text(f"images:\n  custom: {ROOTFS}\n", encoding="utf-8")
        os.environ["BERTH_IMAGES_FILE"] = str(f)
        importlib.reload(images)
        table = images.available_images()
        assert table == {"custom": str(ROOTFS)}, table
        assert images.resolve_image("custom") == Path(ROOTFS)
        try:
            images.resolve_image("base")
            raise AssertionError("配置存在时不应再自动发现 base")
        except ValueError:
            pass
        print("✔ images.yaml 覆盖生效")
        del os.environ["BERTH_IMAGES_FILE"]
        importlib.reload(images)  # 还原

    # 6) 目录大小：算的是目录内容，不是整个文件系统
    #    （踩过的坑：shutil.disk_usage(path).used 对同一个盘上的目录全返回一样的数）
    from sandbox.paths import dir_size

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "img-a"
        base.mkdir()
        (base / "a.bin").write_bytes(b"x" * (1024 * 1024))       # 1 MiB
        other = Path(td) / "img-b"
        other.mkdir()
        (other / "b.bin").write_bytes(b"y" * (4 * 1024 * 1024))  # 4 MiB
        size_a = dir_size(base)
        size_b = dir_size(other)
        assert size_a and size_b, (size_a, size_b)
        assert size_a != size_b, f"两个目录大小不应相同：{size_a} vs {size_b}"
        assert size_b > size_a, (size_a, size_b)
        print(f"✔ 目录大小各自独立：{size_a} vs {size_b}")

    print("全部通过 ✔")


if __name__ == "__main__":
    main()
