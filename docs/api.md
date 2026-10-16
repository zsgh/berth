# berth API

Base URL：`http://localhost:9000`（uvicorn 默认端口 9000）

所有接口返回 JSON。出错时返回 `{"detail": "..."}`。

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
| `image` | string | `base` | 镜像名（base / redis / mysql / 自定义，见「镜像」一节） |
| `timeout` | int | 30 | 超时秒数（1-600），超时强制终止 |
| `task_id` | string? | 自动 uuid | 同一 task_id 共享 `/workspace` |
| `memory_mb` | int | 256 | 内存上限 |
| `cpu_percent` | int | 50 | CPU 配额 |

解释器选择顺序：`language` 参数 → 代码首行 shebang（如 `#!/bin/bash`、`#!/usr/bin/env python3`）→ 默认 python。
非 python/bash/sh 的解释器也能用，前提是镜像里有（写在 shebang 里即可）。

**响应**

```json
{
  "stdout": "hello\n",
  "stderr": "",
  "exit_code": 0,
  "duration": 0.076,
  "artifacts": ["out.txt"],
  "task_id": "test-1",
  "image": "base",
  "language": "python"
}
```

- `exit_code`：进程退出码；超时被强杀为 `-1`；被信号杀死为 `128+signal`（如 OOM → 137）
- `artifacts`：`/workspace` 下的文件列表（相对路径）
- `language`：`cmd` 模式为 `null`；shebang 模式形如 `shebang:/bin/bash`

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
```

沙箱环境：host 网络（可直连外网）；文件系统 = 只读镜像 + 每次独立可写层；
以 uid 1000 运行、capability 清空、cgroup 限内存/CPU/进程数；结束后 cgroup 残留进程一并清理。

### `GET /scripts`

最近执行列表（`?limit=100`，1-1000）。

### `GET /scripts/{task_id}`

某次执行的历史记录（同 task_id 记录为最新一次）。不存在返回 404。

```json
{
  "task_id": "t-1",
  "code": "...",
  "stdout": "done\n",
  "stderr": "",
  "exit_code": 0,
  "duration_ms": 76,
  "artifacts": ["out.txt"],
  "created_at": "...",
  "finished_at": "..."
}
```

### `POST /scripts/{task_id}/clear`

清空该任务的 `/workspace`（下次执行是全新环境）。

---

## 长驻服务

> **名字即身份**：`{ref}` 可以写服务名（推荐）或 `svc-xxxx` id。
> 同一个名字永远不会有两个实例；重复的 ensure 调用是安全幂等的。

### `PUT /services/{ref}`

**ensure 语义**：保证名为 `{ref}` 的服务处于运行状态。

| 现状 | 行为 |
|---|---|
| 名字不存在 | 创建，`"reused": false` |
| 已在运行 | 直接返回现有实例，`"reused": true`（不新建、不重启） |
| 已停止/掉线 | 原样再起，复用数据卷，`"reused": true` |
| 名字被不同模板占用 | `409`（换名字，或先 DELETE） |

**请求体**

| 字段 | 类型 | 说明 |
|---|---|---|
| `template` | string | 模板名，如 `redis` / `mysql` |

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
  "created_at": "...",
  "updated_at": "..."
}
```

错误：`400` 模板不存在/缺字段/镜像没构建；`409` 名字冲突；`504` 健康检查超时（会自动清理）。

```bash
curl -s -X PUT localhost:9000/services/my-redis \
  -H 'Content-Type: application/json' -d '{"template": "redis"}'
```

### `GET /services`

列出服务。默认只返回**活跃**（starting/running）服务 —— 等价 `docker ps`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `all` | false | `true` 含历史（stopped/crashed）—— 等价 `docker ps -a` |
| `status` | - | 精确过滤：`starting` / `running` / `stopped` / `crashed` |
| `usage` | true | `false` 跳过用量统计（服务多时更快） |

每行附带 `usage`（等价 `docker stats` 的静态快照）：

```json
"usage": {
  "memory_bytes": 13598720,        // 当前内存（cgroup 实时值）
  "memory_limit_bytes": 536870912, // 内存上限（模板 resources）
  "cpu_time_ms": 9324.6,           // CPU 累计时间
  "disk_bytes": 49537              // 服务目录实际占用（overlay + 数据卷）
}
```

### `GET /services/{ref}`

单个服务详情（同样带 `usage`）。不存在返回 404。

### `POST /services/{ref}/stop`

停止（保留记录与数据卷；之后可用 `/start` 原样再起）。

### `POST /services/{ref}/start`

把**已停止**的服务原样再起：复用原目录与数据卷，端口优先复用上次的。
已在运行会返回 400。

### `DELETE /services/{ref}`

停止并**删除记录**。

| 参数 | 默认 | 说明 |
|---|---|---|
| `volumes` | false | `true` 时连数据卷一起删（`docker rm -v` 语义） |

```bash
curl -s -X DELETE localhost:9000/services/my-redis                 # 删记录，卷留着
curl -s -X DELETE "localhost:9000/services/my-redis?volumes=true"  # 连数据一起删干净
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

清理孤儿卷：记录没了就直接 `sudo rm -rf <path>`。

---

## 其他

### `GET /health`

```json
{"status": "ok"}
```

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
| `healthcheck` | map | - | `{type: tcp, port, timeout, interval}` | `healthcheck`（仅 tcp） |
| `resources` | map | - | `{memory_mb, cpu_percent}`（100 = 1 核） | `--memory` / `--cpus` |

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
