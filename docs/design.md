# berth 设计文档

> 讲清楚两个核心机制：**overlayfs 沙箱**怎么搭，**systemd** 怎么配合；
> 以及踩过的坑和最终采用的兜底策略。

## 1. 总览

```
                 ┌──────────────────────────────┐
   agent ───────▶│  FastAPI (:9000)             │
   /scripts/run  │   server.py                  │
   /services     └───────┬──────────────────────┘
                         │
        ┌────────────────┼────────────────────┐
        ▼                ▼                    ▼
  sandbox/overlayfs  sandbox/service     state/db.py
  (一次性脚本)        (长驻服务)           (SQLite)
        │                │
        └───────┬────────┘
                ▼
   sudo systemd-run  →  cgroup 限额 + unshare(pid,mount,ipc,uts)
                ▼
   mount overlayfs → pivot_root → setpriv(uid=1000, 无 capability)
                ▼
   只读 rootfs + 任务可写层     （host 网络：不 unshare net）
```

两条执行路径共用同一套「沙箱构造」代码（`sandbox_shell()`），区别只在 systemd 的用法：

| | 脚本（一次性） | 服务（长驻） |
|---|---|---|
| systemd | `systemd-run --scope`（随用随停） | `systemd-run --unit=berth-svc-xxx --collect`（托管） |
| 资源 | MemoryMax / CPUQuota / TasksMax | 同左（模板里的 resources） |
| 生命周期 | `proc.wait(timeout)`，超时杀 cgroup | `systemctl stop`（限时优雅 + 强杀兜底） |
| 输出 | 重定向到文件（避免管道 EOF 阻塞） | journald |
| 目录 | `/var/lib/berth/scripts/{task}/run-{id}/`（用完即删） | `/var/lib/berth/services/{svc}/run/` |

## 2. overlayfs 沙箱怎么搭

每次执行现场构造，步骤（对应 `sandbox/overlayfs.py: sandbox_shell()`）：

```bash
# 1) 合成根：lower 只读共享，upper 任务私有（写操作 COW 到 upper）
mount -t overlay overlay -o lowerdir=/opt/berth/rootfs,upperdir=$RUN/upper,workdir=$RUN/work $RUN/newroot

# 2) 挂虚拟文件系统（/proc、/tmp、/dev 最少设备节点）
mount -t proc proc $RUN/newroot/proc
mount -t tmpfs tmpfs $RUN/newroot/tmp
mount -t tmpfs tmpfs $RUN/newroot/dev
mknod $RUN/newroot/dev/null c 1 3  # + zero / urandom / random

# 3) 绑定工作区（宿主目录 ↔ 沙箱 /workspace，跨次保留）
mount --bind /var/lib/berth/scripts/$TASK/workspace $RUN/newroot/workspace

# 4) 换根
cd $RUN/newroot && mkdir -p .oldroot && pivot_root . .oldroot && umount -l /.oldroot

# 5) 降权：uid 1000 + 清 bounding/inheritable/ambient capability
setpriv --reuid=1000 --regid=1000 --clear-groups \
        --bounding-set=-all --inh-caps=-all --ambient-caps=-all \
        /usr/bin/python3 /sandbox/run.py
```

关键点：

- **代码放哪**：写进 `upper/sandbox/run.py`。upper 是 overlay 的可写层，所以沙箱里天然能看到 `/sandbox/run.py`，且每次执行互相隔离。
- **谁能写**：执行前 `chown -R 1000:1000 upper workspace`，沙箱里 uid 1000 才能写 `/sandbox` 和 `/workspace`；rootfs 下层只读。
- **pivot_root 而不是 chroot**：pivot_root 换根后 `/` 就是合成根，旧根卸载掉，沙箱里看不到宿主文件系统。
- **namespace**：`unshare --pid --mount --ipc --uts --fork`；**不 unshare net**（host 网络，可进可出）。
- **cgroup**：`systemd-run` 包一层，`MemoryMax/CPUQuota/TasksMax` 生效；OOM 由内核按 cgroup 杀，退出码 137。
- **清理**：overlay 目录（upper/work/newroot）执行完 `rm -rf`；`/workspace` 保留（任务状态）；`systemctl kill` 保证 cgroup 内不留后台进程。

## 3. systemd 怎么配合（长驻服务）

启动（`sandbox/service.py: _start_service_impl()`）：

1. `allocate_port()` 从模板区间分配宿主端口（SQLite 记录 + socket 探测去重）
2. 准备 overlay 目录、数据卷（`svc_dir/volumes/{internal}`，`persist: true` 时停止不删）
3. 组装命令（`{port}` 等占位符），套同一套 `sandbox_shell()`
4. `systemd-run --unit=berth-{service_id} --collect -p MemoryMax=... -- unshare ...`
   - 用 `--unit`（service 单元）而不是 `--scope`，因为要长驻、要 `systemctl stop` 管理
   - `--collect`：服务结束后 systemd 自动卸载单元（停止后 `systemctl status` 会 not found）
5. 健康检查（TCP 连接宿主端口，重试到超时）
6. 记录 SQLite（`status=running`）

停止（`stop_service()`）：

1. `stop_unit()`：限时优雅 stop → 超时/没死就 `systemctl kill -s SIGKILL`（整个 cgroup）→ `reset-failed`
2. 删除 overlay 工作目录；`persist: false` 时连数据卷一起删
3. 释放端口，状态置 `stopped`

**状态恢复（daemon 重启不丢服务）**：服务的真身在 systemd（宿主级），SQLite 只存元数据。
daemon 启动时 `recover_services()`：对每条 `running` 记录 `systemctl is-active`，活着的保持 `running`（端口不动），
死了的标 `crashed` 并释放端口。所以 `pkill uvicorn` 再重启，redis 还在、还能连。

**孤儿清理**：`cleanup_orphans()` 对比 `systemctl list-units 'berth-*'` 和 SQLite：
宿主上存在但库里没有的 unit（上次 daemon 崩了留下的）→ 强杀 + reset-failed；
库里非活跃但仍占端口的记录 → 释放。注意 unit 名带后缀（`.service`/`.scope`），比对时要统一。

## 4. 踩过的坑（重要）

这些都是实际调试中撞到的，最终代码里都有对应处理：

1. **`sudo` 的 `use_pty` 让 payload 脱离进程组**
   超时后 `os.killpg()` 杀不掉沙箱里的进程（sudo 给命令分配了独立 pty 会话）。
   → 兜底必须走 cgroup：`systemctl kill --signal=SIGKILL <unit>`。

2. **`systemctl stop` 对 scope 可能卡死**
   沙箱进程有时收不到 SIGTERM（见下条），stop 任务会一直等到 `TimeoutStopSec`（默认 90s），
   daemon 启动清理时被卡住过。
   → `systemd_units.stop_unit()`：`systemctl stop` 限时（subprocess timeout）→ 到点直接强杀。

3. **`unshare` 的信号掩码问题**
   个别情况下 payload 的信号掩码是坏的，SIGTERM 不生效（表现为只能 SIGKILL）。
   → 同上，所有停止路径都做 SIGKILL 兜底。

4. **pid namespace init 被信号杀死时 `unshare` 退出码不对**
   原本 `exec setpriv ... python3` 让 payload 成为 pid ns init，被 OOM 的 SIGKILL 杀死时
   `unshare` 会返回 0（成功！）或报 `sigprocmask unblock failed`。
   → 改成 bash 留在 init 位置：`set +e; <payload>; rc=$?; exit $rc`，
   payload 被信号杀死时 bash 报告 `128+sig`，unshare 以普通退出码透传（OOM → 137）。

5. **管道 EOF 阻塞**
   脚本如果 fork 后台进程并继承 stdout 管道，`communicate()` 会等不到 EOF 而挂住。
   → 输出重定向到文件，`proc.wait(timeout)` 只等主进程；结束后 `systemctl kill` 清掉后台进程。

6. **unit 名后缀不一致**
   DB 里存 `berth-svc-x`，systemd 列出的是 `berth-svc-x.service`，孤儿清理误杀了活着的服务。
   → 比对时统一加 `.service`。

7. **systemd unit 描述里带换行**
   `systemctl list-units` 的文本输出会被描述里的换行拆散，解析出 `set`/`setpriv` 这种假 unit。
   → 改用 `--output=json`（回退解析用 unit 名正则）。

8. **FastAPI 自带遥测**
   FastAPI ≥0.14x 内置 OTel 支持：进程里一旦设置了 TracerProvider，HTTP 请求会自动产生
   `fastapi.endpoint` 等 span（无需安装额外 instrumentor）。daemon 的 trace 因此自带 HTTP 层。

## 5. 可观测性（Phoenix）

- `sandbox/tracing.py`：OTLP HTTP exporter → `http://localhost:6006/v1/traces`，
  resource 带 `openinference.project.name=berth`（Phoenix 按此归项目）
- span：
  - `script.run`：task_id / timeout / exit_code / duration / artifacts
  - `service.start`：template / service_id / port / status
  - `service.stop`：service_id / template / status
- Phoenix 没起/包缺失时自动退化为 no-op（不影响主流程）；`BERTH_TRACING=0` 可关闭
- 因为 FastAPI 原生遥测 + agent 侧 OpenAI instrumentor，Phoenix 里能看到
  HTTP → agent → sandbox 的完整链路

## 6. 数据模型（SQLite）

```sql
runs(task_id PK, code, stdout, stderr, exit_code, duration_ms, artifacts JSON,
     created_at, finished_at)                       -- 脚本执行历史
services(service_id PK, name, template, unit, host, port, internal_port,
         status, dir, persist, created_at, updated_at)   -- 服务元数据
ports(service_id PK, internal_port, external_port UNIQUE, allocated_at) -- 端口分配
```

- WAL 模式 + 每次操作独立连接（uvicorn 多线程安全）
- 表结构懒建（`connect()` 首次调用自动建表），避免「先写后建表」丢数据
