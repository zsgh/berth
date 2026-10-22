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
        /usr/bin/python3 /sandbox/run.py   # 默认；也可以 bash/sh/shebang/cmd（见下）
```

关键点：

- **代码放哪**：写进 `upper/sandbox/run.<ext>`（python→run.py、bash/sh→run.sh；`cmd` 模式直接执行 argv）。
  upper 是 overlay 的可写层，所以沙箱里天然能看到 `/sandbox/`，且每次执行互相隔离。
  解释器选择：`language` 参数 → 首行 shebang → 默认 python；镜像是名字（`sandbox/images.py`）。
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

**同名幂等**：`PUT /services/{name}` 按名字查现有实例——
在跑就直接返回（`reused=true`）、停了就原样再起（复用数据卷）、模板不同报 409。
名字即身份，重复调用不会重复创建实例（agent 重试友好）。

停止（`stop_service()`）：

1. `stop_unit()`：限时优雅 stop → 超时/没死就 `systemctl kill -s SIGKILL`（整个 cgroup）→ `reset-failed`
2. 删除 overlay 工作目录；`persist: false` 时连数据卷一起删
3. 释放端口，状态置 `stopped`

**状态恢复（daemon 重启不丢服务）**：服务的真身在 systemd（宿主级），SQLite 只存元数据。
daemon 启动时 `recover_services()`：对每条 `running` 记录 `systemctl is-active`，活着的保持 `running`（端口不动），
死了的标 `crashed` 并释放端口。所以 `pkill uvicorn` 再重启，redis 还在、还能连。

**孤儿清理**：`cleanup_orphans()` 对比 systemd 和 SQLite。注意只认 **`berth-svc-*`（服务）和 `berth-run-*`（一次性执行）** 两种前缀——
不能图省事用 `berth-*` 一把捞：daemon 自己的 unit（如 `berth.service`、`berth-daemon.service`）也在这个名字空间里，
一把捞会在启动清理时把 daemon 自己 SIGKILL 掉（systemd 再重启它 → 无限自杀循环，真踩过，见第 11 条）。
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

9. **MariaDB 初始化（mysql 模板）**
   `mariadbd --bootstrap` 模式强制 `--skip-grant-tables`，在里面执行 `ALTER USER`/`GRANT`
   会报 `ERROR 1290 ... --skip-grant-tables`。改用 `--init-file`：正常启动流程里执行 SQL，
   幂等语句（`CREATE USER IF NOT EXISTS` + `ALTER USER`）每次启动重复执行也没问题。

10. **缺镜像的报错**
    lowerdir 不存在时 overlayfs 只报 `special device overlay does not exist`，非常难懂。
    现在镜像统一走名字解析（`sandbox/images.py`）：未知名字/目录缺失在启动前直接报
    「镜像不存在 + 构建命令」，不再抛晦涩的 mount 错。

11. **孤儿清理不能一把捞 `berth-*`**
    daemon 自己的 systemd unit（`berth.service` / `berth-daemon.service` …）和 workload
    （`berth-svc-*` / `berth-run-*`）共享 "berth-" 前缀。清理时如果按 `berth-*` 匹配，
    daemon 启动会把**自己**当孤儿 SIGKILL（systemd 再重启 → 无限自杀循环，实测踩到）。
    修法：`list_managed_units()` 只匹配 `berth-svc-*` 和 `berth-run-*`。

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

## 6. 运行模式与镜像（部署视角）

**镜像从哪来**：`scripts/setup_rootfs.sh` 用 debootstrap 构建（默认 `/opt/berth/rootfs`，
含 python3 / ca-certificates / util-linux），redis 镜像 = 基础镜像 + chroot 里装 redis-server。
镜像是普通目录，berth 只当作 overlayfs 只读下层用，升级镜像就换目录（`BERTH_ROOTFS` 指过去即可）。

**进程权限**：沙箱构造需要 root 级操作（mount/overlay/pivot_root/systemd-run/chown），berth 支持两种跑法：

- **root 模式**：直接以 root 起 daemon（`sudo uvicorn server:app ...`）。
  `sandbox/paths.py: sudo()` 检测到 euid==0 时直接执行，不走 sudo（最小系统没装 sudo 也能用）。
- **免密 sudo 模式**：普通用户 + `NOPASSWD`，`sudo -n` 执行特权命令。
  注意：运行数据目录（默认 `/var/lib/berth`）首次由 root 创建后归运行用户。

**数据目录首次创建**：`state/db.py` 打开 DB 前若目录不存在，先按当前用户 mkdir；
失败（非 root 且 /var/lib 受限）则回退 `ensure_base_dir()` 用 sudo 建。

## 7. 数据模型（SQLite）

```sql
runs(task_id PK, code, stdout, stderr, exit_code, duration_ms, artifacts JSON,
     created_at, finished_at,
     status, image, network JSON, labels JSON, timeout, started_at, updated_at)
                                   -- 脚本执行历史 + 异步执行状态
services(service_id PK, name, template, unit, host, port, internal_port,
         status, dir, persist, created_at, updated_at,
         labels JSON, overrides JSON, spec JSON,
         init_spec JSON, init_result JSON, network JSON, task_id)
                                   -- 服务元数据（spec 存生效模板，重启靠它）
ports(service_id PK, internal_port, external_port UNIQUE, allocated_at) -- 端口分配
events(id PK, ts, actor, action, target, method, path, status, detail JSON)
                                   -- 操作审计
berth_tokens(id PK, name, token_hash UNIQUE, scope JSON,
             created_at, last_used_at, revoked_at)   -- 访问令牌（只存 sha256）
image_builds(id PK, name, status, spec JSON, log, image_path, error,
             created_at, finished_at)               -- 镜像构建记录
```

- WAL 模式 + 每次操作独立连接（uvicorn 多线程安全）
- 表结构懒建（`connect()` 首次调用自动建表），避免「先写后建表」丢数据
- 老库启动时自动补列（`ALTER TABLE ADD COLUMN`，见 `state/db.py: MIGRATIONS`），不需要手工迁移

## 8. 网络策略（B2）

### 8.1 机制：systemd 的 cgroup BPF

berth 已经在用 `systemd-run` 包 cgroup，所以网络策略直接用 systemd 的
`IPAddressDeny=` / `IPAddressAllow=`（底层是 cgroup v2 + BPF）：

| 策略 | systemd 属性 | 效果 |
|---|---|---|
| `host`（默认） | 无 | 保持现状：host 网络，可进可出 |
| `none` | `IPAddressDeny=any` | 全部出站被挡（含 loopback） |
| `allow` | `IPAddressDeny=any` + `IPAddressAllow=<CIDR>` | 只有白名单放行 |

systemd 的规则是「**更具体的条目优先**」，所以 `Deny=any` + `Allow=10.0.0.0/8`
的效果是：10.0.0.0/8 放行，其余（含 127.0.0.0/8）全挡 —— 这正好挡住
「沙箱内访问 berth 自己的 API 端口」和「访问其它项目的数据库端口」。

白名单里的域名在**请求时**解析成 IP（`socket.getaddrinfo`），解析失败直接报错
（不静默放行）。注意：域名解析是"请求时"的一次性快照，域名换 IP 后需要重新提交。

要求：cgroup v2 + 内核支持 cgroup BPF（systemd ≥ 230）。`GET /system/info`
的 `network.supported` 会告诉你当前宿主是否满足；不满足时 systemd 会静默忽略
这些属性（**这时沙箱的网络是不受控的**，别当成有隔离）。

### 8.2 端口级隔离：评估与选型

**问题**：`IPAddressDeny/Allow` 只按 IP 过滤，**不分端口**。host 网络下，
沙箱拿到的是宿主的 IP，所以只要目标在同一台机器上，挡住 IP 就等于挡住所有端口
（包括 berth 自己的 API、其它项目的数据库）—— 但**跨机器**的目标按 IP 过滤就够了。
换句话说：host 网络 + IP 级过滤 = "能连到哪台机器" 的隔离，**不是** "能连哪个服务" 的隔离。

评估过的方案：

| 方案 | 能做端口级隔离 | 改动量 | 代价 / 风险 |
|---|---|---|---|
| A. systemd `IPAddressDeny/Allow`（**已实现**） | ❌（只按 IP） | 最小（berth 已在用 systemd-run） | 同机不同服务之间没有端口隔离 |
| B. nftables + cgroup 匹配 | ✅ | 中（要写 nft 规则集 + cgroup id → 规则映射） | 规则集要随 cgroup 生命周期增删；调试成本高；nft 不可用时静默失效 |
| C. 每任务独立 netns + veth | ✅ | 大（要配 veth/ipaddr/路由/DNS，还要给服务分配可路由 IP） | 服务之间要通过 berth 做端口映射才能互访，和"名字即身份 + 动态端口"的模型冲突 |
| D. 每个沙箱一个独立 IP（loopback 别名 127.x.y.z） | ⚠️ 部分 | 中 | 仍然挡不住"同 IP 不同端口" |

**选型**：第一版用 **A**（最小改动、覆盖主要威胁模型：挡住 berth API 和其它项目的数据端口），
并把 B 作为后续可选加固。**已知限制**（写进 README 与本节）：

- 同一台机器上，如果两个服务共享同一个 IP（host 模式就是共享），IP 级过滤无法区分端口；
  实践中靠"不同项目用不同容器/VM"或"不同端口 + 不暴露给不可信代码"来规避。
- 不支持 cgroup BPF 的内核上，网络策略**不生效**（`/system/info` 会提示）。
- 沙箱仍然是 host 网络：可以访问宿主上**任何 IP** 的**任何端口**（只要 IP 在白名单里）。

**加固建议**（未实现，按需上）：
1. 在宿主机上给每个项目用独立的 network namespace / 独立 VM（推荐，隔离最彻底）；
2. 或用 nftables 按 cgroup id 匹配端口（方案 B）。

## 9. 异步执行（B4）

一次执行拆成三步（`sandbox/overlayfs.py`）：

```
prepare_run()  → 校验参数 / 建 overlay 目录 / 写代码 / 组装 systemd-run 命令
spawn()        → 起进程（输出重定向到文件）
wait_run()     → 等结束 / 超时强杀 / 收结果
```

同步路径 `run_script()` = prepare + spawn + wait；
异步路径（`sandbox/runs.py`）= prepare + spawn + **后台线程** wait：

- 立刻返回 `{task_id, run_id, status: "running", ...}`，DB 里先写一条 `status=running` 的记录
- 输出文件放在 `BASE_DIR/scripts/{task_id}/runs/{run_id}/stdout.txt`（不被 overlay 清理删掉）
- `GET /scripts/{task_id}?offset=N` 从活动登记表（`sandbox/active.py`）拿实时输出，按偏移增量读
- `GET /scripts/{task_id}/stream` 是 SSE 版本：轮询活动登记表，把新增内容推给前端
- `POST /scripts/{task_id}/cancel` → `killpg` + `systemctl kill --signal=SIGKILL`（cgroup 兜底）→ 状态 `killed`
- 后台线程结束时统一 `record_run()`（覆盖 running 记录）+ 删 overlay 目录

资源上限全部走环境变量（`sandbox/config.py`），请求里可以在上限内自选，超了直接 400：

| 变量 | 默认 | 含义 |
|---|---|---|
| `BERTH_MAX_TIMEOUT` | 600 | timeout 上限（秒） |
| `BERTH_MAX_MEMORY_MB` | 4096 | 内存上限 |
| `BERTH_MAX_CPU_PERCENT` | 400 | CPU 配额上限（100 = 1 核） |
| `BERTH_MAX_PROCESSES` | 512 | 进程数上限（TasksMax） |

## 10. 文件进出（B3）

```
上传  PUT  /scripts/{task_id}/files      → workspace
下载  GET  /scripts/{task_id}/files      → tar
      GET  /scripts/{task_id}/files/{p}  → 单个文件
树    GET  /scripts/{task_id}/tree       → 带大小 / mime
```

- 落点固定：`BASE_DIR/scripts/{task_id}/workspace`（沙箱内就是 `/workspace`）
- `task_id` 必须是安全字符（`[A-Za-z0-9._-]{1,128}`）—— 它会变成目录名
- 解 tar 时拒绝绝对路径、`..` 穿越、越界软链接（`sandbox/files.py`）
- 上传完把文件 chown 给 uid 1000，沙箱里能读写

**只读挂载**（`mounts` 参数 / 模板的 `mounts` 段）：

- 源路径必须落在 `BERTH_MOUNT_ROOTS`（默认 `/srv/berth-mounts`）白名单内
- 判断前先 `resolve()` 解符号链接，防止 `白名单/../../etc` 绕过
- 沙箱内用 `mount --bind` + `mount -o remount,bind,ro` 挂成只读

## 11. 服务初始化与重置（B5 / B8）

**初始化（init）** 分两层，避免"模板写死 SQL"：

- 模板的 `init` 段 = **怎么执行**（command / args / env / image / timeout）
- 请求的 `init` = **执行什么**（sql / script / file）

执行流程：请求内容先落成 workspace 里的 `.berth-init.sql`（或 `.berth-init.sh`），
再用模板的 init 命令跑（`{init_file}` 占位符指向那个文件）。
这样 SQL 里带引号 / `$` / 花括号都不会被 shell 或 `str.format` 破坏。

幂等语义：**已在运行的同名服务不重复执行 init**（ensure 直接返回 `reused: true`）。
init 的结果（exit code / stdout / stderr / 耗时 / 执行时间）存在服务的 `init_result` 里，
`GET /services/{name}` 能查到；init 失败会让服务启动失败（500）并清理掉。

**重置（reset）**：`停止 → 清数据卷 → 重放 init → 启动`。

- 清卷用「先挪走再删」：`volumes` 目录改名 → 建新的空目录 → 后台删旧的
  （重置本身是秒级的；删大目录的时间不计入重置耗时）
- 加速方案评估：overlay 快照对"数据卷"不适用（数据卷是 bind mount 的普通目录），
  真正能加速的是**文件系统快照**（btrfs/ZFS snapshot）或**卷级 tar 快照**。
  第一版没做快照（大数据量下重建成本 = initdb 时间，见 README 的耗时说明），
  `keep_backup=true` 可以先把旧卷挪到 `volumes.backup.<ts>` 供人工对比。
- 耗时：接口返回 `reset_duration_ms`（实测值），文档里给了记录方法。

## 12. 标签（B12）

- `runs.labels` / `services.labels` 是 JSON 列（`{"project": "foo", "run": "1"}`）
- 过滤：`?label=project=foo`（可重复，AND 语义）；`?label=key` 表示"只要有这个键"
- 解析在 `state/db.py: parse_label_filters()`，匹配在 `match_labels()`
- 没有标签的老数据 `labels = {}`，不带标签过滤 = 不过滤（行为不变）

用途：UI 按项目分组；Shakedown 按项目 / 按次执行回溯沙箱记录。

## 13. 操作审计

`sandbox/service.py` 之外的所有写操作由 `server.py` 的中间件统一记录到 `events` 表：

- `actor`：token 的短哈希（`token:930bbdc51b6a`），**不落 token 明文**
- `action`：`METHOD /path`，`target`：服务名 / task_id
- 审计失败不影响主流程（try/except 吞掉）

## 14. 令牌与作用域

### 14.1 为什么不是 OAuth / JWT

berth 是**单租户自托管**的沙箱服务，调用方是自己可控的 agent（Shakedown 等）。
业界那套复杂的令牌机制是为别的场景设计的：

| 机制 | 为谁设计 | berth 要不要 |
|---|---|---|
| OAuth 刷新令牌 | 第三方应用代用户操作账号 | ❌ 调用方是自己人 |
| 短时效 token（分钟级） | 公网多租户，密钥暴露面大 | ❌ 没有轮换基础设施，只会成为天天要续的负担 |
| mTLS / 请求签名 | 客户端不可信，要防篡改 | ❌ 客户端要实现签名逻辑 |
| **静态 token + 作用域** | 单租户自托管 | ✅ |

所以复杂度预算花在**隔离**（作用域）上，而不是**轮换**（刷新流）上。

### 14.2 两种身份

| 来源 | 身份 | 能做什么 |
|---|---|---|
| `BERTH_TOKEN`（环境变量，逗号分隔多个） | **admin** | 全部 + 宿主级操作（造/删镜像、签发令牌） |
| `berth_tokens` 表（`POST /tokens` 签发） | **非 admin** | 能看能改服务/执行/产物；带 scope 时只看得到自己那域 |

**库里只存 sha256**，明文只在签发时返回一次（跟密码一样）。
审计里的 actor 是 `admin:env` 或 `token:<短id>`，不落明文。

**admin 只属于环境变量里的凭证**：库里签发的 token 哪怕没给 scope，也只是"工作令牌"
（能操作所有服务/执行），碰不到宿主级操作。这样"agent 拿到的 token"和"你的控制面凭证"
天然分开了。

### 14.3 作用域 = 标签域

复用 B12 的 labels 机制，不另造一套资源树：

```json
{"labels": {"project": "shop"}}
```

带这个 token 的调用方：

- 只能看到 / 操作 labels **包含** `project=shop` 的服务、执行、产物
- 创建资源时**强制**打上作用域标签（`{"project":"evil"}` 会被改写成 `{"project":"shop"}`）
- 作用域外的资源 → **404**（不是 403 —— 不泄露存在性）
- 没有标签的老资源 → scoped token 看不见（默认拒绝）

**一个容易漏的攻击面**：scoped token 往**别人项目的 task_id** 里上传文件 = 植入待执行代码
（对方下次跑 pytest 就执行了）。所以上传时会给 task 建一条带作用域标签的**占位记录**
（`status=pending`），把 task 圈进调用方的域；之后别的 token 再写同一个 task → 404。

### 14.4 轮换（零停机）

`BERTH_TOKEN` 支持逗号分隔多个 → 换的时候先加新的：

```
BERTH_TOKEN=旧的,新的   →  重启 berth  →  调用方切到新的  →  验证通过  →  摘掉旧的
```

库里签发的 token 直接 `DELETE /tokens/{id}` 吊销，立刻生效。
（思路同 SSH `authorized_keys` / docker login —— 静态凭证 + 手动轮换，不搞刷新流。）

### 14.5 沙箱能不能拿到 token

不能，已验证：

- pivot_root 之后沙箱的 `/` 是 overlay 合成根，**宿主的 /etc 完全看不见**
  （沙箱里 `/etc/systemd/system` 是空的，宿主那边有 dbus/sshd/lima-guestagent）
- PID namespace 隔离 → 沙箱看不到 daemon 的命令行（`/proc/1` 是沙箱自己的 init）
- 环境变量不继承（沙箱里没有 `BERTH_*` / `*TOKEN*`）

所以 token 放在宿主上（systemd unit / 0600 的 env 文件）是安全的。
**唯一要管的是宿主上的文件权限** —— 沙箱以 uid 1000 运行，宿主上 uid 1000 的真实用户
能读任何 world-readable 的文件，所以放 token 的文件要 `chmod 600 root:root`。

## 15. UI 架构

- 静态页挂在 FastAPI 的 `StaticFiles`（`/ui`），**不新增进程、不引入 Node 构建链**
- 前端：Alpine.js（vendored 在 `ui/vendor/alpine.min.js`，不依赖 CDN）
- hash 路由（`#/services/xxx`），URL 稳定，方便 Shakedown 看板深链
- 鉴权：token 存 `sessionStorage`，只放请求头（**不进 URL / 不进日志 / 不进页面源码**）
- SSE：前端用 `fetch` + `ReadableStream` 手动解析（`EventSource` 不能带 Authorization 头）
- 产物隔离：图片 / 文本转义后渲染；HTML 报告只在 `<iframe sandbox>`（无 `allow-same-origin`）里看，
  服务端同时下发 `Content-Security-Policy: sandbox; default-src 'none'`
- 破坏性操作要二次确认，并**输入目标名称**才执行
- 列表页 3 秒轮询（页面不可见时暂停）

细节见 [docs/ui.md](ui.md)。

