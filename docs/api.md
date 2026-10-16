# berth API

Base URL：`http://localhost:9000`（uvicorn 默认端口 9000）

所有接口返回 JSON。出错时返回 `{"detail": "..."}`.

---

## 脚本执行

### `POST /scripts/run`

在 overlayfs 沙箱里跑一段 Python 代码，跑完自动清理 overlay（`/workspace` 保留）。

**请求体**

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `code` | string | 必填 | 要执行的 Python 代码 |
| `timeout` | int | 30 | 超时秒数（1-600），超时强制终止 |
| `task_id` | string? | 自动 uuid | 任务标识；同一 task_id 共享 `/workspace` |
| `memory_mb` | int | 256 | 内存上限 |
| `cpu_percent` | int | 50 | CPU 配额 |

**响应**

```json
{
  "stdout": "hello\n",
  "stderr": "",
  "exit_code": 0,
  "duration": 0.076,
  "artifacts": ["out.txt"],
  "task_id": "test-1"
}
```

- `exit_code`：进程退出码；超时被强杀为 `-1`；被信号杀死为 `128+signal`（如 OOM → 137）
- `artifacts`：`/workspace` 下的文件列表（相对路径）

**示例**

```bash
curl -s -X POST http://localhost:9000/scripts/run \
  -H "Content-Type: application/json" \
  -d '{"code": "open(\"/workspace/out.txt\",\"w\").write(\"done\")", "task_id": "t-1"}'
```

沙箱环境说明：

- 宿主网络（host 模式），可直连外网
- 文件系统 = 只读 rootfs + 每任务独立可写层；`/workspace` 绑定到宿主持久目录
- 进程以 uid 1000 运行，capability 清空，cgroup 限制内存/CPU/进程数
- 超时或结束后，cgroup 里的残留进程会被一并清理

### `GET /scripts`

最近执行列表。

| 参数 | 默认 | 说明 |
|---|---|---|
| `limit` | 100 | 最多返回条数（1-1000） |

```bash
curl -s "http://localhost:9000/scripts?limit=10"
```

### `GET /scripts/{task_id}`

某次执行的历史记录（同 task_id 多次执行，记录为最新一次）。不存在返回 404。

```json
{
  "task_id": "t-1",
  "code": "...",
  "stdout": "done\n",
  "stderr": "",
  "exit_code": 0,
  "duration_ms": 76,
  "artifacts": ["out.txt"],
  "created_at": "2026-10-15T12:34:42+00:00",
  "finished_at": "2026-10-15T12:34:42+00:00"
}
```

### `POST /scripts/{task_id}/clear`

清空该任务的 `/workspace`（下次执行是全新环境）。

```json
{"status": "cleared", "task_id": "t-1"}
```

---

## 长驻服务

### `POST /services`

按模板启动一个长驻服务（systemd 托管），等到健康检查通过才返回。

**请求体**

| 字段 | 类型 | 说明 |
|---|---|---|
| `template` | string | 模板名，如 `redis` |
| `name` | string? | 服务名（同名幂等，见下） |

**同名幂等（ensure 语义）** —— 重复调用永远不会重复创建：

| 情况 | 行为 |
|---|---|
| 名字不存在 | 新建，响应 `"reused": false` |
| 同名且已在运行 | 直接返回现有实例，`"reused": true`（不新建、不重启） |
| 同名但已停止/掉线 | 原样再起，复用数据卷，`"reused": true` |
| 同名但模板不同 | `409`（换个名字，或先 DELETE） |
| 不传 `name` | 每次新建匿名实例（名字自动取 service_id） |

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

错误：`400` 模板不存在/缺字段；`409` 同名冲突；`504` 健康检查超时（服务会被自动清理）。

### `GET /services`

列出服务。默认只返回**活跃**（starting/running）服务 —— 等价于 `docker ps`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `all` | false | `true` 时把历史记录（stopped/crashed）也带上 —— 等价于 `docker ps -a` |
| `status` | - | 精确过滤：`starting` / `running` / `stopped` / `crashed` |

返回前会核对 systemd 实际状态：unit 已不在的会修正为 `crashed`（默认列表里就不再出现）。

```bash
curl -s http://localhost:9000/services               # 活跃服务
curl -s "http://localhost:9000/services?all=true"    # 含历史
curl -s "http://localhost:9000/services?status=stopped"
```

### `GET /services/{service_id}`

服务详情。无此服务返回 404。

### `DELETE /services/{service_id}`

停止并清理：`systemctl stop` → 删除 overlay 工作目录 →（`persist: false` 时删数据卷）→ 释放端口。

| 参数 | 默认 | 说明 |
|---|---|---|
| `purge` | false | `true` 时连 SQLite 记录一起删除（history 列表里也不再出现） |

```bash
curl -s -X DELETE http://localhost:9000/services/svc-16b83efe
curl -s -X DELETE "http://localhost:9000/services/svc-16b83efe?purge=true"   # 连记录一起清
```

### `POST /services/{service_id}/start`

把**已停止**的服务原样再起：复用原目录与数据卷（`persist: true` 的数据还在），端口重新分配（优先复用上次的端口）。

```bash
curl -s -X POST http://localhost:9000/services/svc-16b83efe/start
# → {"service_id":"svc-16b83efe","port":13307,"status":"running",...}
```

已在运行的服务调用会返回 400（需先 DELETE 再 start）。

---

## 其他

### `GET /health`

```json
{"status": "ok"}
```

---

## 模板格式（templates/{name}.yaml）

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
| `image_rootfs` | string | ✔ | 本地 rootfs 目录（`setup_rootfs.sh` 构建） | `image`（但非仓库引用） |
| `command` | list | ✔ | 可执行程序 argv | `ENTRYPOINT` / k8s `command` |
| `args` | list | - | 追加参数；支持 `{port}`/`{service_id}`/`{name}` 占位符 | `CMD` / k8s `args` |
| `env` | map | - | 环境变量（值同样支持占位符） | `environment` |
| `ports` | list | ✔ | `[{internal, external_range}]`，宿主端口**动态分配** | `-p`（但我们是分配区间而非写死） |
| `volumes` | list | - | `[{internal, persist}]`，数据卷放服务目录 `volumes/` 下 | `volumes` |
| `healthcheck` | map | - | `{type: tcp, port, timeout, interval}` | `healthcheck`（仅 tcp） |
| `resources` | map | - | `{memory_mb, cpu_percent}`（100 = 1 核） | `--memory` / `--cpus` |

### 校验

未知字段**直接报错**，并尽量给出 docker/compose 习惯的迁移提示。完整示例：

```yaml
name: redis
image_rootfs: /opt/berth/rootfs-redis   # 预装该服务的 rootfs
command: ["/usr/bin/redis-server"]
args: ["--port", "{port}", ...]           # {port}/{service_id}/{name} 占位符替换
env: {}
ports:
  - internal: 6379
    external_range: [16379, 16479]        # 宿主端口分配区间
volumes:
  - internal: /var/lib/redis
    persist: true                         # 数据保留（停止时不删）
healthcheck:
  type: tcp                               # 目前支持 tcp
  port: 6379                              # 逻辑端口（实际探测宿主映射端口）
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
| `image:` | 用 `image_rootfs`（本地目录） |
| `environment:` | 用 `env` |
| `ports: ["6379:6379"]` | 用 `{internal, external_range}`（端口动态分配） |
| `mem_limit:` / `cpus:` | 用 `resources.memory_mb` / `resources.cpu_percent` |

---

## 状态与恢复语义

- daemon 重启：读 SQLite 中 `running` 的服务 → `systemctl is-active` 仍然活着 → 保持 `running`；死了 → 标 `crashed` 并释放端口
- 启动时清理孤儿：宿主上存在但 SQLite 无记录的 **`berth-svc-*` / `berth-run-*`** 单元 → 强杀 + reset-failed；
  daemon 自己的 unit 不在清理范围内；没有活跃服务引用的端口记录 → 释放
- 服务 unit 全部用 `systemd-run --collect` 创建，停止后自动从 systemd 卸载
