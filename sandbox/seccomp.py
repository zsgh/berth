"""沙箱 seccomp 过滤器（M3，宿主侧）。

在**真正跑 payload 之前**给沙箱进程装一个"拒绝清单"式的 seccomp 过滤器，
挡住沙箱里用不上的危险系统调用（挂载、ptrace、模块、kexec、namespace 逃逸…）。

为什么不用 systemd 的 `SystemCallFilter=`：
那个属性作用于**整个 unit**，而沙箱的构造过程（overlay 挂载、pivot_root）本身就要用
mount/pivot_root → 会被自己的过滤器拦死。所以过滤器装在 payload 进程上
（exec 之后仍然继承 ✅）。

怎么保证 syscall 编号是对的：
**不硬编码**。宿主上用 libseccomp（systemd 的依赖，基本都有）按**名字**解析，
解析不到就不装这个过滤器（只记日志，不影响功能）—— 宁可没有，也不能拦错系统调用。

生成物：
    seccomp.bpf       —— 编译好的 BPF 程序（宿主生成，架构相关）
    seccomp_wrap.py   —— 装过滤器 + exec payload 的小 loader（纯 Python + ctypes）
两个文件都写进 overlay 的 /sandbox/，沙箱里直接用（不依赖镜像里装什么包）。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import struct
from pathlib import Path

log = logging.getLogger(__name__)

# 拒绝清单（按名字，运行时解析成编号）
DENY_NAMES = [
    # 挂载 / namespace（逃逸 + 破坏宿主）
    "mount", "umount2", "pivot_root", "chroot", "open_tree", "move_mount",
    "fsopen", "fsconfig", "fsmount", "fspick", "mount_setattr",
    "setns", "unshare",
    # 进程窥探（逃逸原语）
    "ptrace", "process_vm_readv", "process_vm_writev", "kcmp",
    "pidfd_open", "pidfd_getfd", "pidfd_send_signal",
    # 内核模块 / BPF / seccomp
    "init_module", "finit_module", "delete_module", "kexec_load",
    "bpf", "userfaultfd",
    # 系统操纵
    "reboot", "swapon", "swapoff", "settimeofday", "clock_settime",
    "clock_adjtime", "adjtimex", "sethostname", "setdomainname",
    "iopl", "ioperm", "acct", "quotactl", "nfsservctl", "vhangup", "tuxcall",
    # 文件系统句柄（绕过权限检查）
    "open_by_handle_at", "name_to_handle_at",
    # 内核密钥
    "add_key", "keyctl", "request_key",
]

# ── BPF 常量 ──
BPF_LD, BPF_JMP, BPF_RET = 0x00, 0x05, 0x06
BPF_W, BPF_ABS, BPF_JEQ, BPF_K = 0x00, 0x20, 0x10, 0x00
OFF_NR, OFF_ARCH = 0, 4
AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
EPERM = 1

PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2


def enabled() -> bool:
    """是否启用 seccomp 过滤（BERTH_SECCOMP=0 关闭）。"""
    raw = os.environ.get("BERTH_SECCOMP")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _host_arch() -> int:
    machine = os.uname().machine
    if machine in ("x86_64", "amd64"):
        return AUDIT_ARCH_X86_64
    if machine in ("aarch64", "arm64"):
        return AUDIT_ARCH_AARCH64
    return 0


def resolve_names(names: list[str]) -> dict[str, int]:
    """用 libseccomp 把 syscall 名字解析成编号（解析不到的跳过）。"""
    out: dict[str, int] = {}
    try:
        lib = ctypes.CDLL("libseccomp.so.2")
    except OSError:
        try:
            path = ctypes.util.find_library("seccomp")
            if not path:
                return out
            lib = ctypes.CDLL(path)
        except OSError:
            return out
    try:
        lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
        lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    except AttributeError:
        return out
    for name in names:
        try:
            nr = int(lib.seccomp_syscall_resolve_name(name.encode()))
        except Exception:
            continue
        if nr >= 0:
            out[name] = nr
    return out


def _bpf_ins(code: int, jt: int = 0, jf: int = 0, k: int = 0) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def build_bpf(deny: dict[str, int], arch: int) -> bytes:
    """构造 BPF：命中拒绝清单 → EPERM；其它 → 放行。

    结构：
        ld  [arch] ; jne ARCH → ret ALLOW（架构不符就放行，别误伤）
        ld  [nr]
        jeq nr → ret EPERM   （每个要拒的 nr 一对指令）
        ret ALLOW
    """
    if not arch or not deny:
        return b""
    prog = b""
    prog += _bpf_ins(BPF_LD | BPF_W | BPF_ABS, k=OFF_ARCH)
    prog += _bpf_ins(BPF_JMP | BPF_JEQ | BPF_K, jt=1, jf=0, k=arch)
    prog += _bpf_ins(BPF_RET | BPF_K, k=SECCOMP_RET_ALLOW)
    prog += _bpf_ins(BPF_LD | BPF_W | BPF_ABS, k=OFF_NR)
    for nr in sorted(set(deny.values())):
        prog += _bpf_ins(BPF_JMP | BPF_JEQ | BPF_K, jt=0, jf=1, k=nr)
        prog += _bpf_ins(BPF_RET | BPF_K, k=SECCOMP_RET_ERRNO | EPERM)
    prog += _bpf_ins(BPF_RET | BPF_K, k=SECCOMP_RET_ALLOW)
    return prog


WRAPPER = '''#!/usr/bin/python3
"""装 seccomp 过滤器然后 exec payload（由 berth 写进 overlay 的 /sandbox/）。"""
import ctypes
import ctypes.util
import os
import sys

PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
BLOB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seccomp.bpf")


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]


def install():
    try:
        with open(BLOB, "rb") as f:
            prog = f.read()
    except OSError:
        return False
    if not prog or len(prog) % 8:
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    except OSError:
        return False
    try:
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            return False
        n = len(prog) // 8
        buf = (SockFilter * n)()
        ctypes.memmove(ctypes.byref(buf), prog, len(prog))
        fprog = SockFprog(n, ctypes.pointer(buf))
        return libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER,
                          ctypes.byref(fprog), 0, 0) == 0
    except Exception:
        return False


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        return 2
    try:
        install()
    except Exception:
        pass
    try:
        os.execvp(argv[0], argv)
    except OSError:
        return 127
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_filter(target_dir: Path) -> bool:
    """把过滤器 + loader 写进 overlay 的 /sandbox/。成功返回 True。"""
    if not enabled():
        return False
    arch = _host_arch()
    if not arch:
        return False
    deny = resolve_names(DENY_NAMES)
    if not deny:
        log.info("解析不到 syscall 编号（缺 libseccomp？）→ 不装 seccomp 过滤器")
        return False
    prog = build_bpf(deny, arch)
    if not prog:
        return False
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "seccomp.bpf").write_bytes(prog)
        (target_dir / "seccomp_wrap.py").write_text(WRAPPER, encoding="utf-8")
        os.chmod(target_dir / "seccomp_wrap.py", 0o755)
    except OSError as e:
        log.warning("写 seccomp 过滤器失败: %s", e)
        return False
    log.debug("seccomp 过滤器就绪：拒 %d 个 syscall", len(deny))
    return True


def wrap_command(argv: list[str], python: str = "/usr/bin/python3") -> list[str] | None:
    """把 payload 命令包一层 seccomp loader（没启用 → None）。"""
    if not enabled():
        return None
    return [python, "/sandbox/seccomp_wrap.py", "--", *argv]
