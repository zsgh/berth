"""任务 1.2 验证：run_script 的四个用例。

运行：python3 tests/test_overlayfs.py
"""
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from sandbox.overlayfs import run_script  # noqa: E402


def case(name):
    print(f"\n===== {name} =====")


def main():
    case("用例 1：hello")
    r = run_script('print("hello")', "test-1")
    print(r)
    assert r["stdout"].strip() == "hello", r
    assert r["exit_code"] == 0, r

    case("用例 2：死循环（timeout=5）")
    t0 = time.time()
    r = run_script('while True: pass', "test-2", timeout=5)
    dt = time.time() - t0
    print(r)
    assert r["exit_code"] == -1, r
    assert "超时" in r["stderr"], r
    assert dt < 20, f"超时终止太慢: {dt:.1f}s"

    case("用例 3：外网访问（host 网络）")
    r = run_script(
        'import urllib.request; print(urllib.request.urlopen("https://httpbin.org/ip", timeout=15).status)',
        "test-3",
        timeout=60,
    )
    print(r)
    assert r["exit_code"] == 0, r
    assert "200" in r["stdout"], r

    case("用例 4：写文件 → artifacts")
    r = run_script('open("/workspace/out.txt", "w").write("done"); print("ok")', "test-4")
    print(r)
    assert r["exit_code"] == 0, r
    assert "out.txt" in r["artifacts"], r

    case("用例 5：异常代码 → exit_code != 0 + traceback")
    r = run_script('raise RuntimeError("boom")', "test-5")
    print(r)
    assert r["exit_code"] != 0, r
    assert "boom" in r["stderr"], r

    case("用例 6：内存上限（256M 下分配 1G）")
    r = run_script('x = bytearray(1024 * 1024 * 1024); print("never")', "test-6", timeout=30)
    print(r)
    assert r["exit_code"] != 0, r
    assert r["stdout"].strip() == "", r

    case("用例 7：/workspace 跨多次执行保留")
    run_script('open("/workspace/persist.txt", "w").write("1")', "test-7")
    r = run_script('print(open("/workspace/persist.txt").read())', "test-7")
    print(r)
    assert r["stdout"].strip() == "1", r

    print("\n全部通过 ✔")


if __name__ == "__main__":
    main()
