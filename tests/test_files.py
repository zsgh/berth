"""B3 验证：文件进出（上传 / 下载 / 文件树 / 只读挂载白名单）。

不需要 systemd / root。

运行：python3 tests/test_files.py
"""
import io
import os
import sys
import tarfile
import tempfile
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="berth-files-test-"))
os.environ["BERTH_HOME"] = str(TEST_HOME)
os.environ["BERTH_DB"] = str(TEST_HOME / "daemon.db")
MOUNT_ROOT = TEST_HOME / "mounts"
MOUNT_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["BERTH_MOUNT_ROOTS"] = str(MOUNT_ROOT)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox.files import (  # noqa: E402
    file_tree,
    make_tar,
    read_file,
    safe_join,
    task_workspace,
    upload_files,
    validate_task_id,
)
from sandbox.mounts import normalize_mounts, resolve_source  # noqa: E402


def main():
    print("=== task_id 校验（不能带路径）===")
    assert validate_task_id("proj-1-run-2") == "proj-1-run-2"
    for bad in ("../etc", "a/b", "", "x" * 200, ".."):
        try:
            validate_task_id(bad)
            raise AssertionError(f"应当拒绝: {bad!r}")
        except ValueError:
            pass
    print("✔ 非法 task_id 被拒绝")

    print("=== 上传多文件 ===")
    result = upload_files(
        "demo",
        files=[
            ("tests/test_a.py", b"def test_a():\n    assert True\n"),
            ("tests/test_b.py", b"def test_b():\n    assert True\n"),
            ("README.md", b"# hello\n"),
        ],
    )
    print("  ", result["files"])
    assert "tests/test_a.py" in result["files"]
    ws = task_workspace("demo")
    assert (ws / "tests" / "test_a.py").exists()
    print("✔ 多文件上传（保留子目录）")

    print("=== 文件树（大小 / mime）===")
    tree = file_tree("demo")
    for entry in tree:
        print("  ", entry["path"], entry["size"], entry["mime"])
    by_path = {e["path"]: e for e in tree}
    assert by_path["README.md"]["mime"], by_path["README.md"]
    assert by_path["README.md"]["size"] == 8
    assert by_path["tests"]["is_dir"] is True
    print("✔ 文件树 OK")

    print("=== 读取 / 下载 ===")
    assert read_file("demo", "README.md") == b"# hello\n"
    print("✔ 读取文件 OK")

    print("=== 打包下载（tar）===")
    data = make_tar("demo")
    assert data[:2] != b"" and len(data) > 0
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = tar.getnames()
    print("  ", names)
    assert any(n.endswith("README.md") for n in names)
    print("✔ tar 打包 OK")

    print("=== 上传 tar 包（自动解包）===")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        payload = b"print('from tar')\n"
        info = tarfile.TarInfo("pkg/script.py")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    result = upload_files("demo2", archive=buf.getvalue())
    print("  ", result["files"])
    assert "pkg/script.py" in result["files"]
    assert read_file("demo2", "pkg/script.py") == b"print('from tar')\n"
    print("✔ tar 上传解包 OK")

    print("=== 路径穿越防护 ===")
    ws = task_workspace("demo")
    for bad in ("../escape", "tests/../../escape"):
        try:
            safe_join(ws, bad)
            raise AssertionError(f"应当拒绝: {bad}")
        except ValueError:
            pass
    print("✔ 路径穿越被拒绝")

    print("=== 只读挂载白名单 ===")
    inside = MOUNT_ROOT / "app"
    inside.mkdir(exist_ok=True)
    mounts = normalize_mounts([{"source": str(inside), "target": "/app"}])
    print("  ", mounts)
    assert mounts[0]["read_only"] is True
    assert mounts[0]["target"] == "/app"

    for bad in ("/etc", str(TEST_HOME / ".."), "/var/lib/berth"):
        try:
            resolve_source(bad)
            raise AssertionError(f"应当拒绝越权路径: {bad}")
        except ValueError:
            print("  ✔ 拒绝:", bad)

    print("全部通过 ✔")


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
