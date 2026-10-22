# berth 改造任务清单

> 在 `github.com/zsgh/berth` 仓库实施，交给开发 AI 执行。
> 起因：berth 要作为 Shakedown（软件测试 Agent 平台）的沙箱。Shakedown 的任务见 `todo.md`，两边独立推进，接口以本文档和 `docs/api.md` 为准。
> 任务前的 `[ ]` 完成后改为 `[x]`。
>
> **实施状态（2026-10-22）**：P0 全部完成，P1 全部完成，UI 全部完成。
> 需要 Linux + systemd 的部分（真机起服务 / Playwright 截图 / 端到端联调）请在目标机器上按
> 「测试与冒烟」一节跑一遍；纯逻辑部分都有 `tests/test_*.py` 覆盖（不依赖 systemd）。

---

## 0. 背景与原则

**berth 现状**（基于 README 与 `docs/api.md`）：
- 轻量沙箱 HTTP 服务（FastAPI + uvicorn，默认 9000 端口），不用 Docker，直接用 Linux 原语：overlayfs、unshare + pivot_root + setpriv、systemd、SQLite
- `POST /scripts/run`：跑一次性脚本，uid 1000、capability 清空、cgroup 限制内存/CPU/进程数，`task_id` 对应持久 `/workspace`
- `PUT /services/{name}`：按模板起长驻服务（现有 redis、mysql/MariaDB），systemd 托管，名字即身份，幂等
- 网络为 host 模式；状态存 SQLite，重启可恢复
- 要求 Linux + systemd；macOS 通过 Lima VM 运行

**改造原则**：
1. **向后兼容**：所有新参数可选，默认行为不变，现有测试必须继续通过
2. 每项改完更新 `docs/api.md`，并在 `tests/` 补验证脚本（风格沿用现有 `tests/test_*.py`）
3. 保持"轻量"：不引入 Docker、Kubernetes 等重依赖，优先复用 systemd、cgroup、overlayfs 现有能力
4. 与 Shakedown 约定：Shakedown 只通过 HTTP API 调用 berth，不依赖其内部实现
5. UI 随 berth 一起部署，不新增进程，不引入 Node 构建链（详见 "UI" 一节）

---

## P0：Shakedown 执行器的前置条件

### B1 API 鉴权
- [x] 增加 Bearer token 鉴权（环境变量 `BERTH_TOKEN` 配置；未配置时行为不变，但启动日志给出警告）
- [x] 默认监听 `127.0.0.1`，对外监听需显式配置（`server.py` 入口默认 127.0.0.1；`BERTH_HOST` 显式配置；对外监听且无 token 时启动日志额外告警）
- [x] 原因：沙箱是 host 网络，沙箱内代码能直接访问 berth 自己的 API，例如删除其他项目的服务与数据卷（`DELETE /services/{name}?volumes=true`）
- **验收**：无 token 请求返回 401；有 token 正常；`/health` 可免鉴权 → `tests/test_auth.py`

### B2 网络策略
- [x] `/scripts/run` 与服务模板新增 `network` 参数：
  - `host`（默认，保持现状）
  - `none`（无网络）
  - `allow`（IP/CIDR 白名单列表，必要时支持"域名先解析成 IP"）
- [x] 第一版方案：用 `systemd-run` 的 `IPAddressDeny=any` + `IPAddressAllow=...`（berth 已在用 systemd-run，改动最小）
- [x] 评估并记录端口级隔离方案（`docs/design.md` 第 8 节）：上述机制只按 IP 过滤、不分端口 → 评估了 nftables+cgroup、独立 netns+veth、独立 IP 三种替代方案，选型为"第一版用 systemd IP 过滤（覆盖同机威胁模型），nftables 列为后续加固"，并把限制写进 README「已知限制」
- **验收**：沙箱内访问白名单地址成功；访问非白名单地址失败；访问 berth API 端口失败；`network: none` 时任何出站失败 → 策略层 `tests/test_network_policy.py`；真机连通性需在 Linux 上验证

### B3 文件进出
- [x] 上传：`PUT /scripts/{task_id}/files`，支持多文件或 tar 包，解包到该任务的 `/workspace`
- [x] 下载：`GET /scripts/{task_id}/files/{path}`；以及整个目录或指定路径打包下载（用于截图、trace、junit xml、HTML 报告）
- [x] 只读挂载：`/scripts/run` 新增 `mounts` 参数，把宿主目录只读挂载进沙箱；挂载源限制在 `BERTH_MOUNT_ROOTS` 白名单内（解符号链接后判定，防 `../` 绕过）
- [x] 现状：`artifacts` 只返回文件名列表 → 现在另有 `GET /scripts/{task_id}/tree`（带大小 / mime）
- **验收**：上传一个 pytest 目录，沙箱内可见；执行后能下载其中生成的文件；`mounts` 挂载的目录在沙箱内只读，越权路径被拒绝 → `tests/test_files.py`

### B4 异步执行与资源上限
- [x] `POST /scripts/run` 新增 `async: true`，立即返回 run id，不阻塞
- [x] `GET /scripts/{task_id}` 返回状态（running / finished / timeout / killed），并支持增量读取 stdout/stderr（如 `?offset=`）
- [x] 提供取消接口：`POST /scripts/{task_id}/cancel`
- [x] 以下上限改为可配置（环境变量设上限，请求里在上限内自选）：`timeout`（`BERTH_MAX_TIMEOUT`，默认 600）、`memory_mb`（`BERTH_MAX_MEMORY_MB`，默认 4096）、`cpu_percent`（`BERTH_MAX_CPU_PERCENT`，默认 400）、进程数（`BERTH_MAX_PROCESSES`，默认 512）
- [x] 原因：Playwright + Chromium 至少需要 1GB 以上内存和更多进程数；Web 用例集可能超过 10 分钟
- **验收**：异步提交一个 15 分钟的任务，期间能查询状态和增量日志，能取消；同步调用行为不变 → `tests/test_async.py`

### B5 服务参数覆盖与初始化
- [x] `PUT /services/{name}` 在 `template` 之外支持：
  - `overrides`：覆盖 env、args、resources、ports 区间（另支持 command / healthcheck / network / mounts）
  - `init`：服务健康检查通过后执行的初始化内容（SQL 文本、脚本，或 workspace 中的文件）
- [x] 幂等语义保持：已在运行的同名服务不重复执行 `init`；`init` 执行结果和错误能在服务详情（`init_result`）里查到
- [x] 现状：只能传模板名，库名、密码、初始数据无法按项目配置
- **验收**：用同一个 postgres 模板起两个服务，库名、密码、初始数据各不相同且互不影响 → `templates/postgres.yaml` + `overrides.env` / `init.sql`

### B6 PostgreSQL 模板
- [x] 新增 `templates/postgres.yaml`（字段语义同现有模板：`command` / `args` / `env` / `ports` / `volumes` / `healthcheck` / `resources`，另含 `init`）
- [x] `scripts/setup_rootfs.sh` 增加 `--with-postgres`，构建 `rootfs-postgres`
- [x] 版本固定到一个 LTS 主版本：PostgreSQL 15（Debian bookworm 默认，官方支持到 2027-11），写入文档
- **验收**：`PUT /services/xxx {"template":"postgres"}` 起服务，外部 `psql` 可连；停止、启动、删除（含数据卷）均正常

### B7 runner 镜像
- [x] 新增 `rootfs-runner`（`scripts/setup_rootfs.sh --with-runner`）：Python3、pytest、httpx、Playwright 及 Chromium
- [x] 依赖在**构建期**安装，运行期不依赖外网（chroot 里 pip install + `playwright install chromium`）
- [x] 沙箱内没有 user namespace 映射 → Chromium 需 `--no-sandbox`（自检脚本里已带）；`/dev/shm` 在 overlay 中单独挂 1G tmpfs
- [x] 提供一个自检脚本：`scripts/runner_selftest.sh`（沙箱内用 Playwright 打开一个本地页面并截图）
- **验收**：`POST /scripts/run {"image":"runner", ...}` 内用 Playwright 打开被测页面，截图写入 `/workspace` 且可通过 B3 的接口下载

---

## P1：提升可用性

### B8 服务重置
- [x] `POST /services/{name}/reset`：停止 → 清数据卷 → 重新初始化（重放 `init`）→ 启动
- [x] 评估用 overlay 或快照加速重置：结论是数据卷是普通 bind mount 目录，overlay 快照不适用；可行的是文件系统快照（btrfs/ZFS）或卷级 tar 快照；第一版用"先挪走再删"（重置本身秒级），并提供 `keep_backup=true` 保留旧卷。评估过程写在 `docs/design.md` 第 11 节
- **验收**：重置后数据回到 `init` 后的起点；耗时记录在文档中（接口返回 `reset_duration_ms`，文档给出记录方法与影响因子）

### B9 服务日志接口
- [x] `GET /services/{name}/logs?tail=N&since=...`，封装 `journalctl -u berth-svc-{id}`
- [x] 供 Shakedown 做失败归因（对照被测服务日志）；另支持 `?follow=true` 的 SSE 实时跟随

### B10 http 健康检查
- [x] `healthcheck.type` 在现有 `tcp` 之外增加 `http`：路径（`path`）、期望状态码（`expect_status`，支持数组）、超时（`timeout`）、间隔（`interval`）

### B11 被测系统运行时
- [x] `scripts/setup_rootfs.sh` 参数化，可按目标栈构建 JDK、Node、Go 等 rootfs（`--stack node,go,java`，沿用 `rootfs-*` 自动发现机制）
- [x] 支持用自定义 spec 起被测服务：`PUT /services/{name}` 传 `spec`（内联模板：只读挂载代码目录 + 启动命令 + 端口 + 健康检查），对应 Shakedown 项目配置里的"被测系统启动方式"
- [x] 与 B5 一并设计：`overrides`（覆盖）与 `spec`（内联模板）走同一套合并 / 校验逻辑，不重复

### B12 标签（labels）
- [x] `/scripts/run` 与 `PUT /services/{name}` 支持 `labels`（键值对，如 `project=foo`、`run=123`），存入 SQLite（JSON 列）
- [x] 列表接口支持按标签过滤：`GET /scripts?label=project=foo`、`GET /services?label=project=foo`（可重复，AND 语义）
- [x] 用于 UI 按项目分组，以及 Shakedown 按项目、按次执行回溯沙箱记录
- **验收**：带标签创建的服务和执行，能按标签过滤出来，不带标签的行为不变 → `tests/test_labels.py`

---

## UI：可视化管理界面

目前 berth 只有 HTTP API 和 curl，排查要靠 `journalctl` 和手动 `ls /var/lib/berth`。UI 的目标是：**不用命令行就能完成起服务、看日志、跑脚本、看产物、清理孤儿卷**，并且能被 Shakedown 的看板深链进来排障。

### 设计原则
- [x] **随 berth 一起部署**：FastAPI 的 `StaticFiles` 托管在 `/ui`，访问 `http://host:9000/ui/`，不新增进程
- [x] **无构建步骤**：静态 HTML + Alpine.js（`ui/vendor/alpine.min.js` 随仓库提交），不依赖 CDN，不引入 Node 工具链
- [x] **UI 只调用 berth 自己的 HTTP API**，不走后门；UI 能做的事 API 都能做
- [x] **自用定位**：不做多用户和权限，鉴权复用 B1 的 token（输入一次，存 `sessionStorage`，不放进 URL）
- [x] **刷新策略**：列表页 3 秒轮询（页面不可见时暂停）；日志和执行输出用 SSE 增量推送，不用 WebSocket
- [x] 桌面浏览器为主，不做移动端适配

### 页面与路由
采用 hash 路由，URL 保持稳定，便于 Shakedown 看板深链。

| 路由 | 页面 | 主要内容 | 状态 |
|---|---|---|---|
| `/ui/#/` | 总览 | 健康状态、版本、宿主 CPU/内存/磁盘、活跃服务数、近 24 小时执行数与失败数、孤儿卷数量、告警（如磁盘占用超阈值） | [x] |
| `/ui/#/services` | 服务列表 | 表格：名称、模板、状态、`host:port`、内存/上限、CPU 累计、磁盘、创建时间、标签；开关"含历史"；行内操作：停止、启动、重置、删除、删除并清数据卷、查看日志 | [x] |
| `/ui/#/services/{name}` | 服务详情 | 完整 spec（模板与覆盖项）、端口、systemd unit、健康检查状态、`init` 执行结果、资源用量、实时日志（SSE） | [x] |
| `/ui/#/runs` | 执行列表 | task_id、状态、镜像、耗时、exit_code、网络策略、标签、时间；按状态、镜像、标签、时间过滤 | [x] |
| `/ui/#/runs/{task_id}` | 执行详情 | 代码或 cmd、参数（资源、网络、挂载）、stdout/stderr 实时输出、产物文件树（图片、文本、HTML 预览，文件下载）、取消按钮、清空 `/workspace` | [x] |
| `/ui/#/run/new` | 手动运行 | Playground：填代码或 cmd，选镜像、资源、网络策略、超时，提交后跳转到执行详情 | [x] |
| `/ui/#/volumes` | 数据卷 | 所属服务、大小、孤儿标记；孤儿卷可清理 | [x] |
| `/ui/#/images` | 镜像 | 已发现的 rootfs 列表：名称、路径、大小；只读，构建方式用提示文字给出 | [x] |
| `/ui/#/templates` | 模板 | 展示模板 yaml，提供"校验"按钮（复用服务端校验逻辑）；第一版只读 | [x] |
| `/ui/#/events` | 操作记录（P1） | 谁（token 标识）、何时、对什么做了什么操作 | [x] |

### 后端需新增或补充的接口
- [x] `GET /system/info`：版本、配置的上限（B4）、白名单挂载目录（B3）、镜像列表、网络策略能力
- [x] `GET /system/stats`：宿主 CPU、内存、`BERTH_HOME` 所在磁盘的占用
- [x] `GET /services/{name}/logs`（B9，含 `?follow=true` 的 SSE）
- [x] `GET /scripts/{task_id}/stream`（SSE，实时输出；依赖 B4）
- [x] `GET /scripts` 补充分页与过滤：`status`、`image`、`since`、`label`（B12）、`limit`、`cursor`
- [x] 产物：返回带大小和 mime 的文件树（`GET /scripts/{task_id}/tree`）；下载与预览接口见 B3
- [x] `GET /images`、`GET /templates`、`GET /templates/{name}`、`POST /templates/validate`
- [x] `DELETE /volumes/{service_id}`：**只允许清理孤儿卷**，非孤儿卷返回 409
- [x] `GET /events`（P1）：新增操作审计表，记录所有写操作

### 安全要求
- [x] **产物预览必须隔离**：图片和文本在前端转义后渲染（`x-text`，不用 innerHTML）；HTML 报告只放在 `<iframe sandbox>` 中展示（不加 `allow-same-origin`），服务端同时下发 `Content-Security-Policy: sandbox; default-src 'none'`；下载响应带 `Content-Disposition` 与 `X-Content-Type-Options: nosniff`
- [x] 日志、stdout、stderr 一律按纯文本渲染；单次最多展示最后 1MB，超出部分提供下载
- [x] 破坏性操作（删除服务、删除并清数据卷、重置、清理孤儿卷、清空 workspace）需二次确认，且需要输入目标名称
- [x] token 不出现在 URL、日志和页面源码里；401 时回到登录态

### UI 任务

**P0**
- [x] **U1 骨架与鉴权**：`/ui` 静态托管、登录（token）、侧栏导航、顶栏健康状态、统一错误提示、hash 路由
  - 验收：未登录访问任何页面都回到登录态；token 错误有明确提示
- [x] **U2 总览页**：实现 `GET /system/info`、`GET /system/stats` 与总览页
  - 验收：能一眼看到 berth 是否健康、磁盘是否快满、有没有孤儿卷
- [x] **U3 服务列表与详情**：表格、行内操作、详情页、日志查看（依赖 B9）、创建服务表单（模板选择 + `overrides` + `init`，依赖 B5）
  - 验收：在页面上创建一个 postgres 服务，看到日志，停止、启动、重置（B8）、删除（含数据卷），每步状态实时更新
- [x] **U4 执行列表与详情**：列表过滤、实时输出、产物文件树与预览、取消、清空 workspace（依赖 B3、B4）
  - 验收：跑一个会生成截图和 HTML 报告的任务，能在页面上看到实时输出，预览截图，在隔离的 iframe 里查看 HTML 报告，下载整个产物目录
- [x] **U5 手动运行（Playground）**：表单覆盖 `/scripts/run` 的全部参数，提交后跳转执行详情
  - 验收：不用 curl 就能验证 runner 镜像和网络策略是否生效
- [x] **U6 数据卷页与孤儿卷清理**：实现 `DELETE /volumes/{service_id}` 与页面
  - 验收：制造一个孤儿卷，页面标出并能清理；非孤儿卷无法清理

**P1**
- [x] **U7 镜像与模板页**：只读展示，模板支持校验
- [x] **U8 按项目分组与过滤**：基于标签（B12）；服务和执行列表可按 `project`、`run` 过滤，总览页按项目汇总
- [x] **U9 操作记录页**：实现 `GET /events` 与页面
- [x] **U10 深链约定文档**：在 `docs/ui.md` 写明稳定的 URL 规则（如 `/ui/#/runs/{task_id}`），供 Shakedown 看板跳转；约定 Shakedown 使用 `task_id = {项目}-{执行}-{用例}`、服务名 `{项目}-db` 等命名，便于在 UI 里定位

---

## 总验收（与 Shakedown 联调）

用一个真实项目完整走通：

1. 起被测库（PostgreSQL 或 MariaDB）并初始化（B5、B6）
2. 起被测服务（B10、B11）
3. 上传用例目录（B3）
4. 在 `allow` 网络策略下异步执行 pytest 与 Playwright（B2、B4、B7）
5. 下载报告、截图、trace（B3），读取被测服务日志（B9）
6. 重置数据库后再次执行（B8）
7. 清理全部服务与数据卷

8. 全程在 UI 上能看到：服务状态与日志、执行的实时输出与产物预览、数据卷与孤儿卷（U1 到 U6）

同时确认：沙箱内访问不到 berth API 和其他项目的服务端口（B1、B2）。

> 上面 8 步需要一台 Linux + systemd 的机器（macOS 请用 `scripts/macos-bootstrap.sh` 的 Lima VM）。
> 每一项对应的接口都已实现并有自动化测试覆盖（`tests/`），但"真机跑通"这一步要在目标机器上执行；
> 跑完把结果回填到本节。

---

## 已知限制（沿用 README，改造后需更新）

- 沙箱内固定降权到 uid 1000，无 user namespace 映射
- host 网络模式下服务直接监听宿主端口，靠 SQLite 记录 + 探测去重，不是强制隔离（B2 完成后更新）
- 停止/超时依赖 cgroup 强杀
- **新增**：网络隔离只到 IP 级（`IPAddressDeny/Allow` 不分端口）；宿主不支持 cgroup v2 + BPF 时网络策略不生效
- **新增**：服务重置是"清卷 + 重来"，大数据库重建耗时 = initdb 时间
