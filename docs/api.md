# berth API

Base URL：`http://127.0.0.1:9000`（默认只监听本机；对外监听要显式配置 `BERTH_HOST`）

所有接口返回 JSON。出错时返回 `{"detail": "..."}`。

## 鉴权（B1）

除 `/health` 和 `/ui/*` 外的所有接口都要带 Bearer token：

```bash
curl -H "Authorization: Bearer $BERTH_TOKEN" http://127.0.0.1:9000/system/info
```

- 没配置任何 token（环境变量空 + 库里没有）→ 不鉴权（保持旧行为），启动日志会打警告
- token 不对 / 已吊销 → `401`（带 `WWW-Authenticate: Bearer`）
- `/health` 永远免鉴权（探活用）
- `/ui/*` 静态资源免鉴权（登录页要能加载；静态文件里没有敏感数据）

### 两种身份

| 来源 | 身份 | 能做什么 |
|---|---|---|
| `BERTH_TOKEN`（环境变量 / 只读配置文件，可逗号分隔多个） | **超级管理员**（独一份） | 跨 user + 宿主级操作（造/删镜像、管 user、签发令牌） |
| `berth_tokens` 表签发（`POST /tokens`） | 某个 **user** | 只能看到/操作这个 user 的资源 + 全局共享资源（只读） |

**库里只存 token 的 sha256**，明文只在签发时返回一次（跟密码一样）。

### user（租户/项目）

`user` 是一等公民（`users` 表），**授权判定 = owner 相等**：

| 资源 | 归属 | 谁能看 | 谁能改 |
|---|---|---|---|
| 某个 user 私有 | `owner = <user>` | 该 user + 超管 | 该 user + 超管 |
| **全局共享**（公共 mysql/redis） | `owner` 为空 | 所有 user | **只有超管** |
| 别人的 | `owner = 别人` | 看不见（`404`） | — |

- 创建资源时 `owner` **自动取令牌所属的 user**（普通令牌不能把资源建到别人名下）
- 超级管理员可以显式指定 `owner`；不指定 = 全局共享资源
- `labels` 只是**元数据**（分组/检索/统计），**不再参与授权判定**

```bash
# 管理员把公共库设为全局共享
sudo scripts/berth_admin.sh share mysql               # owner 置空
sudo scripts/berth_admin.sh share mysql --owner acme  # 或划给某个 user
```

### 令牌的有效期（TTL）

签发时给 `ttl`（秒），到期**自动失效**（不用人工吊销）—— 动态 worker 场景必需：

```json
{"name": "w-1", "user": "shop", "ttl": 3600}
```

### 令牌怎么拿（三种自动获取）

**A. 本地令牌文件**（同机 agent 零配置）—— `BERTH_TOKEN_FILE=/run/berth/token`，
启动时不存在就自动生成（0600）；agent 读**自己那份**（不是超管的）。

**B. 一次性引导码**（跨机接入）—— `POST /tokens/bootstrap`（见下），换到**受限**令牌。

**C. 客户端封装** —— `scripts/berth_client.py`（自动找 token + 带 header + 支持 TLS）。

### 自动获取 token

三种方式，按需选：

**A. 本地令牌文件**（同机 agent，零配置）

设 `BERTH_TOKEN_FILE=/run/berth/token` → berth 启动时文件不存在就**自动生成**一个
（0600，目录 0700；重启不变）。同机跑的 agent 直接读这个文件：

```python
from berth_client import Berth      # scripts/berth_client.py
b = Berth()                          # 自动读 BERTH_TOKEN / BERTH_TOKEN_FILE
b.get("/services")
```

**B. 一次性引导码**（跨机接入，不用碰 admin 凭证）

berth 启动时在令牌文件同目录生成 `bootstrap.code`（0600，30 分钟有效、只能用一次），
启动日志里也会打印：

```bash
curl -X POST http://<berth>:9000/tokens/bootstrap \
  -H 'Content-Type: application/json' \
  -d '{"code": "SJBBQ3P9", "name": "shakedown", "scope": {"labels": {"project": "shop"}}}'
```

换来的令牌**不是 admin**（只能管自己作用域内的资源），也不能再签发令牌。
连续错码 5 次会锁 5 分钟。

**C. 客户端封装**

```bash
python3 scripts/berth_client.py get /services
python3 scripts/berth_client.py post /scripts/run '{"code":"print(1)"}'
```

### 轮换（零停机）

`BERTH_TOKEN` 可以挂多个（逗号分隔）→ 换的时候先加新的：

```bash
BERTH_TOKEN=旧的,新的     # 改完重启 berth
# → 调用方切到新 token 验证通过
BERTH_TOKEN=新的           # 再把旧的摘掉
```

库里签发的 token 直接吊销即可（`DELETE /tokens/{id}`，立刻生效）。

---

## 脚本执行

### `POST /scripts/run`

在沙箱里跑一次性代码/命令，用完即走（overlay 自动清理；`/workspace` 按 `task_id` 保留）。

**请求体**（`code` 和 `cmd` 二选一）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `code` | string? | - | 源码字符串 |
| `cmd` | string[]? | - | 直接执行的 argv，如 `["/bin/ls", "-la"]` |
| `language` | string? | 自动 | `python`（默认）/ `bash` / `sh`；给了 `code` 才有效 |
| `image` | string | `base` | 镜像名（base / redis / mysql / postgres / runner / 自定义，见「镜像」一节） |
| `timeout` | int | 30 | 超时秒数（上限见 `/system/info`，默认 600） |
| `task_id` | string? | 自动 uuid | 同一 task_id 共享 `/workspace`（只允许字母/数字/`.`/`_`/`-`） |
| `memory_mb` | int | 256 | 内存上限（上限见 `/system/info`，默认 4096） |
| `cpu_percent` | int | 50 | CPU 配额（上限见 `/system/info`，默认 400） |
| `processes` | int? | 512 | 进程数上限（cgroup TasksMax） |
| `async` | bool | false | 异步执行：立即返回 run id，不阻塞（B4） |
| `network` | object? | `host` | 网络策略：`{"mode": "host\|none\|allow", "allow": [...]}`（B2） |
| `mounts` | object[]? | - | 只读挂载：`[{"source": "/srv/berth-mounts/app", "target": "/app"}]`（B3） |
| `labels` | object? | - | 标签：`{"project": "foo", "run": "1"}`（B12） |

超过配置上限的参数直接返回 `400`（例如 `timeout 超过上限（3600s > 600s）`）。

解释器选择顺序：`language` 参数 → 代码首行 shebang（如 `#!/bin/bash`、`#!/usr/bin/env python3`）→ 默认 python。
非 python/bash/sh 的解释器也能用，前提是镜像里有（写在 shebang 里即可）。

**响应**（同步）

```json
{
  "stdout": "hello\n",
  "stderr": "",
  "exit_code": 0,
  "duration": 0.076,
  "artifacts": ["out.txt"],
  "task_id": "test-1",
  "image": "base",
  "language": "python",
  "status": "finished",
  "network": {"mode": "host", "allow": []},
  "labels": {}
}
```

- `exit_code`：进程退出码；超时被强杀为 `-1`；被信号杀死为 `128+signal`（如 OOM → 137）
- `artifacts`：`/workspace` 下的文件列表（相对路径）
- `language`：`cmd` 模式为 `null`；shebang 模式形如 `shebang:/bin/bash`
- `status`：`finished` / `timeout` / `killed` / `error`

**响应**（`"async": true`）

```json
{
  "task_id": "auto-3f2a9c",
  "run_id": "1a2b3c4d",
  "status": "running",
  "image": "runner",
  "network": {"mode": "none", "allow": []},
  "labels": {"project": "foo"},
  "timeout": 900,
  "started_at": 1760000000.12,
  "unit": "berth-run-1a2b3c4d"
}
```

**示例**

```bash
# Python（默认）
curl -s -X POST localhost:9000/scripts/run -H 'Content-Type: application/json' \
  -d '{"code": "print(1+1)"}'

# bash / sh
curl -s -X POST localhost:9000/scripts/run -H 'Content-Type: application/json' \
  -d '{"code": "echo $((6*7))", "language": "bash"}'

# 直接跑命令
curl -s -X POST localhost:9000/scripts/run -H 'Content-Type: application/json' \
  -d '{"cmd": ["/usr/bin/env"]}'

# 换镜像跑
curl -s -X POST localhost:9000/scripts/run -H 'Content-Type: application/json' \
  -d '{"code": "print(1)", "image": "redis"}'

# 断网执行
curl -s -X POST localhost:9000/scripts/run -H 'Content-Type: application/json' \
  -d '{"code": "print(1)", "network": {"mode": "none"}}'

# 只允许访问白名单（域名会先解析成 IP）
curl -s -X POST localhost:9000/scripts/run -H 'Content-Type: application/json' \
  -d '{"code": "...", "network": {"mode": "allow", "allow": ["10.0.0.0/8", "example.com"]}}'

# 异步执行（15 分钟的大任务）
curl -s -X POST localhost:9000/scripts/run -H 'Content-Type: application/json' \
  -d '{"code": "...", "async": true, "timeout": 900, "memory_mb": 2048}'
```

沙箱环境：host 网络（可直连外网，或按 `network` 限制）；文件系统 = 只读镜像 + 每次独立可写层；
以 uid 1000 运行、capability 清空、cgroup 限内存/CPU/进程数；结束后 cgroup 残留进程一并清理。

### `GET /scripts`

执行列表（分页 + 过滤）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `limit` | 100 | 返回条数（1-1000） |
| `status` | - | `running` / `finished` / `timeout` / `killed` / `error` |
| `image` | - | 按镜像过滤 |
| `since` | - | 只看这个时间之后创建的（ISO 字符串） |
| `cursor` | - | 游标分页：上一页最后一条的 `created_at` |
| `label` | - | 标签过滤，可重复：`?label=project=foo&label=run=1`（AND 语义） |

```json
{
  "runs": [
    {
      "task_id": "t-1",
      "code": "...",
      "stdout": "done\n",
      "stderr": "",
      "exit_code": 0,
      "duration_ms": 76,
      "artifacts": ["out.txt"],
      "created_at": "2026-01-01T10:00:00+00:00",
      "finished_at": "2026-01-01T10:00:01+00:00",
      "status": "finished",
      "image": "base",
      "network": {"mode": "host", "allow": []},
      "labels": {"project": "foo"}
    }
  ],
  "count": 1,
  "next_cursor": "2026-01-01T10:00:00+00:00"
}
```

### `GET /scripts/{task_id}`

某次执行的状态 / 历史记录。还在跑 → `status: "running"` 并带实时输出。

| 参数 | 默认 | 说明 |
|---|---|---|
| `offset` | 0 | stdout 增量偏移：只返回 `stdout[offset:]`，并回 `stdout_offset` |

不存在返回 404。

### `POST /scripts/{task_id}/cancel`

取消正在跑的执行（杀 cgroup + 停 unit）。没在跑返回 404。

```json
{"task_id": "t-1", "status": "killed", "cancelled": true}
```

### `POST /scripts/{task_id}/clear`

清空该任务的 `/workspace`（下次执行是全新环境）。

### `GET /scripts/{task_id}/stream`

实时输出（SSE，`text/event-stream`）。事件类型：`stdout` / `stderr` / `status` / `done`。
token 走请求头（不放 URL）。

### `GET /scripts/{task_id}/tree`

workspace 文件树（带大小 / mime），UI 的产物列表用它。

```json
{
  "task_id": "t-1",
  "workspace": "/var/lib/berth/scripts/t-1/workspace",
  "files": [
    {"path": "report.html", "name": "report.html", "is_dir": false,
     "size": 1234, "mime": "text/html", "modified_at": "2026-01-01T10:00:00"}
  ]
}
```

### `PUT /scripts/{task_id}/files`

上传文件到 `/workspace`（B3）。

- `multipart/form-data`：多个文件（`filename` 是 workspace 里的相对路径），也可以直接传 tar 包（自动解包）
- 其它 content-type：按原始 body 处理（tar 包；带 `?filename=` 时当单文件写）

```bash
# 多文件
curl -X PUT localhost:9000/scripts/t-1/files \
  -F 'files=@tests/test_a.py' -F 'files=@tests/test_b.py'

# tar 包（自动解包）
tar czf tests.tgz tests/
curl -X PUT localhost:9000/scripts/t-1/files --data-binary @tests.tgz
```

返回：`{"task_id": "t-1", "workspace": "...", "files": ["tests/test_a.py", ...]}`

安全：task_id 只能是安全字符；解包拒绝绝对路径、`..` 穿越、越界软链接。

### `GET /scripts/{task_id}/files`

把 workspace（或 `?path=` 指定的子目录）打包成 tar 下载。

### `GET /scripts/{task_id}/files/{path}`

下载单个文件。

| 参数 | 默认 | 说明 |
|---|---|---|
| `preview` | false | `true` = 内联预览（HTML 会带严格 CSP） |

下载响应带 `Content-Disposition` 和 `X-Content-Type-Options: nosniff`。
HTML 预览额外带 `Content-Security-Policy: sandbox; default-src 'none'`（只能放在 `<iframe sandbox>` 里看）。

### `DELETE /scripts/{task_id}/files/{path}`

删 workspace 里的文件 / 目录。

---

## 长驻服务

> **名字即身份**：`{ref}` 可以写服务名（推荐）或 `svc-xxxx` id。
> 同一个名字永远不会有两个实例；重复的 ensure 调用是安全幂等的。

### `PUT /services/{ref}`

**ensure 语义**：保证名为 `{ref}` 的服务处于运行状态。

| 现状 | 行为 |
|---|---|
| 名字不存在 | 创建，`"reused": false` |
| 已在运行 | 直接返回现有实例，`"reused": true`（不新建、不重启、**不重复执行 init**） |
| 已停止/掉线 | 原样再起，复用数据卷，`"reused": true` |
| 名字被不同模板占用 | `409`（换名字，或先 DELETE） |

**请求体**

| 字段 | 类型 | 说明 |
|---|---|---|
| `template` | string? | 模板名，如 `redis` / `mysql` / `postgres` |
| `spec` | object? | 内联模板（起"被测服务"用；与 `template` 二选一，B11） |
| `overrides` | object? | 覆盖模板：`env` / `args` / `command` / `resources` / `ports` / `network` / `mounts` / `healthcheck`（B5） |
| `init` | object? | 健康检查通过后执行的初始化（SQL / 脚本 / workspace 文件）（B5） |
| `labels` | object? | 标签（B12） |
| `task_id` | string? | 关联任务：服务 workspace 与该任务共享（`init.file` 从这里读） |

`overrides` 合并规则：`env` / `resources` / `healthcheck` 是**合并**（覆盖同名键），
`args` / `command` / `ports` / `mounts` 是**整体替换**。

`init` 结构：

| 字段 | 说明 |
|---|---|
| `sql` | SQL 文本（写进 workspace 的 `.berth-init.sql`，用模板的 init 命令执行） |
| `script` | 脚本（写进 `.berth-init.sh`；模板没定义 init 段时用 `sh -c` 执行） |
| `file` | 服务 workspace（或挂载白名单目录）里的文件，内容当 SQL / 脚本 |
| `env` | 初始化时额外的环境变量 |
| `timeout` | 初始化超时秒数 |
| `image` | 用哪个镜像跑初始化（默认服务镜像） |

模板里的 `init` 段决定"怎么执行"：

```yaml
init:
  image: postgres
  command: ["/usr/bin/psql"]
  args: ["-h", "127.0.0.1", "-p", "{port}", "-U", "postgres", "-d", "postgres", "-f", "{init_file}"]
  env:
    PGPASSWORD: "{POSTGRES_PASSWORD}"
  timeout: 60
```

可用占位符：`{port}` `{service_id}` `{name}` `{init_file}` `{init_sql}` `{init_script}`，
以及模板 `env` 里的键（如 `{POSTGRES_PASSWORD}`）。

**响应**

```json
{
  "service_id": "svc-16b83efe",
  "name": "my-redis",
  "template": "redis",
  "unit": "berth-svc-16b83efe",
  "host": "localhost",
  "port": 16379,
  "internal_port": 6379,
  "status": "running",
  "dir": "/var/lib/berth/services/svc-16b83efe",
  "persist": 1,
  "reused": false,
  "labels": {"project": "foo"},
  "network": {"mode": "host", "allow": []},
  "spec": { "...生效的完整模板..." },
  "init_result": {"exit_code": 0, "stdout": "...", "duration": 0.4},
  "created_at": "...",
  "updated_at": "..."
}
```

错误：`400` 模板不存在/缺字段/镜像没构建/参数非法；`409` 名字冲突；`504` 健康检查超时（会自动清理）；
`500` 初始化失败（服务会被清理）。

```bash
# 起两个 postgres：库名 / 密码 / 初始数据各不相同
curl -s -X PUT localhost:9000/services/app1-db \
  -H 'Content-Type: application/json' \
  -d '{"template":"postgres","overrides":{"env":{"POSTGRES_DB":"app1","POSTGRES_PASSWORD":"p1"}},
       "init":{"sql":"CREATE TABLE IF NOT EXISTS users(id int);"}}'

curl -s -X PUT localhost:9000/services/app2-db \
  -H 'Content-Type: application/json' \
  -d '{"template":"postgres","overrides":{"env":{"POSTGRES_DB":"app2","POSTGRES_PASSWORD":"p2"}},
       "init":{"sql":"CREATE TABLE IF NOT EXISTS orders(id int);"}}'
```

### `GET /services`

列出服务。默认只返回**活跃**（starting/running）服务 —— 等价 `docker ps`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `all` | false | `true` 含历史（stopped/crashed）—— 等价 `docker ps -a` |
| `status` | - | 精确过滤：`starting` / `running` / `stopped` / `crashed` |
| `usage` | true | `false` 跳过用量统计（服务多时更快） |
| `label` | - | 标签过滤，可重复：`?label=project=foo` |

每行附两个用量视图：`usage`（机器可读原始值）和 `usage-h`（人类可读，`-h` = human）。

```json
"usage": {
  "memory_bytes": 131440640,       // 当前实际占用（cgroup 实时）
  "memory_limit_bytes": 1073741824,// 上限（模板 resources.memory_mb）
  "cpu_time_ms": 1131.6,           // 启动至今累计消耗的 CPU 时间（不是百分比）
  "disk_bytes": 130500281          // 服务目录占用（overlay + 数据卷）
},
"usage-h": {
  "memory": "125.4 MiB",
  "memory_limit": "1.0 GiB",
  "cpu_time": "1.1s",
  "disk": "124.5 MiB"
}
```

### `GET /services/{ref}`

单个服务详情（同样带 `usage`，另含 `spec` / `init_result` / `network` / `labels`）。不存在返回 404。

### `POST /services/{ref}/stop`

停止（保留记录与数据卷；之后可用 `/start` 原样再起）。

### `POST /services/{ref}/start`

把**已停止**的服务原样再起：复用原目录与数据卷，端口优先复用上次的。
已在运行会返回 400。重启会**重放 init**。

### `POST /services/{ref}/reset`

重置服务（B8）：停止 → 清数据卷 → 重新初始化（重放 `init`）→ 启动。

| 参数 | 默认 | 说明 |
|---|---|---|
| `keep_backup` | false | `true` 时把旧数据卷挪到 `<dir>/volumes.backup.<ts>`（不删） |

```json
{
  "service_id": "svc-16b83efe",
  "name": "my-redis",
  "status": "running",
  "port": 16379,
  "reset_duration_ms": 842,
  "init": {"sql": "..."},
  "init_result": {"exit_code": 0, "...": "..."},
  "backup": null
}
```

### `DELETE /services/{ref}`

停止并**删除记录**。

| 参数 | 默认 | 说明 |
|---|---|---|
| `volumes` | false | `true` 时连数据卷一起删（`docker rm -v` 语义） |

```bash
curl -s -X DELETE localhost:9000/services/my-redis                 # 删记录，卷留着
curl -s -X DELETE "localhost:9000/services/my-redis?volumes=true"  # 连数据一起删干净
```

### `GET /services/{ref}/logs`

服务日志（B9，封装 `journalctl -u berth-svc-{id}`）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `tail` | 100 | 取最后 N 行（1-10000） |
| `since` | - | `journalctl --since` 的时间表达式（如 `-1h`） |
| `follow` | false | `true` = SSE 实时跟随 |

```json
{
  "service_id": "svc-16b83efe",
  "name": "my-redis",
  "unit": "berth-svc-16b83efe",
  "text": "…journal 输出…",
  "lines": ["…", "…"]
}
```

---

## 数据卷

### `GET /volumes`

所有数据卷一览（含孤儿：记录已删但卷还在磁盘上的）：

```json
[
  {
    "service_id": "svc-652bafb3",
    "name": "mysql",
    "template": "mysql",
    "status": "running",
    "path": "/var/lib/berth/services/svc-652bafb3/volumes",
    "bytes": 130471609,
    "orphan": false
  }
]
```

### `DELETE /volumes/{service_id}`

**只允许清理孤儿卷**（记录已删但卷还在磁盘上）。非孤儿卷返回 `409`。

```bash
curl -s -X DELETE localhost:9000/volumes/svc-652bafb3
```

---

## 镜像 / 模板

### `GET /images`

已发现的镜像 + 管理信息。

```json
[
  {
    "name": "base",
    "path": "/opt/berth/rootfs",
    "size_bytes": 381866176,
    "managed": false,          // 是否归 berth 管（rootfs-* 目录）→ 才允许删
    "deletable": false,        // 现在能不能删
    "in_use_by": [],           // 哪些服务在用
    "is_base": true            // 是不是基础镜像
  }
]
```

`size_bytes` 是目录实际占用（`du -sb`），不是文件系统余量。

### `POST /images` — 制作镜像

从已有镜像复制一份，装上东西，生成新的 `rootfs-<name>`。

**这是宿主 root 级操作**（chroot + apt 的 maintainer script 以 root 跑），默认关闭：
要设 `BERTH_ALLOW_IMAGE_BUILD=1` 并重启 daemon，否则返回 `403`。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `name` | string | ✔ | 镜像名（`^[a-z0-9][a-z0-9._-]{0,31}$`） |
| `base` | string | `base` | 从哪个镜像复制 |
| `packages` | string[] | - | apt 安装的包 |
| `pip_packages` | string[] | - | pip 安装的包 |
| `commands` | string[] | - | 在 chroot 里执行的命令（每条一行） |
| `force` | bool | false | 目标已存在时重建 |

```bash
curl -s -X POST localhost:9000/images -H 'Content-Type: application/json' \
  -d '{"name":"myimg","base":"base","packages":["jq"],"pip_packages":["httpx==0.27"]}'
```

```json
{
  "build_id": "img-c7cb82c7",
  "name": "myimg",
  "status": "building",
  "spec": {"name": "myimg", "base": "base", "source": "/opt/berth/rootfs", "target": "/opt/berth/rootfs-myimg", "packages": ["jq"]}
}
```

构建在后台跑（`202 Accepted`），进度查 `GET /images/builds/{build_id}`。
构建过程跑在 mount/pid/uts/ipc namespace 里：装的包起的后台进程不会漏到宿主。

### `GET /images/builds` / `GET /images/builds/{build_id}`

构建记录列表 / 单个构建的状态 + 输出日志。

```json
{
  "id": "img-c7cb82c7",
  "name": "myimg",
  "status": "success",
  "spec": {"packages": ["jq"]},
  "log": "[1/6] 复制 ...\n[6/6] 完成: /opt/berth/rootfs-myimg（202M）",
  "image_path": "/opt/berth/rootfs-myimg",
  "error": null,
  "created_at": "2026-01-01T10:00:00+00:00",
  "finished_at": "2026-01-01T10:02:31+00:00"
}
```

`status`：`building` / `success` / `failed`。

### `DELETE /images/{name}`

删除一个镜像（只删 berth 自己管的 `rootfs-*` 目录）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `force` | false | 镜像正被服务使用时：**先停掉这些服务**（保留服务记录和数据卷）再删 |

流程（`force=true`）：

1. 找出占用者（按**解析后的镜像路径**比较 —— 老记录没有 `spec` 列，会回退到模板的 `image` 字段）
2. 逐个 `stop`（**停，不删** —— 服务记录和数据卷都留着）
3. 等内核释放 overlay 挂载（最多 3s）
4. 删镜像目录

拒绝（`409`）的情况：

- `base` 镜像（其它镜像都基于它）
- 不在受管范围内（`images.yaml` 指向别处的镜像）
- 正被服务使用（**不带 `force` 时**：提示是哪些服务在用、让调用方决定要不要 force）
- **正被内核挂载引用**（扫 `/proc/self/mountinfo` 的 overlay `lowerdir`）—— 硬保护，停完还挂着就拒绝
- 正在构建中

```bash
curl -s -X DELETE localhost:9000/images/myimg
# {"name":"myimg","path":"/opt/berth/rootfs-myimg","deleted":true,"size_bytes":193694064,"stopped_services":[]}

# 被服务占用时：先看提示
curl -s -X DELETE localhost:9000/images/redis
# 409 {"detail":"镜像 'redis' 正被服务使用（my-redis）：加 force=true 会先停止这些服务（保留记录和数据卷）再删除"}

# 确认后：自动停掉占用者再删
curl -s -X DELETE "localhost:9000/images/redis?force=true"
# {"name":"redis","deleted":true,"stopped_services":["my-redis"]}
```

删除用 `BERTH_ALLOW_IMAGE_DELETE`（默认 `1`）控制。

### `GET /templates` / `GET /templates/{name}`

模板名列表 / 模板内容（校验过的）。

### `POST /templates/validate`

校验一份模板（YAML 文本或 JSON 对象）。合法返回 `{"valid": true, "template": {...}}`，
不合法返回 `400` + `detail`（带迁移提示）。

---

## 系统信息

### `GET /system/info`

版本、配置的上限、白名单挂载目录、镜像列表、网络策略能力。

```json
{
  "version": "0.3.0",
  "limits": {"timeout_seconds": 600, "memory_mb": 4096, "cpu_percent": 400, "processes": 512},
  "mount_roots": ["/srv/berth-mounts"],
  "images": {"base": "/opt/berth/rootfs"},
  "templates": ["mysql", "postgres", "redis"],
  "network": {
    "modes": ["host", "none", "allow"],
    "mechanism": "systemd IPAddressDeny/IPAddressAllow (cgroup v2 + BPF)",
    "cgroup_v2": true,
    "supported": true
  },
  "auth": {"enabled": true, "scheme": "Bearer"},
  "ui": {"enabled": true, "path": "/ui/"},
  "home": "/var/lib/berth"
}
```

### `GET /system/stats`

宿主 CPU / 内存 / `BERTH_HOME` 所在磁盘的占用 + berth 自己的统计 + 告警。

```json
{
  "cpu": {"percent": 12.3, "cores": 8, "load": [0.5, 0.4, 0.3]},
  "memory": {"total_bytes": 16777216000, "used_bytes": 8000000000, "available_bytes": 8777216000, "percent": 47.7},
  "disk": {"path": "/var/lib/berth", "total_bytes": 107374182400, "used_bytes": 21474836480, "free_bytes": 85899345920, "percent": 20.0},
  "services": {"active": 2, "total": 5},
  "runs": {"last_24h": 12, "failed_24h": 1},
  "volumes": {"total": 3, "orphan": 1},
  "alerts": ["磁盘占用 92%（/var/lib/berth），接近写满"]
}
```

---

## 访问令牌

### `POST /tokens/bootstrap`

用一次性引导码换令牌（**这个接口本身不需要已有 token**）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string | 引导码（`BERTH_TOKEN_FILE` 同目录的 `bootstrap.code`） |
| `name` | string | 令牌名 |
| `user` | string | 令牌归属的 user（不存在会自动建） |
| `ttl` | int | 有效期（秒），0 = 永不过期 |

返回同 `POST /tokens`（含明文 `token`）。错码 `401`；连续错码 5 次 → `429`。

### `GET /tokens`（admin）

列出令牌（**不含明文/哈希**）：id、名字、作用域、创建时间、最近使用时间、吊销时间。

### `POST /tokens`（超级管理员）

签发令牌（绑定一个 user）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | 令牌名（审计里显示） |
| `user` | string | 令牌归属的 user（**不存在会自动建**） |
| `ttl` | int | 有效期（秒），0 = 永不过期 |

```json
{
  "id": "tk-712c6e0d01",
  "name": "shakedown-shop",
  "user": "shop",
  "ttl": 3600,
  "expires_at": "2026-01-01T11:00:00+00:00",
  "created_at": "2026-01-01T10:00:00+00:00",
  "last_used_at": null,
  "revoked_at": null,
  "token": "bt_xxxxxxxxxxxxxxxx",
  "note": "token 明文只显示这一次，请立刻保存"
}
```

### `DELETE /tokens/{id}`（admin）

吊销令牌（立刻失效，调用方开始收到 401）。

### 命令行等价物

```bash
python3 scripts/berth_token.py create --name shakedown-shop --project shop
python3 scripts/berth_token.py list
python3 scripts/berth_token.py revoke tk-712c6e0d01
```

---

## user（租户/项目）

### `GET /users`（超级管理员）

列出所有 user：名字、id、配额、创建时间、资源数（服务/执行）。

### `POST /users`（超级管理员）

建一个 user。`{name, quota_runs?, quota_memory_mb?, quota_services?, notes?}`
（配额 `0` = 不限）。**令牌引用不存在的 user 时会自动建。**

### `GET /users/{name}` / `GET /users/{name}/resources`

user 详情 / 名下资源汇总。本人或超管可读。

### `PATCH /users/{name}`（超级管理员）

改配额：`{"quota_runs": 10, "quota_services": 5}`

### `DELETE /users/{name}`（超级管理员）

删 user 记录。**它名下的服务/执行不会被自动删**（要自己清），避免误删一整套环境。

---

## 操作审计

### `GET /events`

所有写操作的审计记录（`actor` 是 token 的短哈希，不落明文）。

```json
[
  {"id": 42, "ts": "2026-01-01T10:00:00+00:00", "actor": "token:930bbdc51b6a",
   "action": "PUT /services/my-redis", "target": "my-redis",
   "method": "PUT", "path": "/services/my-redis", "status": 200, "detail": null}
]
```

---

## 其他

### `GET /health`

```json
{"status": "ok", "version": "0.3.0"}
```

### `GET /ui/`

管理界面（静态页，见 [docs/ui.md](ui.md)）。

---

## 镜像

`image` 字段 / 模板里的 `image` 都是**名字**，按下面顺序解析（`sandbox/images.py`）：

1. 绝对路径：直接当 rootfs 用（如 `/opt/berth/rootfs`）
2. `images.yaml`（默认仓库根目录，可用 `BERTH_IMAGES_FILE` 覆盖）：

   ```yaml
   images:
     base: /opt/berth/rootfs
     node: /opt/berth/rootfs-node
   ```

3. 自动发现：`BERTH_ROOTFS`（默认 `/opt/berth/rootfs`）的同级 `rootfs-*` 目录 ——
   正好匹配 `scripts/setup_rootfs.sh` 的产物（`rootfs-redis` → 名字 `redis`）。

内置镜像：`base`（python3）、`redis`、`mysql`（MariaDB）、`postgres`（PostgreSQL 15）、
`runner`（pytest / httpx / Playwright + Chromium）；`--stack node,go,java` 可按需构建被测运行时。

---

## 服务模板（templates/{name}.yaml）

模板 ≈ 「`docker run` 的固定参数模板」。**字段语义与 Kubernetes 对齐**：

| | 可执行文件 | 参数 |
|---|---|---|
| Dockerfile | `ENTRYPOINT` | `CMD` |
| docker-compose | `entrypoint:` | `command:` |
| Kubernetes | `command:` | `args:` |
| **berth 模板** | `command:` | `args:` |

> 规则：**最终 argv = command + args**。
> 注意：berth 和 docker-compose 是两套命名——compose 的 `entrypoint` 对应 berth 的 `command`，
> compose 的 `command` 对应 berth 的 `args`。

### 字段表

| 字段 | 类型 | 必填 | 说明 | Docker 对照 |
|---|---|---|---|---|
| `name` | string | ✔ | 服务名 | `--name` |
| `image` | string | ✔ | 镜像名（见「镜像」一节） | `image`（但是本地 rootfs 名字） |
| `command` | list | ✔ | 可执行程序 argv | `ENTRYPOINT` / k8s `command` |
| `args` | list | - | 追加参数；支持 `{port}`/`{service_id}`/`{name}` 占位符 | `CMD` / k8s `args` |
| `env` | map | - | 环境变量（值同样支持占位符） | `environment` |
| `ports` | list | ✔ | `[{internal, external_range}]`，宿主端口**动态分配** | `-p`（分配区间而非写死） |
| `volumes` | list | - | `[{internal, persist}]`，数据卷在服务目录 `volumes/` 下 | `volumes` |
| `healthcheck` | map | - | `{type: tcp\|http, port, timeout, interval, path, expect_status}` | `healthcheck` |
| `resources` | map | - | `{memory_mb, cpu_percent}`（100 = 1 核） | `--memory` / `--cpus` |
| `network` | map | - | `{mode: host\|none\|allow, allow: [...]}`（B2） | - |
| `init` | map | - | 初始化怎么执行（`{image, command, args, env, timeout}`，B5） | - |
| `mounts` | list | - | 只读挂载 `[{source, target, read_only}]`（白名单内，B11） | `-v ro` |

### 健康检查

```yaml
healthcheck:
  type: tcp          # tcp（默认）或 http
  port: 5432
  timeout: 20
  interval: 0.3
---
healthcheck:
  type: http         # B10：被测 Web 服务用（tcp 通了不代表应用就绪）
  port: 3000
  path: /healthz
  expect_status: 200 # 可以是数组 [200, 204]
  timeout: 30
  interval: 0.5
```

### 校验

未知字段**直接报错**，并尽量给出 docker/compose 习惯的迁移提示。完整示例：

```yaml
name: redis
image: redis                            # 镜像名（自动发现 /opt/berth/rootfs-redis）
command: ["/usr/bin/redis-server"]
args: ["--port", "{port}", ...]         # {port}/{service_id}/{name} 占位符替换
env: {}
ports:
  - internal: 6379
    external_range: [16379, 16479]      # 宿主端口分配区间
volumes:
  - internal: /var/lib/redis
    persist: true                       # 数据保留（停止时不删）
healthcheck:
  type: tcp
  port: 6379
  timeout: 20
  interval: 0.3
resources:
  memory_mb: 512
  cpu_percent: 100
```

常见的被拦写法：

| 写了 | 报错提示 |
|---|---|
| `entrypoint:` | 用 `command`（berth 同 K8s 语义） |
| `image_rootfs:` | 用 `image: <名字>`（镜像名表） |
| `environment:` | 用 `env` |
| `ports: ["6379:6379"]` | 用 `{internal, external_range}`（端口动态分配） |
| `mem_limit:` / `cpus:` | 用 `resources.memory_mb` / `resources.cpu_percent` |

---

## 状态与恢复语义

- daemon 重启：读 SQLite 中 `running` 的服务 → `systemctl is-active` 仍活着 → 保持 `running`；死了 → 标 `crashed` 并释放端口
- 启动时清理孤儿：宿主上存在但 SQLite 无记录的 **`berth-svc-*` / `berth-run-*`** 单元 → 强杀 + reset-failed；
  daemon 自己的 unit 不在清理范围内；没有活跃服务引用的端口记录 → 释放
- 服务 unit 全部用 `systemd-run --collect` 创建，停止后自动从 systemd 卸载
- 名字唯一性在应用层保证（同名 ensure 见 PUT 一节）；历史记录用 `DELETE` 清
