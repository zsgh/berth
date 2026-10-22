# berth 管理界面（UI）

> 目标：**不用命令行就能完成起服务、看日志、跑脚本、看产物、清理孤儿卷**，
> 并且能被 Shakedown 的看板深链进来排障。

## 访问

```
http://127.0.0.1:9000/ui/
```

- 需要 token（和 API 用同一个 `BERTH_TOKEN`）：登录框输入一次，存 `sessionStorage`
- token **不进 URL、不进日志、不进页面源码**；401 时自动回到登录态
- 静态资源（HTML / JS / CSS）免鉴权，里面没有敏感数据
- `BERTH_UI_ENABLED=0` 可以关掉 UI（只留 API）

## 技术选型

| | 选择 | 理由 |
|---|---|---|
| 托管 | FastAPI `StaticFiles` 挂在 `/ui` | 随 berth 一起部署，不新增进程 |
| 构建 | **无**（静态 HTML + Alpine.js） | 不引入 Node 工具链；berth 所在机器可能没外网 |
| 依赖 | `ui/vendor/alpine.min.js`（随仓库提交） | 不依赖 CDN |
| 路由 | hash 路由（`#/services/xxx`） | URL 稳定，方便深链 |
| 实时 | SSE（`text/event-stream`） | 不用 WebSocket；服务端已有 journalctl / 输出文件流 |
| 鉴权 | 请求头 `Authorization: Bearer` | SSE 用 `fetch` + `ReadableStream` 手动解析（`EventSource` 不能带请求头） |

UI **只调用 berth 自己的 HTTP API**，没有后门：UI 能做的事，`curl` 都能做。

## 页面

| 路由 | 页面 | 主要内容 |
|---|---|---|
| `/ui/#/` | 总览 | 健康状态、版本、宿主 CPU/内存/磁盘、活跃服务数、近 24h 执行数与失败数、孤儿卷数、告警、运行配置（上限 / 白名单 / 网络能力） |
| `/ui/#/services` | 服务列表 | 名称、模板、状态、`host:port`、内存/上限、CPU 累计、磁盘、标签；开关"含历史"；行内操作：停止、启动、重置、删除、删除并清数据卷；新建服务表单（模板 + overrides + init） |
| `/ui/#/services/{name}` | 服务详情 | 完整 spec（模板与覆盖项）、端口、systemd unit、健康检查状态、`init` 执行结果、用量、实时日志（SSE） |
| `/ui/#/runs` | 执行列表 | task_id、状态、镜像、耗时、exit_code、网络策略、标签、时间；按状态 / 镜像 / 标签过滤 |
| `/ui/#/runs/{task_id}` | 执行详情 | stdout/stderr 实时输出、产物文件树（预览 / 下载）、取消、清空 `/workspace` |
| `/ui/#/run/new` | 手动运行 | Playground：填代码或 cmd，选镜像、资源、网络策略、超时、挂载、标签，提交后跳执行详情 |
| `/ui/#/volumes` | 数据卷 | 所属服务、大小、孤儿标记；孤儿卷可清理（非孤儿卷按钮不出现，服务端也会 409） |
| `/ui/#/images` | 镜像 | 已发现的 rootfs 列表：名称、路径、大小；构建方式用提示文字给出 |
| `/ui/#/templates` | 模板 | 展示模板内容 + "校验"按钮（复用服务端校验逻辑）；第一版只读 |
| `/ui/#/events` | 操作记录 | 时间、actor（token 短哈希）、动作、对象、状态码 |

## 刷新策略

- 列表页 / 详情页：**3 秒轮询**（`document.visibilityState !== 'visible'` 时暂停）
- 执行输出：SSE 增量推送（`GET /scripts/{task_id}/stream`）
- 服务日志：SSE 增量推送（`GET /services/{name}/logs?follow=true`）
- 页面不可见时不做任何轮询（省电、不打扰）

## 安全要求（实现方式）

| 要求 | 实现 |
|---|---|
| 产物预览隔离 | 图片 / 文本在浏览器里转义后渲染（`x-text`，不用 `innerHTML`）；HTML 报告只放在 `<iframe sandbox>`（**不加 `allow-same-origin`**），服务端同时下发 `Content-Security-Policy: sandbox; default-src 'none'` |
| 下载安全 | 下载响应带 `Content-Disposition` 和 `X-Content-Type-Options: nosniff`；前端用 `fetch` + Blob 下载（不用裸链接，避免 token 出现在 URL） |
| 日志纯文本 | stdout / stderr / journal 一律用 `<pre x-text>` 渲染；单次最多显示最后 1MB（服务端截断），超出部分用下载接口取 |
| 破坏性操作二次确认 | 删除服务、删除并清数据卷、重置、清理孤儿卷、清空 workspace 都要弹确认框，并**输入目标名称**才执行 |
| token 不外泄 | 只在 `sessionStorage`；请求头携带；401 → 清 token + 回登录态 |
| 审计 | 所有写操作由服务端中间件记进 `events` 表（actor 是 token 短哈希） |

## 深链约定（给 Shakedown 看板）

URL 规则（稳定，不会随版本变）：

```
{berth}/ui/#/                          总览
{berth}/ui/#/services                  服务列表
{berth}/ui/#/services/{service_name}   服务详情
{berth}/ui/#/runs                      执行列表
{berth}/ui/#/runs/{task_id}            执行详情
{berth}/ui/#/run/new                   手动运行
{berth}/ui/#/volumes                   数据卷
{berth}/ui/#/images                    镜像
{berth}/ui/#/templates                 模板
{berth}/ui/#/events                    操作记录
```

建议的命名约定（Shakedown 侧照此命名，UI 里一眼能定位）：

| 对象 | 命名 | 例子 |
|---|---|---|
| 任务 | `{project}-{run}-{case}` | `shop-42-login` |
| 服务 | `{project}-{role}` | `shop-db`、`shop-api` |
| 标签 | `project` / `run` / `case` | `{"project": "shop", "run": "42"}` |

这样：

- 服务列表用 `?label=project=shop` 过滤 → 只看某个项目的服务
- 执行列表用 `?label=project=shop` 过滤 → 只看某个项目的执行
- 看板直接链到 `http://berth:9000/ui/#/runs/shop-42-login` 就能看实时输出和产物

## 自己改 UI

```
ui/
├── index.html          页面结构（Alpine 指令）
├── app.js              逻辑（API 客户端 / 路由 / 各页面数据）
├── style.css           样式
└── vendor/alpine.min.js  随仓库提交的前端库（不依赖 CDN）
```

改完刷新页面即可（服务端给 UI 资源加了 `Cache-Control: no-cache`，不用清缓存）。
没有构建步骤：不要引入需要 `npm` 的依赖。
