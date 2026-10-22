"""overlayfs 沙箱执行器（任务 1.2 + B2/B3/B4 扩展）。

把 `sandbox_overlay.sh` 的核心逻辑改写成可复用函数。一次执行的全流程：

    /opt/berth/rootfs (只读 lower)
        + 任务独立 upper (可写层，COW)
        + overlayfs 合成 → pivot_root 换根
        + unshare(pid/mount/ipc/uts) 隔离（**保留 host 网络，不 unshare net**）
        + setpriv 降权到 uid 1000 + 清空 capability
        + systemd-run --scope 包 cgroup（内存 / CPU / 进程数限额）
        + 可选：systemd IPAddressDeny/Allow 做网络白名单（B2）

与宿主共享 /workspace（bind mount），执行结束后列出其中的文件作为 artifacts。

B3 扩展：可以额外把宿主目录**只读**挂进沙箱（白名单内，见 sandbox/mounts.py）。
B4 扩展：执行可以拆成 prepare / spawn / wait 三步，支持异步跑 + 增量读输出。
"""
from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sandbox.images import resolve_image
from sandbox.mounts import normalize_mounts
from sandbox.network import normalize_policy, systemd_network_properties
from sandbox.paths import BASE_DIR, ROOTFS, SANDBOX_GID, SANDBOX_UID, ensure_base_dir, priv_cmd, sudo
from sandbox.systemd_units import kill_unit, stop_unit
from sandbox.tracing import span

MAX_OUTPUT = 10_000

# 沙箱内最终降权执行的入口（python3 在 rootfs 里的路径）
SETPRIV = "setpriv --reuid={uid} --regid={gid} --clear-groups --bounding-set=-all --inh-caps=-all --ambient-caps=-all".format(
    uid=SANDBOX_UID, gid=SANDBOX_GID
)

# /dev/shm 大小（Chromium / Playwright 需要，默认 1G 上限，不预占内存）
DEFAULT_SHM_SIZE = "1g"


def sandbox_shell(
    *,
    upper: Path,
    work: Path,
    newroot: Path,
    lower: str = ROOTFS,
    binds: dict[str, Path] | None = None,
    binds_ro: dict[str, Path] | None = None,
    env: dict[str, str] | None = None,
    entry: str,
    shm_size: str = DEFAULT_SHM_SIZE,
) -> str:
    """生成沙箱内部执行的 shell 片段（以 root 在 mount namespace 里跑）。

    步骤与 sandbox_overlay.sh 一致：overlay 合成 → 挂虚拟 fs → pivot_root
    → 降权执行 entry。`binds` 是 {容器内路径: 宿主路径}（可写），
    `binds_ro` 是只读挂载（B3）。
    """
    q = shlex.quote
    lines = [
        "set -e",
        # 1. overlayfs 合成：lower 只读 + upper 可写
        f"mount -t overlay overlay -o lowerdir={q(str(lower))},upperdir={q(str(upper))},workdir={q(str(work))} {q(str(newroot))}",
        # 2. 挂虚拟文件系统
        f"mount -t proc proc {q(str(newroot))}/proc",
        f"mount -t tmpfs tmpfs {q(str(newroot))}/tmp",
        f"mount -t tmpfs tmpfs {q(str(newroot))}/dev",
        # 2b. /dev/shm 单独给一块 tmpfs（Chromium 默认往这写，64M 不够用）
        f"mkdir -p {q(str(newroot) + '/dev/shm')}",
        f"mount -t tmpfs -o size={q(shm_size)} tmpfs {q(str(newroot) + '/dev/shm')}",
    ]
    for dev, major, minor in (("null", 1, 3), ("zero", 1, 5), ("urandom", 1, 9), ("random", 1, 8)):
        lines += [
            f"mknod {q(str(newroot))}/dev/{dev} c {major} {minor}",
            f"chmod 666 {q(str(newroot))}/dev/{dev}",
        ]
    # 3. bind mount 外部目录（workspace / 数据卷）
    for inner, host in (binds or {}).items():
        lines += [
            f"mkdir -p {q(str(newroot) + inner)}",
            f"mount --bind {q(str(host))} {q(str(newroot) + inner)}",
        ]
    # 3b. 只读 bind mount（B3：被测代码目录等）
    for inner, host in (binds_ro or {}).items():
        lines += [
            f"mkdir -p {q(str(newroot) + inner)}",
            f"mount --bind {q(str(host))} {q(str(newroot) + inner)}",
            f"mount -o remount,bind,ro {q(str(newroot) + inner)}",
        ]
    # 4. pivot_root 换根
    lines += [
        f"cd {q(str(newroot))}",
        "mkdir -p .oldroot",
        "pivot_root . .oldroot",
        "/bin/umount -l /.oldroot",
    ]
    # 5. 环境变量 + 降权执行
    for k, v in (env or {}).items():
        lines.append(f"export {k}={q(v)}")
    lines.append(entry)
    return "\n".join(lines)


def systemd_run_cmd(
    inner: str,
    *,
    unit: str,
    scope: bool = False,
    memory_mb: int = 256,
    cpu_percent: int = 50,
    tasks_max: int = 64,
    network: dict | None = None,
) -> list[str]:
    """组装 `sudo systemd-run` 命令。

    scope=False → 长驻服务：`systemd-run --unit=berth-xxx`（交给 systemd 托管）。
    scope=True  → 一次性脚本：`systemd-run --scope`，前台执行随用随停。
    两者都套 cgroup 限额，并用 unshare 隔离 pid/mount/ipc/uts（保留 host 网络）。

    network（B2）：非 host 时加 `IPAddressDeny=any` / `IPAddressAllow=...`。
    """
    cmd = priv_cmd("systemd-run")
    if scope:
        cmd.append("--scope")
    cmd += [
        f"--unit={unit}",
        "--collect",
        "-p", f"MemoryMax={memory_mb}M",
        "-p", f"CPUQuota={cpu_percent}%",
        "-p", f"TasksMax={tasks_max}",
    ]
    cmd += systemd_network_properties(network)
    cmd += [
        "--",
        "unshare", "--propagation", "private", "--pid", "--mount", "--ipc", "--uts", "--fork",
        "bash", "-c", inner,
    ]
    return cmd


def _strip_noise(stderr: str) -> str:
    """过滤 systemd-run 的例行提示行。"""
    keep = []
    for line in stderr.splitlines():
        if line.startswith("Running scope as unit") or line.startswith("Running as unit"):
            continue
        keep.append(line)
    # 去掉结尾多余的空行
    while keep and not keep[-1].strip():
        keep.pop()
    return "\n".join(keep)


def _kill_scope(proc: subprocess.Popen, unit: str) -> None:
    """超时兜底：杀进程组 + 按 cgroup 强杀，确保不留残留。

    注意：sudo 的 use_pty 会让 payload 脱离原进程组，killpg 杀不干净；
    unshare 还可能把信号掩码弄坏导致 SIGTERM 失效，所以直接上 SIGKILL，
    且 `systemctl stop` 必须限时（见 systemd_units.stop_unit）。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    kill_unit(unit)
    stop_unit(unit, graceful_timeout=5)


def list_artifacts(workspace: Path) -> list[str]:
    """列出 workspace 里的文件（相对路径，排序）。"""
    if not workspace.exists():
        return []
    return sorted(
        str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file()
    )


# 支持的语言 → (解释器, 文件后缀)。其它解释器用 shebang 指定（需在镜像里存在）。
LANGUAGES: dict[str, tuple[str, str]] = {
    "python": ("/usr/bin/python3", ".py"),
    "python3": ("/usr/bin/python3", ".py"),
    "bash": ("/bin/bash", ".sh"),
    "sh": ("/bin/sh", ".sh"),
}


def _resolve_interpreter(code: str, language: str | None) -> tuple[list[str], str, str]:
    """决定用什么跑 code：language 参数 > 首行 shebang > 默认 python。"""
    if language:
        key = str(language).lower()
        if key in LANGUAGES:
            interp, ext = LANGUAGES[key]
            return [interp], ext, key
        raise ValueError(
            f"不支持的语言: {language}（支持: {sorted(LANGUAGES)}；"
            f"其它解释器可在代码首行写 shebang，前提是镜像里有）"
        )
    first = (code.splitlines() or [""])[0]
    if first.startswith("#!"):
        tokens = first[2:].strip().split()
        if tokens:
            if tokens[0].endswith("/env") and len(tokens) >= 2:
                interp = [tokens[0], tokens[1]]
            else:
                interp = [tokens[0]]
            return interp, ".sh", "shebang:" + " ".join(interp)
    return ["/usr/bin/python3"], ".py", "python"


# ─────────────────────────── 准备 / 执行（B4 拆分） ───────────────────────────


@dataclass
class PreparedRun:
    """一次执行的全部准备工作（还没跑）。"""

    task_id: str
    run_id: str
    unit: str
    argv: list[str]
    lang_label: str | None
    audit_code: str
    image: str
    run_dir: Path
    script_dir: Path
    workspace: Path
    inner: str
    cmd: list[str]
    timeout: int
    memory_mb: int
    cpu_percent: int
    tasks_max: int
    network: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    mounts: list[dict] = field(default_factory=list)
    owner: str | None = None      # 归属的 user（授权靠它）
    out_path: Path | None = None
    err_path: Path | None = None
    started_at: float = 0.0


def prepare_run(
    code: str | None = None,
    cmd: list[str] | None = None,
    language: str | None = None,
    image: str = "base",
    task_id: str | None = None,
    timeout: int = 30,
    memory_mb: int = 256,
    cpu_percent: int = 50,
    tasks_max: int = 64,
    network: dict | None = None,
    mounts: list | None = None,
    labels: dict | None = None,
    owner: str | None = None,
    output_dir: Path | None = None,
) -> PreparedRun:
    """校验参数 + 准备 overlay 目录 + 组装命令（不执行）。"""
    # ─── 参数校验：code/cmd 二选一 ───
    if (code is None) == (cmd is None):
        raise ValueError("code 和 cmd 必须（且只能）给一个")
    if cmd is not None:
        if language:
            raise ValueError("cmd 模式不需要 language（直接给完整 argv）")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, (str, int, float)) for x in cmd):
            raise ValueError('cmd 必须是非空数组，如 ["/bin/ls", "-la"]')
        argv = [str(x) for x in cmd]
        script_name = None
        lang_label = None
        audit_code = "$ " + " ".join(shlex.quote(str(x)) for x in cmd)
    else:
        interp, ext, lang_label = _resolve_interpreter(code, language)
        script_name = f"run{ext}"
        argv = [*interp, f"/sandbox/{script_name}"]
        audit_code = code

    image_path = resolve_image(image)
    policy = normalize_policy(network)
    mount_list = normalize_mounts(mounts)

    from sandbox.files import validate_task_id

    task_id = validate_task_id(str(task_id)) if task_id else f"auto-{uuid.uuid4().hex[:12]}"
    ensure_base_dir()

    run_id = uuid.uuid4().hex[:8]
    script_dir = BASE_DIR / "scripts" / task_id
    run_dir = script_dir / f"run-{run_id}"
    upper, work, newroot = run_dir / "upper", run_dir / "work", run_dir / "newroot"
    workspace = script_dir / "workspace"

    # ─── 准备 overlay 目录 ───
    for d in (upper / "sandbox", work, newroot, workspace):
        d.mkdir(parents=True, exist_ok=True)

    # 源码写进 upper 层的 /sandbox/（自动成为 overlay 的一部分）
    if script_name:
        (upper / "sandbox" / script_name).write_text(code, encoding="utf-8")

    # upper / workspace 归 uid 1000，沙箱内才能写
    sudo("chown", "-R", f"{SANDBOX_UID}:{SANDBOX_GID}", upper, workspace)

    binds = {"/workspace": workspace}
    binds_ro: dict[str, Path] = {}
    for m in mount_list:
        binds_ro[m["target"]] = Path(m["source"])

    inner = sandbox_shell(
        upper=upper,
        work=work,
        newroot=newroot,
        lower=str(image_path),
        binds=binds,
        binds_ro=binds_ro,
        # 不用 exec、不用 set -e 收尾：让 bash 作为 pid ns 的 init 捕获子进程状态后
        # 以普通 exit 返回。否则 payload 被信号打死（如 cgroup OOM 的 SIGKILL）时，
        # unshare 会把 init 的信号死亡错误地报告成 rc=0。
        entry=(
            "set +e\n"
            f"{SETPRIV} {shlex.join(argv)}\n"
            "rc=$?\n"
            "exit $rc"
        ),
    )

    unit = f"berth-run-{run_id}"
    run_cmd = systemd_run_cmd(
        inner,
        unit=unit,
        scope=True,
        memory_mb=memory_mb,
        cpu_percent=cpu_percent,
        tasks_max=tasks_max,
        network=policy,
    )

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path, err_path = output_dir / "stdout.txt", output_dir / "stderr.txt"
    else:
        out_path, err_path = run_dir / "stdout.txt", run_dir / "stderr.txt"

    return PreparedRun(
        task_id=task_id,
        run_id=run_id,
        unit=unit,
        argv=argv,
        lang_label=lang_label,
        audit_code=audit_code,
        image=image,
        run_dir=run_dir,
        script_dir=script_dir,
        workspace=workspace,
        inner=inner,
        cmd=run_cmd,
        timeout=timeout,
        memory_mb=memory_mb,
        cpu_percent=cpu_percent,
        tasks_max=tasks_max,
        network=policy,
        labels=dict(labels or {}),
        mounts=mount_list,
        owner=owner,
        out_path=out_path,
        err_path=err_path,
    )


def spawn(prepared: PreparedRun) -> subprocess.Popen:
    """启动执行（不等待）。输出重定向到文件（避免管道 EOF 阻塞）。"""
    assert prepared.out_path and prepared.err_path
    prepared.started_at = time.time()
    outf = open(prepared.out_path, "w")
    errf = open(prepared.err_path, "w")
    proc = subprocess.Popen(
        prepared.cmd, stdout=outf, stderr=errf, start_new_session=True
    )
    # 文件句柄交给子进程，父进程这边可以关掉（子进程已 dup）
    outf.close()
    errf.close()
    return proc


def wait_run(prepared: PreparedRun, proc: subprocess.Popen, timeout: int | None = None) -> dict:
    """等待执行结束，返回结果字典（不删目录、不写 DB）。"""
    timeout = prepared.timeout if timeout is None else timeout
    started = prepared.started_at or time.time()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_scope(proc, prepared.unit)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # 极端兜底：不让 API 永久挂住
            pass
        exit_code = -1
    # scope 里可能还有主进程 fork 出来的后台进程，一并清掉（一次性脚本不留残留）
    kill_unit(prepared.unit)
    duration = round(time.time() - started, 3)

    stdout = _read_text(prepared.out_path)
    stderr = _strip_noise(_read_text(prepared.err_path))
    if timed_out:
        stderr = (stderr + f"\nERROR: 超时 {timeout}s，已强制终止").strip()

    artifacts = list_artifacts(prepared.workspace)

    status = "finished"
    if timed_out:
        status = "timeout"
    elif _was_cancelled(prepared.task_id):
        status = "killed"

    return {
        "stdout": stdout[:MAX_OUTPUT],
        "stderr": stderr,
        "exit_code": exit_code,
        "duration": duration,
        "artifacts": artifacts,
        "task_id": prepared.task_id,
        "image": prepared.image,
        "language": prepared.lang_label,
        "status": status,
        "network": prepared.network,
        "labels": prepared.labels,
        "owner": prepared.owner,
        "timeout": timeout,
        "started_at": prepared.started_at,
    }


def _read_text(path: Path | None) -> str:
    if not path:
        return ""
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _was_cancelled(task_id: str) -> bool:
    """这次执行是不是被 cancel 掉的（异步执行用）。"""
    from sandbox import active as active_mod

    run = active_mod.get(task_id)
    return bool(run and run.cancelled)


def run_script(
    code: str | None = None,
    cmd: list[str] | None = None,
    language: str | None = None,
    image: str = "base",
    task_id: str | None = None,
    timeout: int = 30,
    memory_mb: int = 256,
    cpu_percent: int = 50,
    keep_dirs: bool = False,
    tasks_max: int = 64,
    network: dict | None = None,
    mounts: list | None = None,
    labels: dict | None = None,
    owner: str | None = None,
) -> dict:
    """在 overlayfs 沙箱里跑一段代码或一条命令（带 Phoenix trace 埋点）。

    code 和 cmd 二选一：
        code:  源码字符串；language（python/bash/sh）或首行 shebang 决定解释器，默认 python
        cmd:   直接执行的 argv（等价 `docker run <image> <cmd...>`，程序需在镜像里存在）
    image:     镜像名（base/redis/mysql/自定义，见 sandbox/images.py）
    task_id:   任务标识（缺省自动生成 uuid）；同一 task_id 共享 /workspace
    timeout:   超时秒数，超时强制终止并返回 exit_code=-1
    memory_mb / cpu_percent: cgroup 限额
    keep_dirs: 保留本次 overlay 目录（默认执行完即删）
    network:   网络策略 {"mode": host|none|allow, "allow": [...]}（B2）
    mounts:    只读挂载列表（白名单内，B3）
    labels:    标签（B12）

    返回：
        {"stdout", "stderr", "exit_code", "duration", "artifacts", "task_id", "image", "language"}
    """
    with span("script.run", task_id=str(task_id or ""), timeout=timeout, image=image) as s:
        prepared = prepare_run(
            code,
            cmd=cmd,
            language=language,
            image=image,
            task_id=task_id,
            timeout=timeout,
            memory_mb=memory_mb,
            cpu_percent=cpu_percent,
            tasks_max=tasks_max,
            network=network,
            mounts=mounts,
            labels=labels,
            owner=owner,
        )
        proc = spawn(prepared)
        result = wait_run(prepared, proc)
        s.set_attribute("exit_code", result["exit_code"])
        s.set_attribute("duration", result["duration"])
        s.set_attribute("artifacts", ",".join(result["artifacts"][:50]))

        # 审计记录（记录失败不影响执行结果）
        try:
            from state.db import record_run

            record_run(prepared.task_id, prepared.audit_code, result)
        except Exception:
            logging.getLogger(__name__).exception(
                "record_run failed: task_id=%s", prepared.task_id
            )
        return result


def exec_command(
    argv: list[str],
    *,
    image: str = "base",
    binds: dict[str, Path] | None = None,
    binds_ro: dict[str, Path] | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 60,
    memory_mb: int = 256,
    cpu_percent: int = 50,
    tasks_max: int = 64,
    network: dict | None = None,
    workdir: Path | None = None,
) -> dict:
    """在沙箱里执行一条 argv（服务 init 用）。

    和 run_script 的区别：不写脚本文件，直接执行；返回
    {"stdout", "stderr", "exit_code", "duration"}。
    """
    image_path = resolve_image(image)
    ensure_base_dir()
    run_id = uuid.uuid4().hex[:8]
    base = workdir or (BASE_DIR / "exec")
    run_dir = base / f"exec-{run_id}"
    upper, work, newroot = run_dir / "upper", run_dir / "work", run_dir / "newroot"
    for d in (upper, work, newroot):
        d.mkdir(parents=True, exist_ok=True)
    sudo("chown", "-R", f"{SANDBOX_UID}:{SANDBOX_GID}", upper)

    inner = sandbox_shell(
        upper=upper,
        work=work,
        newroot=newroot,
        lower=str(image_path),
        binds=binds,
        binds_ro=binds_ro,
        env=env,
        entry=(
            "set +e\n"
            f"{SETPRIV} {shlex.join([str(a) for a in argv])}\n"
            "rc=$?\n"
            "exit $rc"
        ),
    )
    unit = f"berth-run-{run_id}"
    run_cmd = systemd_run_cmd(
        inner,
        unit=unit,
        scope=True,
        memory_mb=memory_mb,
        cpu_percent=cpu_percent,
        tasks_max=tasks_max,
        network=network,
    )
    out_path, err_path = run_dir / "stdout.txt", run_dir / "stderr.txt"
    started = time.time()
    timed_out = False
    with open(out_path, "w") as outf, open(err_path, "w") as errf:
        proc = subprocess.Popen(run_cmd, stdout=outf, stderr=errf, start_new_session=True)
        try:
            proc.wait(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_scope(proc, unit)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            exit_code = -1
    kill_unit(unit)
    duration = round(time.time() - started, 3)
    stdout = _read_text(out_path)
    stderr = _strip_noise(_read_text(err_path))
    if timed_out:
        stderr = (stderr + f"\nERROR: 超时 {timeout}s，已强制终止").strip()
    sudo("rm", "-rf", run_dir, check=False)
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "duration": duration,
        "status": "timeout" if timed_out else "finished",
    }


# ─────────────────────────── 取消（B4） ───────────────────────────


def cancel_active(task_id: str) -> dict | None:
    """终止一个正在跑的执行（返回被取消的运行信息；没在跑返回 None）。"""
    from sandbox import active as active_mod

    run = active_mod.get(task_id)
    if run is None:
        return None
    run.cancelled = True
    run.status = "killed"
    try:
        os.killpg(os.getpgid(run.proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    kill_unit(run.unit)
    stop_unit(run.unit, graceful_timeout=5)
    return run.to_dict()


__all__ = [
    "MAX_OUTPUT",
    "SETPRIV",
    "PreparedRun",
    "cancel_active",
    "exec_command",
    "list_artifacts",
    "prepare_run",
    "run_script",
    "sandbox_shell",
    "spawn",
    "systemd_run_cmd",
    "wait_run",
]
