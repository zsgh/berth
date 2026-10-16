# 在 macOS 上运行 berth

berth 依赖的是 **Linux 内核**能力，macOS 上一个都没有：

| berth 依赖 | 用途 | macOS |
|---|---|---|
| overlayfs | 只读镜像 + 任务可写层 | ❌ |
| unshare（mount/PID namespace）+ pivot_root | 沙箱隔离/换根 | ❌ |
| cgroup（systemd-run 包） | 内存/CPU 限额 | ❌ |
| systemd | 长驻服务托管、重启恢复 | ❌ |
| debootstrap | 构建沙箱镜像 | ❌ |

所以 macOS 上要先有一台 Linux —— 这和 Docker Desktop 必须内置一个 Linux VM 是同一个道理。
berth 的选择是**用现成的通用 VM（推荐 Lima）**，不自己藏 VM。

---

## 一条命令：macos-bootstrap.sh

```bash
git clone https://github.com/zsgh/berth.git
cd berth
scripts/macos-bootstrap.sh
```

脚本做这些事（全部幂等，可以反复跑）：

1. 检查 `limactl`（没有就提示 `/brew install lima`）
2. 创建（或复用）名为 **`lima`** 的 VM：4 CPU / 8 GiB 内存 / 40 GiB 磁盘
   （**只操作该名字的实例**，不会碰 `default`、`colima` 等其他 VM）
3. VM 内：装依赖 → `git clone` 代码到 `~/berth` → 建 venv → 构建沙箱镜像（base+redis+mysql）
4. 写入并启动 `berth.service`（**systemd 托管，随 VM 开机自启**）
5. 健康检查 + 打印 Mac 上的访问地址

完成后在 Mac 上直接：

```bash
curl http://localhost:9000/health
curl -X POST http://localhost:9000/services \
  -H 'Content-Type: application/json' -d '{"template": "redis"}'
redis-cli -h localhost -p 16379 ping     # 服务端口也自动转发
```

### 日常管理

| 操作 | 命令 |
|---|---|
| 看状态（VM / daemon / 服务） | `scripts/macos-bootstrap.sh --status` |
| 体检（不改动） | `scripts/macos-bootstrap.sh --check` |
| 进 VM | `scripts/macos-bootstrap.sh --shell` |
| 关/开 VM | `limactl stop lima` / `limactl start lima`（开后 berth 自动启动） |
| 删掉重建 | `scripts/macos-bootstrap.sh --recreate` |
| 彻底删除 VM | `scripts/macos-bootstrap.sh --destroy` |

可用环境变量：`BERTH_VM`（实例名）/ `BERTH_PORT` / `BERTH_SERVICE` / `BERTH_VM_DIR` / `BERTH_REPO` / `BERTH_IMAGES=all|base`。

---

## 资源配置：都是「上限」，不是预留

| 资源 | 机制 | 实际影响 |
|---|---|---|
| `cpus: 4` | 上限，空闲时 ≈ 0% | 几乎无感 |
| `memory: 8GiB` | 封顶，按需增长 | 空转 Debian + berth 通常 300–600MB；跑 mysql 再多些 |
| `disk: 40GiB` | 稀疏镜像，用多少占多少 | 实测：系统 ~1.5G + 三个镜像 ~1.4G + 数据，远不到上限 |

调整（磁盘只能调大）：

```bash
limactl stop lima
limactl edit lima          # 改 cpus/memory/disk
limactl start lima
```

## 端口转发

VM 内监听 `0.0.0.0` / `127.0.0.1` 的端口会自动转发到 **Mac 的 localhost**：
daemon `:9000`、redis `16379+`、mysql `13306+` 都能直接用。

不生效时：

```bash
limactl list                    # 确认 VM 在 Running
limactl shell lima -- ss -tlnp  # 确认进程在监听
# 兜底：ssh -N -L 9000:localhost:9000 -p <ssh端口> lima@127.0.0.1
```

## 代码和数据放在哪

全部在 **VM 本地盘**（ext4）：

```
~/berth            代码（VM 内 clone）
/opt/berth/rootfs* 沙箱镜像
/var/lib/berth     运行数据（SQLite、workspace、服务数据卷）
```

**不要把 `BERTH_HOME` / 镜像目录放到 Mac 共享目录**（virtiofs/9p 挂载）：
overlayfs 的 upper/work 建在共享目录上会直接失败；共享目录的性能也不适合小文件密集操作。
需要和 Mac 交换文件用：git / `limactl copy` / rsync。

## 更新代码

```bash
# Mac：改了已经 push 的代码
scripts/macos-bootstrap.sh          # 幂等重跑：pull + 重启服务

# 本地未提交的试验改动
rsync -av --exclude .venv --exclude .git ./ lima@<vm>:/home/lima/berth/
limactl shell lima -- sudo systemctl restart berth
```

更顺的方式：直接用 VS Code Remote-SSH 连进 VM 开发，或像现在这样在 VM 里跑 agent/编辑器。

## 已有 Linux 环境？

**这个脚本不需要参与**。berth 就是个 HTTP 服务，在哪台 Linux 上跑都一样：

- 已有远程服务器：在那台上跑 berth，Mac 直接 `curl http://<那台>:9000`
  （端口不通就用 `ssh -L 9000:localhost:9000 user@host` 隧道）
- 已有别的 Lima 实例：`scripts/macos-bootstrap.sh --instance <名字>`（显式指定才使用）
- WSL2：开启 systemd 后和普通 Debian/Ubuntu 一样，直接跑

## 故障排查

| 现象 | 处理 |
|---|---|
| `未找到 limactl` | 按提示 `brew install lima` |
| VM 起不来 | `limactl list` 看状态；`limactl start lima` 看日志 |
| Mac 上 `localhost:9000` 不通 | `--status` 确认 VM/daemon；看「端口转发」一节 |
| daemon 没起来 | `limactl shell lima -- journalctl -u berth -n 50` |
| 镜像缺失/损坏 | `--bare` 或手动重建：`limactl shell lima -- sudo ~/berth/scripts/setup_rootfs.sh --with-redis --with-mysql` |
| 磁盘快满 | `limactl shell lima -- df -h /`；`limactl edit lima` 调大磁盘 |
| 想彻底重来 | `scripts/macos-bootstrap.sh --destroy` 后重跑 |
