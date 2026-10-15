# berth（泊位）

> 给每个任务一个泊位，给每个服务一个泊位：跑完即走（`/scripts/run`），或停泊常驻（`/services`，systemd 托管）。

把 `sandbox_overlay.sh` 手搓沙箱改造成的常驻服务：给 agent 提供「跑一段代码」和「起一个长驻服务（redis 等）」的能力。

- 沙箱底层：**overlayfs + unshare + pivot_root + setpriv**（无 Docker / 无 bwrap）
- 网络：**host 模式**（不 unshare net，可进可出，零配置）
- 长驻服务：**systemd 托管**（`systemd-run --unit=berth-xxx`，不加自研进程管理）
- 元数据：**SQLite**（执行历史 / 服务记录 / 端口分配）
- API：**FastAPI**（:9000）

## 快速开始

```bash
cd ~/stage3/berth
# 依赖已随环境提供；需要独立环境时：
# python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements.txt

uvicorn server:app --host 0.0.0.0 --port 9000
```

跑一段代码：

```bash
curl -s -X POST http://localhost:9000/scripts/run \
  -H "Content-Type: application/json" \
  -d '{"code": "print(\"hello\")"}'
# → {"stdout":"hello\n","stderr":"","exit_code":0,"duration":0.08,"artifacts":[],"task_id":"auto-..."}
```

起一个 redis：

```bash
curl -s -X POST http://localhost:9000/services \
  -H "Content-Type: application/json" \
  -d '{"template": "redis", "name": "my-redis"}'
# → {"service_id":"svc-xxx","host":"localhost","port":16379,"status":"running",...}

redis-cli -h localhost -p 16379 ping     # → PONG
curl -s -X DELETE http://localhost:9000/services/svc-xxx   # 停止并清理
```

## 目录结构

```
berth/
├── server.py               # FastAPI 入口（uvicorn server:app）
├── api/schemas.py          # 请求模型
├── sandbox/
│   ├── paths.py            # 路径 / sudo 辅助
│   ├── overlayfs.py        # run_script：overlay+pivot_root+setpriv 执行
│   ├── network.py          # 端口分配（host 模式无需 veth）
│   ├── service.py          # 长驻服务生命周期 + 恢复 + 孤儿清理
│   ├── systemd_units.py    # systemd unit 查询/有界停止
│   └── tracing.py          # OpenTelemetry → Phoenix
├── state/db.py             # SQLite：runs / services / ports
├── templates/              # redis.yaml / mysql.yaml
├── tests/                  # 各任务验证脚本
├── scripts/                # 冒烟（smoke）与验收脚本
└── docs/                   # api.md / design.md
```

运行数据（默认，可被 `BERTH_HOME` 覆盖）：

```
/var/lib/berth/
├── daemon.db                         # SQLite 元数据
├── scripts/{task_id}/
│   ├── run-{run_id}/upper|work|newroot   # 单次执行的 overlay（用完即删）
│   └── workspace/                    # /workspace，同一 task_id 跨次保留
└── services/{service_id}/
    ├── run/upper|work|newroot        # 服务沙箱
    └── volumes/{容器内路径}/         # persist: true 的数据卷
```

## 环境依赖

- Debian 12 + systemd（`systemd-run`/`systemctl`）、`unshare`（util-linux）
- 密码免密 sudo（覆盖 `systemd-run`、`systemctl`、`chown`、`rm` 等）
- rootfs：`/opt/berth/rootfs`（基础，含 python3）、`/opt/berth/rootfs-redis`（redis 模板用）
  - redis rootfs 构建：`cp -a /opt/berth/rootfs /opt/berth/rootfs-redis`
    然后 chroot 里 `apt-get install -y --no-install-recommends redis-server redis-tools`

## 验证

```bash
python3 tests/test_overlayfs.py    # 任务 1.2：hello / 超时 / 外网 / artifacts / OOM
python3 tests/test_db.py           # 任务 1.3：SQLite
python3 tests/test_network.py      # 任务 2.2：端口分配
python3 tests/test_service.py      # 任务 2.3：redis 起停 + redis-cli
scripts/smoke_persistence.sh           # 阶段三：重启恢复 + 孤儿清理
```

评测（agent 走 daemon，15 任务 × 3 次）：

```bash
cd ~/stage3 && python3 eval_daemon.py 3
```

## 已知限制

- 沙箱内进程 uid 固定 1000，无用户命名空间映射（继承宿主 uid 1000 语义）
- host 网络模式下服务直接监听宿主端口，端口分配靠 SQLite + 探测，非强制隔离
- 超时/停止依赖 cgroup 强杀：`unshare` 在个别情况下会弄坏信号掩码（SIGTERM 不可靠），
  所以 `systemd_units.stop_unit()` 采用「限时优雅 + SIGKILL 兜底」策略
- `templates/mysql.yaml` 为结构示例，未提供预装 mysql 的 rootfs，未验证
