# berth（泊位）

> 给每个任务一个泊位，给每个服务一个泊位：跑完即走（`POST /scripts/run`），或停泊常驻（`PUT /services/{name}`，systemd 托管，名字即身份）。

berth 是一个轻量沙箱服务（HTTP API），用来**跑一次性脚本**和**起长驻服务**（redis 等）。
沙箱不用 Docker、不用 bwrap，直接复用 Linux 原语：

- **overlayfs** —— 只读基础镜像 + 每个任务独立可写层，跑完即弃
- **unshare + pivot_root + setpriv** —— PID/mount/IPC/UTS 隔离、换根、降权到 uid 1000 并清空 capability
- **systemd** —— 一次性脚本用 `systemd-run --scope`，长驻服务用 `systemd-run --unit=berth-xxx` 托管
- **host 网络** —— 沙箱直接用宿主网络栈，可进可出、零配置
- **SQLite** —— 执行历史 / 服务记录 / 端口分配；重启不丢状态

## 环境要求

| 要求 | 说明 |
|---|---|
| Linux + **systemd** | Debian 12 / Ubuntu 22.04+ 验证过；需要真实的 systemd（推荐 VM 或裸机） |
| **root 权限** | 直接以 root 运行；或普通用户 + 免密 sudo（见下） |
| 内核 | overlayfs；标准发行版内核都满足 |
| 构建镜像 | `debootstrap`（安装脚本会自动装） |
| Python | 3.10+ |

> 不建议在受限容器里运行：`unshare` 新 PID namespace 常被 seccomp 拦，overlayfs 也可能被禁。VM / 裸机上没有这些问题。

## 在 macOS 上（需要一台 Linux）

上面这些内核依赖 macOS 都没有 —— 和 Docker Desktop 必须内置一个 Linux VM 是同一个道理。
berth 的选择是用现成的通用 VM（推荐 Lima），一条命令装好：

```bash
git clone https://github.com/zsgh/berth.git && cd berth
scripts/macos-bootstrap.sh     # 建 VM(4C/8G/40G) → 装 berth → 注册 systemd 服务 → 验证
curl http://localhost:9000/health
```

- 资源都是**上限不是预留**（空闲≈0），端口自动转发到 Mac 的 localhost
- 已有 Linux（远程服务器 / WSL2 / 其他 VM）不需要这个脚本，直接在那台上跑 berth 就行
- 细节（管理命令、共享目录的坑、排障）见 [docs/macos.md](docs/macos.md)

## 安装（从零开始）

```bash
git clone https://github.com/zsgh/berth.git
cd berth

# 1) Python 依赖（虚拟环境可选）
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2) 构建沙箱基础镜像（debootstrap，约 3-5 分钟 / 300MB，装到 /opt/berth/rootfs）
sudo scripts/setup_rootfs.sh

# 3) 需要长驻服务镜像的话（redis / mysql，按需选择）
sudo scripts/setup_rootfs.sh --with-redis
sudo scripts/setup_rootfs.sh --with-mysql     # Debian 下是 MariaDB（协议兼容）
```

镜像就是普通目录，berth 把它当 overlayfs 的只读下层用。
自定义位置：`sudo PREFIX=/srv/berth scripts/setup_rootfs.sh`，运行时用 `BERTH_ROOTFS=/srv/berth/rootfs` 指过去。

## 运行

```bash
# 以 root 运行（简单直接）
sudo -E $(which uvicorn) server:app --host 0.0.0.0 --port 9000
# 或者装了 venv：sudo -E .venv/bin/uvicorn server:app --host 0.0.0.0 --port 9000
```

不想用 root？给运行用户配免密 sudo 即可（berth 需要调用 `systemd-run` / `systemctl` / `chown` 等）：

```bash
echo "$USER ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/berth
chmod 440 /etc/sudoers.d/berth
# 然后直接：uvicorn server:app --host 0.0.0.0 --port 9000
```

### （可选）交给 systemd 开机自启

```ini
# /etc/systemd/system/berth.service
[Unit]
Description=berth sandbox daemon
After=network.target

[Service]
WorkingDirectory=/opt/berth-app            # 改成你的 clone 目录
ExecStart=/usr/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port 9000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now berth
```

## 快速验证

```bash
# 健康检查
curl -s http://localhost:9000/health
# {"status":"ok"}

# 跑代码（沙箱内执行，host 网络，/workspace 是持久工作区）
curl -s -X POST http://localhost:9000/scripts/run \
  -H 'Content-Type: application/json' -d '{"code": "print(\"hello\")"}'
# 也可以跑其它语言 / 直接跑命令 / 换镜像：
curl -s -X POST http://localhost:9000/scripts/run \
  -H 'Content-Type: application/json' -d '{"code": "echo $((6*7))", "language": "bash"}'
curl -s -X POST http://localhost:9000/scripts/run \
  -H 'Content-Type: application/json' -d '{"cmd": ["/bin/uname", "-s"]}'

# 起一个 redis（redis 是名字；重复调用幂等，不会重复创建）
curl -s -X PUT http://localhost:9000/services/my-redis \
  -H 'Content-Type: application/json' -d '{"template": "redis"}'
# {"service_id":"svc-xxx","host":"localhost","port":16379,"status":"running","reused":false,...}

redis-cli -h localhost -p 16379 ping          # PONG
curl -s http://localhost:9000/services        # 列表（含内存/磁盘用量）
curl -s http://localhost:9000/volumes         # 数据卷一览（含孤儿卷）
curl -s -X POST   localhost:9000/services/my-redis/stop    # 停（数据保留）
curl -s -X POST   localhost:9000/services/my-redis/start   # 原样再起
curl -s -X DELETE "localhost:9000/services/my-redis?volumes=true"  # 连数据删干净

# 起一个 mysql（MariaDB；首次启动自动初始化数据目录，root 密码见模板 env）
curl -s -X PUT http://localhost:9000/services/mysql \
  -H 'Content-Type: application/json' -d '{"template": "mysql"}'
mariadb -h 127.0.0.1 -P 13306 -u root -psandbox -e 'SELECT VERSION();'
```

完整 API 见 [docs/api.md](docs/api.md)，设计（overlayfs + systemd 怎么配合）见 [docs/design.md](docs/design.md)。

## 和 Docker 的对照

| 想做的事 | berth |
|---|---|
| `docker run image cmd` | `POST /scripts/run` `{"cmd": [...]}`（跑完即走，自动清理） |
| `docker run -d redis` | `PUT /services/my-redis` `{"template": "redis"}`（名字即身份，重复调用幂等） |
| `docker ps` | `GET /services`（默认只列活跃服务） |
| `docker ps -a` | `GET /services?all=true`（含 stopped/crashed 历史） |
| `docker stats` | `GET /services` 每行的 `usage`（内存/CPU/磁盘） |
| `docker stop` / `docker start` | `POST /services/{name}/stop` / `.../start`（数据保留） |
| `docker rm` / `docker rm -v` | `DELETE /services/{name}` / `...?volumes=true` |
| `docker volume ls` | `GET /volumes`（含孤儿卷标记） |
| `docker logs` | `journalctl -u berth-svc-{id}.service` |
| `docker exec` | `POST /scripts/run` `{"task_id": "同一 task_id", "cmd": [...]}`（共享 `/workspace`） |

## 服务模板（templates/*.yaml）

模板 ≈ 「`docker run` 的固定参数模板」，字段语义与 Kubernetes 对齐：

| | 可执行文件 | 参数 |
|---|---|---|
| Dockerfile | `ENTRYPOINT` | `CMD` |
| docker-compose | `entrypoint:` | `command:` |
| Kubernetes | `command:` | `args:` |
| **berth 模板** | `command:` | `args:` |

> 规则：**最终 argv = command + args**；`args` 支持 `{port}` / `{service_id}` / `{name}` 占位符。
> 注意：berth 与 compose 是两套命名（我们同 K8s）——compose 的 `entrypoint` 对应 `command`、
> compose 的 `command` 对应 `args`，别按 compose 习惯写。
> 写错字段会**直接报错并给迁移提示**（如 `entrypoint:` → 提示改用 `command:`，`image_rootfs:` → 提示用 `image:`）。

常用字段：`name`、`image`（镜像名：base/redis/mysql/自定义，见 `scripts/setup_rootfs.sh`）、
`command`/`args`、`env`、`ports`（`{internal, external_range}`，宿主端口**动态分配**）、
`volumes`（`{internal, persist}`）、`healthcheck`（tcp）、`resources`（`memory_mb` / `cpu_percent`）。
完整字段表见 [docs/api.md](docs/api.md)。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BERTH_HOME` | `/var/lib/berth` | 运行数据根目录（任务 overlay、workspace、服务卷） |
| `BERTH_DB` | `$BERTH_HOME/daemon.db` | SQLite 路径 |
| `BERTH_ROOTFS` | `/opt/berth/rootfs` | 基础镜像（`/scripts/run` 用） |
| `BERTH_TEMPLATES_DIR` | `<仓库>/templates` | 服务模板目录 |
| `BERTH_TRACING` | `1` | `0` 关闭 OpenTelemetry 上报；端点不可达时自动跳过 |
| `BERTH_PHOENIX_ENDPOINT` | `http://localhost:6006/v1/traces` | OTLP 收集端点 |
| `BERTH_PHOENIX_PROJECT` | `berth` | Phoenix 项目名 |

## 运行数据布局

```
/var/lib/berth/
├── daemon.db                         # SQLite：runs / services / ports
├── scripts/{task_id}/
│   ├── run-{run_id}/upper|work|newroot   # 单次执行的 overlay（用完即删）
│   └── workspace/                    # /workspace，同一 task_id 跨次保留
└── services/{service_id}/
    ├── run/upper|work|newroot        # 服务沙箱
    └── volumes/{容器内路径}/         # persist: true 的数据卷
```

仓库结构：

```
berth/
├── server.py               # FastAPI 入口（uvicorn server:app）
├── api/schemas.py          # 请求模型
├── sandbox/
│   ├── paths.py            # 路径 / 提权辅助（root 或 sudo -n 两种模式）
│   ├── overlayfs.py        # run_script：overlay+pivot_root+setpriv 执行
│   ├── images.py           # 镜像名解析（base/redis/mysql/自动发现）
│   ├── network.py          # 端口分配（host 模式无需 veth）
│   ├── service.py          # 长驻服务生命周期 + 恢复 + 孤儿清理
│   ├── systemd_units.py    # systemd unit 查询 / 有界停止
│   └── tracing.py          # OpenTelemetry → Phoenix（可选）
├── state/db.py             # SQLite：runs / services / ports
├── templates/              # redis.yaml / mysql.yaml（MariaDB）
├── tests/                  # 各模块验证脚本
├── scripts/                # setup_rootfs / macos-bootstrap / lima.yaml / smoke 脚本
└── docs/                   # api.md / design.md / macos.md
```

## 测试与冒烟

和运行 daemon 相同的权限条件下执行（root 或免密 sudo 用户）：

```bash
python3 tests/test_overlayfs.py     # 沙箱：hello / 超时 / 外网 / artifacts / OOM
python3 tests/test_db.py            # SQLite
python3 tests/test_network.py       # 端口分配
python3 tests/test_template.py      # 模板校验（未知字段拦截 / docker 迁移提示）
python3 tests/test_images.py        # 镜像名解析（自动发现 / images.yaml）
python3 tests/test_orphans.py       # 孤儿清理安全（不误伤 daemon 自身的 unit）
python3 tests/test_service.py       # redis 起停（需要 rootfs-redis + redis-cli）

scripts/smoke_services.sh           # 起 redis → 外部连上 → 停止
scripts/smoke_persistence.sh        # kill daemon → 重启 → 状态恢复 + 孤儿清理
scripts/smoke_langgraph.sh          # （dev）LangGraph agent 断点恢复；需要 agent_langgraph.py
```

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `unshare: Operation not permitted` | 在受限容器里跑了。换 VM / 裸机（或特权容器） |
| `mount -t overlay ... failed` | 内核不支持 overlayfs，或 upper/work 不在同一文件系统 |
| `/scripts/run` 报找不到 python3 | 基础镜像没建好：`sudo scripts/setup_rootfs.sh` |
| 沙箱里解析不了域名 | 宿主 DNS 变了：`sudo cp /etc/resolv.conf /opt/berth/rootfs/etc/resolv.conf` |
| 健康检查超时（服务起不来） | 看日志：`journalctl -u berth-svc-xxx.service --no-pager -n 50` |
| 提示没权限 | 以 root 运行，或配免密 sudo（见「运行」一节） |

## 已知限制

- 沙箱内固定降权到 uid 1000（无 user namespace 映射）；宿主上 uid 1000 的用户能写沙箱产物
- host 网络模式下服务直接监听宿主端口，靠 SQLite 记录 + 探测去重，不是强制隔离
- 停止/超时依赖 cgroup 强杀（`unshare` 个别情况下信号掩码不可靠，SIGTERM 不生效）
- `templates/mysql.yaml` 基于 MariaDB 10.11（MySQL 协议/客户端兼容），首次启动自动初始化数据目录；
  root 密码在模板 `env.MYSQL_ROOT_PASSWORD`（默认 `sandbox`），改密码需清掉数据卷重新初始化
