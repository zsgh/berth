# berth（泊位）

> 给每个任务一个泊位，给每个服务一个泊位：跑完即走（`POST /scripts/run`），或停泊常驻（`PUT /services/{name}`，systemd 托管，名字即身份）。

berth 是一个轻量沙箱服务（HTTP API），用来**跑一次性脚本**和**起长驻服务**（redis / mysql / postgres 等）。
沙箱不用 Docker、不用 bwrap，直接复用 Linux 原语：

- **overlayfs** —— 只读基础镜像 + 每个任务独立可写层，跑完即弃
- **unshare + pivot_root + setpriv** —— PID/mount/IPC/UTS 隔离、换根、降权到 uid 1000 并清空 capability
- **systemd** —— 一次性脚本用 `systemd-run --scope`，长驻服务用 `systemd-run --unit=berth-xxx` 托管
- **systemd cgroup BPF** —— 出网白名单（`IPAddressDeny/Allow`）
- **host 网络** —— 默认零配置；也可以按任务限制出网
- **SQLite** —— 执行历史 / 服务记录 / 端口分配 / 标签 / 审计；重启不丢状态
- **自带 UI** —— 静态页挂在 `/ui`，起服务、看日志、跑脚本、看产物、清理孤儿卷

## 环境要求

| 要求 | 说明 |
|---|---|
| Linux + **systemd** | Debian 12 / Ubuntu 22.04+ 验证过；需要真实的 systemd（推荐 VM 或裸机） |
| **root 权限** | 直接以 root 运行；或普通用户 + 免密 sudo（见下） |
| 内核 | overlayfs；网络策略还需要 cgroup v2 + BPF（可选） |
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

# 3) 需要长驻服务镜像的话（redis / mysql / postgres，按需选择）
sudo scripts/setup_rootfs.sh --with-redis
sudo scripts/setup_rootfs.sh --with-mysql     # Debian 下是 MariaDB（协议兼容）
sudo scripts/setup_rootfs.sh --with-postgres  # PostgreSQL 15（LTS）

# 4) 需要跑测试（pytest / Playwright + Chromium）的话
sudo scripts/setup_rootfs.sh --with-runner

# 5) 需要被测运行时的话（node / go / java）
sudo scripts/setup_rootfs.sh --stack node,go
```

镜像就是普通目录，berth 把它当 overlayfs 的只读下层用。
自定义位置：`sudo PREFIX=/srv/berth scripts/setup_rootfs.sh`，运行时用 `BERTH_ROOTFS=/srv/berth/rootfs` 指过去。

## 运行

```bash
# 以 root 运行（简单直接）
sudo -E $(which uvicorn) server:app --host 127.0.0.1 --port 9000
# 或者装了 venv：sudo -E .venv/bin/uvicorn server:app --host 127.0.0.1 --port 9000
# 或者用自带入口（默认 127.0.0.1:9000，可用 BERTH_HOST / BERTH_PORT 改）：
sudo -E python3 server.py
```

> **默认只监听 `127.0.0.1`**。要对外监听必须显式配置：
> `BERTH_HOST=0.0.0.0 BERTH_TOKEN=<长随机串> python3 server.py`
> 没配 token 时 API 不鉴权（只在"本机自用"的前提下安全）—— 启动日志会打警告。

不想用 root？给运行用户配免密 sudo 即可（berth 需要调用 `systemd-run` / `systemctl` / `chown` 等）：

```bash
echo "$USER ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/berth
chmod 440 /etc/sudoers.d/berth
# 然后直接：uvicorn server:app --host 127.0.0.1 --port 9000
```

### （可选）交给 systemd 开机自启

```ini
# /etc/systemd/system/berth.service
[Unit]
Description=berth sandbox daemon
After=network.target

[Service]
WorkingDirectory=/opt/berth-app            # 改成你的 clone 目录
Environment=BERTH_TOKEN=change-me         # 对外访问必须配
ExecStart=/usr/bin/python3 -m uvicorn server:app --host 127.0.0.1 --port 9000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now berth
```

## 快速验证

```bash
# 健康检查（免鉴权）
curl -s http://localhost:9000/health
# {"status":"ok","version":"0.3.0"}

# 跑代码（沙箱内执行，host 网络，/workspace 是持久工作区）
curl -s -X POST http://localhost:9000/scripts/run \
  -H 'Content-Type: application/json' -d '{"code": "print(\"hello\")"}'
# 也可以跑其它语言 / 直接跑命令 / 换镜像：
curl -s -X POST http://localhost:9000/scripts/run \
  -H 'Content-Type: application/json' -d '{"code": "echo $((6*7))", "language": "bash"}'
curl -s -X POST http://localhost:9000/scripts/run \
  -H 'Content-Type: application/json' -d '{"cmd": ["/bin/uname", "-s"]}'

# 异步执行（不阻塞）+ 查状态 + 增量读输出 + 取消
curl -s -X POST http://localhost:9000/scripts/run \
  -H 'Content-Type: application/json' -d '{"code": "import time; time.sleep(60)", "async": true, "timeout": 120}'
curl -s http://localhost:9000/scripts/<task_id>
curl -s -X POST http://localhost:9000/scripts/<task_id>/cancel

# 起一个 redis（redis 是名字；重复调用幂等，不会重复创建）
curl -s -X PUT http://localhost:9000/services/my-redis \
  -H 'Content-Type: application/json' -d '{"template": "redis"}'
# {"service_id":"svc-xxx","host":"localhost","port":16379,"status":"running","reused":false,...}

redis-cli -h localhost -p 16379 ping          # PONG
curl -s http://localhost:9000/services        # 列表（含内存/磁盘用量）
curl -s http://localhost:9000/volumes         # 数据卷一览（含孤儿卷）
curl -s -X POST   localhost:9000/services/my-redis/stop    # 停（数据保留）
curl -s -X POST   localhost:9000/services/my-redis/start   # 原样再起
curl -s -X POST   localhost:9000/services/my-redis/reset   # 重置（清卷 + 重放 init）
curl -s -X DELETE "localhost:9000/services/my-redis?volumes=true"  # 连数据删干净

# 起一个 mysql（MariaDB；首次启动自动初始化数据目录，root 密码见模板 env）
curl -s -X PUT http://localhost:9000/services/mysql \
  -H 'Content-Type: application/json' -d '{"template": "mysql"}'
mariadb -h 127.0.0.1 -P 13306 -u root -psandbox -e 'SELECT VERSION();'

# 起一个 postgres（PostgreSQL 15），顺带初始化一张表
curl -s -X PUT http://localhost:9000/services/app-db \
  -H 'Content-Type: application/json' \
  -d '{"template":"postgres",
       "overrides":{"env":{"POSTGRES_DB":"app","POSTGRES_PASSWORD":"secret"}},
       "init":{"sql":"CREATE TABLE IF NOT EXISTS users(id int);"},
       "labels":{"project":"demo"}}'
psql -h 127.0.0.1 -p 15432 -U postgres -d app -c '\dt'

# 上传用例目录 → 跑 → 下载产物
tar czf tests.tgz tests/
curl -s -X PUT http://localhost:9000/scripts/demo-1/files --data-binary @tests.tgz
curl -s -X POST http://localhost:9000/scripts/run \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"demo-1","image":"runner","cmd":["/usr/bin/pytest","-q","/workspace/tests"],"timeout":900,"memory_mb":2048}'
curl -s http://localhost:9000/scripts/demo-1/tree            # 产物文件树
curl -s -OJ http://localhost:9000/scripts/demo-1/files        # 打包下载

# 管理界面（自带，不用命令行）
open http://localhost:9000/ui/
```

完整 API 见 [docs/api.md](docs/api.md)，设计（overlayfs + systemd 怎么配合、网络隔离怎么做的）见
[docs/design.md](docs/design.md)，UI 说明与深链约定见 [docs/ui.md](docs/ui.md)。

## 和 Docker 的对照

| 想做的事 | berth |
|---|---|
| `docker run image cmd` | `POST /scripts/run` `{"cmd": [...]}`（跑完即走，自动清理） |
| `docker run -d redis` | `PUT /services/my-redis` `{"template": "redis"}`（名字即身份，重复调用幂等） |
| `docker run -e FOO=1` | `PUT /services/my-redis` `{"overrides": {"env": {"FOO": "1"}}}` |
| `docker exec` | `POST /scripts/run` `{"task_id": "同一 task_id", "cmd": [...]}`（共享 `/workspace`） |
| `docker cp` | `PUT /scripts/{task_id}/files` / `GET /scripts/{task_id}/files` |
| `docker ps` | `GET /services`（默认只列活跃服务） |
| `docker ps -a` | `GET /services?all=true`（含 stopped/crashed 历史） |
| `docker stats` | `GET /services` 每行的 `usage`（内存/CPU/磁盘） |
| `docker stop` / `docker start` | `POST /services/{name}/stop` / `.../start`（数据保留） |
| `docker rm` / `docker rm -v` | `DELETE /services/{name}` / `...?volumes=true` |
| `docker volume ls` | `GET /volumes`（含孤儿卷标记） |
| `docker volume rm`（孤儿卷） | `DELETE /volumes/{service_id}` |
| `docker logs` | `GET /services/{name}/logs` |
| `docker network`（限制出网） | `{"network": {"mode": "none" \| "allow", "allow": [...]}}` |

## 镜像管理

镜像 = 一个普通目录（overlayfs 的只读下层）。除了 `scripts/setup_rootfs.sh` 批量构建，
也可以在 UI 或 API 上按需制作 / 删除。

```bash
# 制作（后台跑，返回 build_id）
curl -s -X POST localhost:9000/images -H 'Content-Type: application/json' \
  -d '{"name":"myimg","base":"base","packages":["jq"],"pip_packages":["httpx==0.27"]}'

curl -s localhost:9000/images/builds/<build_id>      # 看进度和日志

# 删除（只删 berth 自己管的 rootfs-* 目录；base / 使用中 / 内核挂载中的会被拒）
curl -s -X DELETE localhost:9000/images/myimg
```

> **制作镜像是宿主 root 级操作**（chroot + apt 的 maintainer script 以 root 跑），
> 所以默认关闭：要设 `BERTH_ALLOW_IMAGE_BUILD=1` 再重启 daemon。
> 构建在 mount/pid/uts/ipc namespace 里跑，装的包起的后台进程不会漏到宿主。
> 删除会拒绝：base 镜像、正被服务用的（含老记录）、正被内核 overlay 挂载引用的。

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

常用字段：`name`、`image`（镜像名：base/redis/mysql/postgres/runner/自定义，见 `scripts/setup_rootfs.sh`）、
`command`/`args`、`env`、`ports`（`{internal, external_range}`，宿主端口**动态分配**）、
`volumes`（`{internal, persist}`）、`healthcheck`（`tcp` / `http`）、`resources`（`memory_mb` / `cpu_percent`）、
`network`（出网限制）、`init`（初始化怎么执行）、`mounts`（只读挂载被测代码）。
完整字段表见 [docs/api.md](docs/api.md)。

## 令牌与作用域

berth 有两种令牌，权限不同：

| 来源 | 权限 | 用途 |
|---|---|---|
| `BERTH_TOKEN`（环境变量，可挂多个） | **admin**：全部 + 宿主级操作（造/删镜像、签发令牌） | 你自己的控制面凭证 |
| `POST /tokens` / `scripts/berth_token.py` 签发 | **非 admin**：能看能改服务/执行/产物 | 给 agent / agent 用 |

签发的 token 可以带**作用域**（按标签隔离）：

```bash
# 签一个只管 shop 项目的令牌
python3 scripts/berth_token.py create --name shakedown-shop --project shop
# → token: bt_xxxx（明文只显示这一次，库里只存 sha256）

# 之后这个 token 只能看到 / 操作 project=shop 的服务、执行、产物
curl -H "Authorization: Bearer bt_xxxx" localhost:9000/services
```

**轮换零停机**：`BERTH_TOKEN=旧的,新的` → 改完重启 → 调用方切新的 → 验证通过 → 摘掉旧的。
库里签发的 token 直接 `DELETE /tokens/{id}` 吊销，立刻失效。

### 自动获取 token（不用把长期凭证复制来复制去）

```bash
# A. 本地令牌文件（推荐，同机 agent 零配置）
#    berth 侧：BERTH_TOKEN_FILE=/run/berth/token  → 启动时自动生成（0600）
#    agent 侧：读这个文件就行
python3 scripts/berth_client.py get /services        # 自动找 token + 带 header

# B. 一次性引导码（跨机接入，不用碰 admin 凭证）
python3 scripts/berth_token.py bootstrap              # 看/生成引导码
curl -X POST http://<berth>:9000/tokens/bootstrap \
  -H 'Content-Type: application/json' \
  -d '{"code": "SJBBQ3P9", "name": "shakedown", "scope": {"labels": {"project": "shop"}}}'
# → 换到一个受限 token（不是 admin，只能管自己作用域）
```

> 为什么沙箱在 host 网络下也必须配 token：沙箱里的代码能直连 `127.0.0.1:9000`，
> 没有 token 就能删别的项目的服务和数据卷。token 防的就是这个（不是防外网）。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BERTH_TOKEN` | - | Bearer token（**可逗号分隔多个** → 零停机轮换）；**未设置 = 不鉴权**（启动日志会警告） |
| `BERTH_TOKEN_FILE` | - | 本地令牌文件：启动时不存在就**自动生成**（0600），同机 agent 直接读它 |
| `BERTH_BOOTSTRAP_TTL` | `1800` | 一次性引导码有效期（秒） |
| `BERTH_HOST` | `127.0.0.1` | 监听地址（对外监听需显式配置） |
| `BERTH_PORT` | `9000` | 监听端口 |
| `BERTH_HOME` | `/var/lib/berth` | 运行数据根目录（任务 overlay、workspace、服务卷） |
| `BERTH_DB` | `$BERTH_HOME/daemon.db` | SQLite 路径 |
| `BERTH_ROOTFS` | `/opt/berth/rootfs` | 基础镜像（`/scripts/run` 用） |
| `BERTH_TEMPLATES_DIR` | `<仓库>/templates` | 服务模板目录 |
| `BERTH_MAX_TIMEOUT` | `600` | `/scripts/run` 的 timeout 上限（秒） |
| `BERTH_MAX_MEMORY_MB` | `4096` | 内存上限 |
| `BERTH_MAX_CPU_PERCENT` | `400` | CPU 配额上限（100 = 1 核） |
| `BERTH_MAX_PROCESSES` | `512` | 进程数上限（cgroup TasksMax） |
| `BERTH_MOUNT_ROOTS` | `/srv/berth-mounts` | 只读挂载允许的宿主目录（冒号分隔） |
| `BERTH_ALLOW_IMAGE_BUILD` | `0` | 允许**制作**镜像（宿主 root 级操作，默认关） |
| `BERTH_ALLOW_IMAGE_DELETE` | `1` | 允许**删除**镜像 |
| `BERTH_UI_ENABLED` | `1` | 是否托管 `/ui` 管理界面 |
| `BERTH_TRACING` | `1` | `0` 关闭 OpenTelemetry 上报；端点不可达时自动跳过 |
| `BERTH_PHOENIX_ENDPOINT` | `http://localhost:6006/v1/traces` | OTLP 收集端点 |
| `BERTH_PHOENIX_PROJECT` | `berth` | Phoenix 项目名 |

## 运行数据布局

```
/var/lib/berth/
├── daemon.db                         # SQLite：runs / services / ports / events
├── scripts/{task_id}/
│   ├── run-{run_id}/upper|work|newroot   # 单次执行的 overlay（用完即删）
│   ├── runs/{run_id}/stdout.txt|stderr.txt  # 异步执行的输出（增量读）
│   └── workspace/                    # /workspace，同一 task_id 跨次保留
└── services/{service_id}/
    ├── run/upper|work|newroot        # 服务沙箱
    ├── workspace/                    # init.file 的默认位置（可与 task 共享）
    └── volumes/{容器内路径}/         # persist: true 的数据卷
```

仓库结构：

```
berth/
├── server.py               # FastAPI 入口（uvicorn server:app）
├── api/
│   ├── auth.py             # Bearer token 鉴权
│   └── schemas.py          # 请求模型
├── sandbox/
│   ├── paths.py            # 路径 / 提权辅助（root 或 sudo -n 两种模式）
│   ├── config.py           # 环境变量配置（上限 / 白名单 / 鉴权）
│   ├── overlayfs.py        # run_script：overlay+pivot_root+setpriv 执行
│   ├── active.py           # 在跑的执行登记表（异步 / 取消）
│   ├── runs.py             # 异步执行管理
│   ├── files.py            # 文件上传 / 下载 / 文件树
│   ├── mounts.py           # 只读挂载白名单
│   ├── images.py           # 镜像名解析（base/redis/mysql/自动发现）
│   ├── network.py          # 端口分配 + 网络策略（IP 白名单）
│   ├── service.py          # 长驻服务生命周期 + init/reset/日志 + 恢复 + 孤儿清理
│   ├── systemd_units.py    # systemd unit 查询 / 有界停止
│   └── tracing.py          # OpenTelemetry → Phoenix（可选）
├── state/db.py             # SQLite：runs / services / ports / events
├── templates/              # redis.yaml / mysql.yaml（MariaDB）/ postgres.yaml
├── ui/                     # 管理界面（静态页 + Alpine.js，随仓库提交）
├── tests/                  # 各模块验证脚本
├── scripts/                # setup_rootfs / runner_selftest / macos-bootstrap / smoke 脚本
└── docs/                   # api.md / design.md / ui.md / macos.md
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

# 不需要 systemd / root 的一组：
python3 tests/test_auth.py           # B1 鉴权（401 / 放行 / /health 免鉴权）
python3 tests/test_network_policy.py # B2 网络策略（host / none / allow / CIDR 归一化）
python3 tests/test_files.py          # B3 文件上传下载 / 路径穿越 / 挂载白名单
python3 tests/test_labels.py         # B12 标签过滤 / 状态流转 / 审计
python3 tests/test_service_spec.py   # B5/B10/B11 overrides / spec / 模板校验
python3 tests/test_api.py            # HTTP 层（鉴权 / 上传下载 / 标签 / 孤儿卷 / UI 资源）
python3 tests/test_image_mgmt.py      # 镜像管理（制作 / 删除 / 占用保护）
python3 tests/test_tokens.py          # 令牌与作用域（签发 / 隔离 / admin-only）

scripts/smoke_services.sh           # 起 redis → 外部连上 → 停止
scripts/smoke_persistence.sh        # kill daemon → 重启 → 状态恢复 + 孤儿清理
scripts/runner_selftest.sh          # runner 镜像自检（Playwright 打开本地页面并截图）
scripts/smoke_langgraph.sh          # （dev）LangGraph agent 断点恢复；需要 agent_langgraph.py
```

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `unshare: Operation not permitted` | 在受限容器里跑了。换 VM / 裸机（或特权容器） |
| `mount -t overlay ... failed` | 内核不支持 overlayfs，或 upper/work 不在同一文件系统 |
| `/scripts/run` 报找不到 python3 | 基础镜像没建好：`sudo scripts/setup_rootfs.sh` |
| 沙箱里解析不了域名 | 宿主 DNS 变了：`sudo cp /etc/resolv.conf /opt/berth/rootfs/etc/resolv.conf` |
| 健康检查超时（服务起不来） | 看日志：`GET /services/{name}/logs` 或 `journalctl -u berth-svc-xxx.service -n 50` |
| 提示没权限 | 以 root 运行，或配免密 sudo（见「运行」一节） |
| UI 里 401 | token 输错了；token 在 `sessionStorage`，退出后要重新输入 |
| 网络策略看起来没生效 | 宿主不支持 cgroup v2 + BPF：`GET /system/info` 的 `network.supported` |

## 已知限制

- 沙箱内固定降权到 uid 1000（无 user namespace 映射）；宿主上 uid 1000 的用户能写沙箱产物
- **网络隔离只到 IP 级**：`IPAddressDeny/Allow` 不分端口。host 网络下沙箱可以访问宿主上
  **任意 IP** 的**任意端口**（只要 IP 在白名单里）；要真正的端口级隔离需要 nftables + cgroup 匹配
  或每任务独立 netns（评估见 [docs/design.md](docs/design.md) 第 8 节）
- 宿主不支持 cgroup v2 + BPF 时，网络策略**不生效**（`/system/info` 会提示）
- 停止/超时依赖 cgroup 强杀（`unshare` 个别情况下信号掩码不可靠，SIGTERM 不生效）
- `templates/mysql.yaml` 基于 MariaDB 10.11（MySQL 协议/客户端兼容），首次启动自动初始化数据目录；
  root 密码在模板 `env.MYSQL_ROOT_PASSWORD`（默认 `sandbox`），改密码需清掉数据卷重新初始化
- `templates/postgres.yaml` 基于 PostgreSQL 15（LTS）；改密码同样需要清掉数据卷重新初始化
- 服务重置是"清卷 + 重来"，大数据库的重建耗时 = initdb 时间（接口返回 `reset_duration_ms`）

## License

MIT —— 见 [LICENSE](LICENSE)。
