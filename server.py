"""berth：沙箱服务 HTTP 入口。

    uvicorn server:app --host 127.0.0.1 --port 9000
    # 或者：python3 server.py  （默认只监听 127.0.0.1，对外监听要显式配置）

脚本执行：
    POST   /scripts/run              跑代码（code+language）或命令（cmd）；image 选镜像
    GET    /scripts                  最近执行列表（?limit=100 ?label=project=foo）
    GET    /scripts/{task_id}        某次执行的状态 / 历史记录（?offset= 增量读输出）
    POST   /scripts/{task_id}/cancel 取消正在跑的执行
    POST   /scripts/{task_id}/clear  清空任务 workspace
    PUT    /scripts/{task_id}/files  上传文件 / tar 包到 workspace
    GET    /scripts/{task_id}/files  打包下载（?path= 子目录）
    GET    /scripts/{task_id}/files/{path}  下载单个文件
    GET    /scripts/{task_id}/tree   文件树（大小 / mime）
    GET    /scripts/{task_id}/stream 实时输出（SSE）

长驻服务（名字即身份，{ref} 可以是服务名或 svc-id）：
    PUT    /services/{ref}           ensure：不存在则建，停了原样再起，在跑直接返回
    GET    /services                 列表（?all=true 含历史；?status= 过滤；?label=）
    GET    /services/{ref}           详情（含内存/CPU/磁盘用量）
    POST   /services/{ref}/stop      停止（保留记录与数据）
    POST   /services/{ref}/start     再起（复用数据卷）
    POST   /services/{ref}/reset     重置（停 → 清卷 → 重放 init → 起）
    DELETE /services/{ref}           停止并删除记录（?volumes=true 连数据卷一起删）
    GET    /services/{ref}/logs      服务日志（?tail= &since= &follow=）

数据卷：
    GET    /volumes                  所有数据卷及占用（含孤儿卷）
    DELETE /volumes/{service_id}     只允许清理孤儿卷

其它：
    GET    /health                   健康检查（免鉴权）
    GET    /system/info              版本 / 上限 / 白名单 / 镜像 / 网络能力
    GET    /system/stats             宿主 CPU / 内存 / 磁盘
    GET    /images                   镜像列表
    GET    /templates                模板列表
    GET    /templates/{name}         模板内容
    POST   /templates/validate       校验一份模板
    GET    /events                   操作审计
    GET    /ui/                      管理界面（静态页）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from api.auth import actor_of, require_auth
from api.schemas import EnsureRequest, ImageBuildRequest, RunRequest
from sandbox import config
from sandbox.files import (
    file_tree,
    guess_mime,
    make_tar,
    read_file,
    task_workspace,
    upload_files,
    validate_task_id,
    write_file,
)
from sandbox.images import available_images
from sandbox.overlayfs import run_script
from sandbox.paths import BASE_DIR, ROOTFS, dir_size
from sandbox.paths import sudo as sudo_rm
from sandbox.service import (
    ServiceConflictError,
    cleanup_orphans,
    delete_service,
    delete_volume,
    get_service,
    list_services,
    list_templates,
    list_volumes,
    load_template,
    recover_services,
    reset_service,
    service_logs,
    service_logs_stream,
    start_existing_service,
    start_service,
    stop_service,
    validate_template,
)
from state.db import (
    get_run,
    init_db,
    list_events,
    list_runs,
    parse_label_filters,
    record_event,
)

VERSION = "0.3.0"

log = logging.getLogger("berth")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：初始化 DB → 恢复服务状态 → 清理孤儿资源。"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    init_db()
    if not config.token():
        log.warning(
            "BERTH_TOKEN 未设置：API 不鉴权（仅建议本机自用）。"
            "对外暴露前请设置 BERTH_TOKEN 并用 'Authorization: Bearer <token>' 调用。"
        )
        host = config.bind_host()
        if host not in ("127.0.0.1", "localhost", "::1"):
            log.warning(
                "对外监听 %s 且没有 token：任何人都能调用 API"
                "（包括删除其它项目的服务和数据卷）—— 强烈建议设置 BERTH_TOKEN",
                host,
            )
    else:
        log.info("API 鉴权已启用（Bearer token）")
    try:
        recover_services()
    except Exception as e:  # 没有 systemd（比如 macOS 直跑）时不阻塞 daemon 启动
        log.warning("恢复服务状态失败（宿主机没有 systemd？）: %s", e)
    try:
        cleanup_orphans()
    except Exception as e:
        log.warning("清理孤儿资源失败（宿主机没有 systemd？）: %s", e)
    yield

app = FastAPI(title="berth", version=VERSION, lifespan=lifespan)

# 所有业务接口都要鉴权（B1）；/health 和 /ui 静态资源除外
api = APIRouter(dependencies=[Depends(require_auth)])


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """写操作记审计（P1：/events）。"""
    response = await call_next(request)
    if request.method not in ("GET", "HEAD", "OPTIONS") and config.events_enabled():
        try:
            record_event(
                action=f"{request.method} {request.scope.get('path')}",
                target=request.path_params.get("task_id")
                or request.path_params.get("ref")
                or request.path_params.get("name"),
                actor=actor_of(request),
                method=request.method,
                path=request.url.path,
                status=response.status_code,
            )
        except Exception:  # 审计失败不能影响主流程
            log.exception("record_event failed")
    return response


# ─────────────────────────── 基础 ───────────────────────────


@app.get("/health")
def health() -> dict:
    """健康检查（免鉴权）。"""
    return {"status": "ok", "version": VERSION}


@api.get("/")
def index() -> dict:
    """接口索引（方便 discover）。"""
    return {
        "name": "berth",
        "version": VERSION,
        "endpoints": {
            "health": "GET /health",
            "run": "POST /scripts/run",
            "runs": "GET /scripts",
            "services": "GET /services",
            "ensure_service": "PUT /services/{name}",
            "system": "GET /system/info, GET /system/stats",
            "ui": "GET /ui/",
        },
    }


# ─────────────────────────── 脚本 ───────────────────────────


def _check_limits(timeout: int, memory_mb: int, cpu_percent: int, processes: int | None) -> None:
    """请求里的资源参数不能超过配置的上限（B4）。"""
    limits = config.limits()
    if timeout > limits["timeout_seconds"]:
        raise HTTPException(
            400, f"timeout 超过上限（{timeout}s > {limits['timeout_seconds']}s）"
        )
    if memory_mb > limits["memory_mb"]:
        raise HTTPException(
            400, f"memory_mb 超过上限（{memory_mb} > {limits['memory_mb']}）"
        )
    if cpu_percent > limits["cpu_percent"]:
        raise HTTPException(
            400, f"cpu_percent 超过上限（{cpu_percent} > {limits['cpu_percent']}）"
        )
    if processes is not None and processes > limits["processes"]:
        raise HTTPException(
            400, f"processes 超过上限（{processes} > {limits['processes']}）"
        )


@api.post("/scripts/run")
def scripts_run(req: RunRequest) -> dict:
    """跑一次性代码/命令：overlayfs 沙箱执行 → 返回结果 → 自动清理。

    code 和 cmd 二选一：
        {"code": "print(1)"}                       # 默认 python
        {"code": "echo hi", "language": "bash"}    # 或 sh / 首行 shebang
        {"cmd": ["/bin/ls", "-la"]}                # 直接执行 argv
        {"code": "...", "image": "redis"}          # 选镜像（base/redis/mysql/自定义）

    异步（不阻塞）：
        {"code": "...", "async": true}             # → {"task_id": ..., "status": "running"}
    """
    _check_limits(req.timeout, req.memory_mb, req.cpu_percent, req.processes)
    try:
        validate_task_id(req.task_id) if req.task_id else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    from sandbox import runs as runs_mod

    kwargs = dict(
        code=req.code,
        cmd=req.cmd,
        language=req.language,
        image=req.image,
        task_id=req.task_id,
        timeout=req.timeout,
        memory_mb=req.memory_mb,
        cpu_percent=req.cpu_percent,
        tasks_max=req.processes or config.max_processes(),
        network=req.network_policy().to_dict(),
        mounts=[m.model_dump() for m in (req.mounts or [])],
        labels=req.labels or {},
    )
    try:
        if req.async_:
            return runs_mod.start_async(**kwargs)
        return run_script(**kwargs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@api.get("/scripts")
def scripts_list(
    limit: int = Query(100, ge=1, le=1000),
    status: str | None = None,
    image: str | None = None,
    since: str | None = None,
    cursor: str | None = None,
    label: list[str] | None = Query(None, description="标签过滤，如 ?label=project=foo"),
) -> dict:
    """执行列表（支持状态 / 镜像 / 时间 / 标签过滤 + 游标分页）。"""
    runs = list_runs(
        limit=limit,
        status=status,
        image=image,
        since=since,
        labels=parse_label_filters(label),
        cursor=cursor,
    )
    next_cursor = runs[-1]["created_at"] if runs and len(runs) == limit else None
    return {"runs": runs, "count": len(runs), "next_cursor": next_cursor}


@api.get("/scripts/{task_id}")
def scripts_get(task_id: str, offset: int = Query(0, ge=0, description="stdout 增量偏移")) -> dict:
    """某次执行的状态 / 历史记录。

    还在跑 → status=running，并带实时输出（?offset= 增量读）。
    """
    from sandbox import runs as runs_mod

    record = runs_mod.run_status(task_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"no run for task_id={task_id}")
    if offset:
        stdout = record.get("stdout") or ""
        record["stdout"] = stdout[int(offset):]
        record["stdout_offset"] = int(offset) + len(record["stdout"])
    return record


@api.post("/scripts/{task_id}/cancel")
def scripts_cancel(task_id: str) -> dict:
    """取消正在跑的执行。"""
    from sandbox import runs as runs_mod

    result = runs_mod.cancel(task_id)
    if not result.get("cancelled"):
        raise HTTPException(status_code=404, detail=f"没有在跑的执行: {task_id}")
    return result


@api.post("/scripts/{task_id}/clear")
def scripts_clear(task_id: str) -> dict:
    """清空任务的 workspace（下次执行是全新环境）。"""
    ws = BASE_DIR / "scripts" / task_id
    if ws.exists():
        # workspace 里有沙箱 uid(1000) 的文件，用 root 删
        sudo_rm("rm", "-rf", ws, check=False)
    return {"status": "cleared", "task_id": task_id}


@api.get("/scripts/{task_id}/stream")
async def scripts_stream(task_id: str, offset: int = Query(0, ge=0)):
    """实时输出（SSE）。token 走请求头（不放 URL）。"""
    from sandbox import runs as runs_mod

    async def gen():
        pos = offset
        while True:
            live = runs_mod.live_output(task_id)
            if live is None:
                break
            if live["stdout_offset"] > pos:
                chunk = live["stdout"]
                yield f"event: stdout\ndata: {json.dumps({'text': chunk})}\n\n"
                pos = live["stdout_offset"]
            if live.get("stderr"):
                yield f"event: stderr\ndata: {json.dumps({'text': live['stderr']})}\n\n"
            yield f"event: status\ndata: {json.dumps({'status': live['status'], 'elapsed': live['elapsed']})}\n\n"
            await asyncio.sleep(0.5)
        record = get_run(task_id) or {}
        yield f"event: done\ndata: {json.dumps({'status': record.get('status', 'finished')})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────── 文件进出（B3） ───────────────────────────


@api.get("/scripts/{task_id}/tree")
def scripts_tree(task_id: str) -> dict:
    """workspace 文件树（带大小 / mime）。"""
    try:
        validate_task_id(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    entries = file_tree(task_id)
    return {"task_id": task_id, "workspace": str(task_workspace(task_id)), "files": entries}


@api.put("/scripts/{task_id}/files")
async def scripts_upload(task_id: str, request: Request):
    """上传文件到 workspace。

    - `multipart/form-data`：多个文件（filename 是 workspace 里的相对路径），
      也可以直接传一个 tar 包（自动解包）
    - 其它 content-type：按原始 body 处理（tar 包，或 `?filename=` 单文件）
    """
    try:
        validate_task_id(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    content_type = request.headers.get("content-type", "")
    try:
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            payloads: list[tuple[str, bytes]] = []
            archive: bytes | None = None
            for key, value in form.multi_items():
                # Starlette 的 UploadFile 不是 fastapi.UploadFile 的实例，用 duck typing
                if hasattr(value, "filename") and hasattr(value, "read"):
                    data = await value.read()
                    if key == "archive" or _is_tar(data):
                        archive = archive or data
                    else:
                        payloads.append((value.filename or key, data))
                elif isinstance(value, str):
                    payloads.append((key, value.encode()))
            return upload_files(task_id, files=payloads, archive=archive)

        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="空请求体")
        filename = request.query_params.get("filename")
        if filename:
            return write_file(task_id, filename, body)
        return upload_files(task_id, archive=body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _is_tar(data: bytes) -> bool:
    return len(data) > 262 and data[257:262] == b"ustar"


@api.get("/scripts/{task_id}/files")
def scripts_download(task_id: str, path: str = Query("", description="子目录（空 = 整个 workspace）")):
    """打包下载 workspace（或子目录），返回 tar。"""
    try:
        validate_task_id(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    data = make_tar(task_id, path)
    name = f"{task_id}{('/' + path.strip('/')) if path else ''}.tar"
    return Response(
        content=data,
        media_type="application/x-tar",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@api.get("/scripts/{task_id}/files/{file_path:path}")
def scripts_download_file(task_id: str, file_path: str, preview: bool = Query(False)):
    """下载单个文件（?preview=1 可以内联预览，HTML 会带严格 CSP）。"""
    try:
        validate_task_id(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        data = read_file(task_id, file_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"没有这个文件: {file_path}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    mime = guess_mime(file_path)
    headers = {"X-Content-Type-Options": "nosniff"}
    if preview:
        if mime in ("text/html", "application/xhtml+xml"):
            # HTML 报告：只允许在 sandbox iframe 里看
            headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
            headers["Content-Disposition"] = "inline"
        else:
            headers["Content-Disposition"] = "inline"
    else:
        headers["Content-Disposition"] = f'attachment; filename="{Path(file_path).name}"'
    return Response(content=data, media_type=mime, headers=headers)


@api.delete("/scripts/{task_id}/files/{file_path:path}")
def scripts_delete_file(task_id: str, file_path: str) -> dict:
    """删 workspace 里的文件。"""
    from sandbox.files import delete_path

    try:
        validate_task_id(task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        return delete_path(task_id, file_path)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ─────────────────────────── 长驻服务 ───────────────────────────


@api.put("/services/{ref}")
def services_ensure(ref: str, req: EnsureRequest) -> dict:
    """ensure 语义（幂等）：不存在则创建；已停止则原样再起（复用数据卷）；
    已在运行直接返回现有实例；名字被不同模板占用返回 409。

    可选：`overrides`（env/args/resources/ports 覆盖）、`init`（健康检查后执行）、
    `labels`（标签）、`spec`（内联模板，起被测服务用）。
    """
    if not req.template and not req.spec:
        raise HTTPException(status_code=400, detail="必须给 template（模板名）或 spec（内联模板）")
    try:
        return start_service(
            req.template or "",
            name=ref,
            spec=req.spec,
            overrides=req.overrides_dict(),
            init=req.init_spec().model_dump(exclude_none=True),
            labels=req.labels or {},
            task_id=req.task_id,
        )
    except ServiceConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.get("/services")
def services_list(
    all: bool = False,
    status: str | None = None,
    usage: bool = True,
    label: list[str] | None = Query(None, description="标签过滤，如 ?label=project=foo"),
) -> list[dict]:
    """服务列表：默认只列活跃服务（?all=true 含历史，?usage=false 跳过用量统计）。"""
    return list_services(
        include_inactive=all,
        status=status,
        with_usage=usage,
        labels=parse_label_filters(label),
    )


@api.get("/services/{ref}")
def services_get(ref: str) -> dict:
    svc = get_service(ref)
    if not svc:
        raise HTTPException(status_code=404, detail=f"no such service: {ref}")
    return svc


@api.post("/services/{ref}/stop")
def services_stop(ref: str) -> dict:
    """停止服务（保留记录与数据卷）。"""
    try:
        return stop_service(ref)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@api.post("/services/{ref}/start")
def services_start_existing(ref: str) -> dict:
    """把已停止的服务原样再起（复用数据卷，端口优先复用）。"""
    try:
        return start_existing_service(ref)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@api.post("/services/{ref}/reset")
def services_reset(ref: str, keep_backup: bool = Query(False)) -> dict:
    """重置服务：停止 → 清数据卷 → 重放 init → 启动。"""
    try:
        return reset_service(ref, keep_backup=keep_backup)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@api.delete("/services/{ref}")
def services_delete(ref: str, volumes: bool = False) -> dict:
    """停止并删除服务记录；?volumes=true 连数据卷一起删（docker rm -v 语义）。"""
    try:
        return delete_service(ref, volumes=volumes)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@api.get("/services/{ref}/logs")
def services_logs(
    ref: str,
    tail: int = Query(100, ge=1, le=10000),
    since: str | None = None,
    follow: bool = False,
):
    """服务日志（journalctl -u berth-svc-{id}）；?follow=true 是 SSE。"""
    if follow:
        async def gen():
            try:
                lines = service_logs_stream(ref, tail=tail)
                while True:
                    # journalctl 是阻塞读，放线程里跑，别卡住事件循环
                    line = await asyncio.to_thread(next, lines, None)
                    if line is None:
                        break
                    yield f"data: {json.dumps({'line': line})}\n\n"
            except ValueError as e:
                yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        return service_logs(ref, tail=tail, since=since)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ─────────────────────────── 数据卷 ───────────────────────────


@api.get("/volumes")
def volumes_list() -> list[dict]:
    """所有数据卷一览：归属、占用字节数、是否孤儿（记录已删但卷还在）。"""
    return list_volumes()


@api.delete("/volumes/{service_id}")
def volumes_delete(service_id: str) -> dict:
    """清理**孤儿**卷（记录已删但卷还在磁盘上）。非孤儿卷返回 409。"""
    try:
        return delete_volume(service_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


# ─────────────────────────── 系统信息 ───────────────────────────


@api.get("/system/info")
def system_info() -> dict:
    """版本、配置的上限、白名单挂载目录、镜像列表、网络策略能力。"""
    from sandbox.mounts import allowed_roots
    from sandbox.network import network_capabilities

    images = available_images()
    from sandbox.images import managed_images

    return {
        "version": VERSION,
        "limits": config.limits(),
        "mount_roots": [str(p) for p in allowed_roots()],
        "images": images,
        "image_mgmt": {
            "build": config.allow_image_build(),
            "delete": config.allow_image_delete(),
            "managed": sorted(managed_images()),
            "note": "制作镜像是宿主 root 级操作（chroot + apt），默认关闭",
        },
        "templates": list_templates(),
        "network": network_capabilities(),
        "auth": {"enabled": bool(config.token()), "scheme": "Bearer"},
        "ui": {"enabled": config.ui_enabled(), "path": "/ui/"},
        "home": str(BASE_DIR),
    }


@api.get("/system/stats")
def system_stats() -> dict:
    """宿主 CPU / 内存 / BERTH_HOME 所在磁盘的占用 + berth 自己的统计。"""
    stats = _host_stats()
    services = list_services(with_usage=False)
    runs = list_runs(limit=1000)
    since = time.time() - 24 * 3600
    last24 = [r for r in runs if (r.get("created_at") or "") >= _iso(since)]
    failed24 = [r for r in last24 if r.get("exit_code") not in (0, None)]
    volumes = list_volumes()
    orphans = [v for v in volumes if v.get("orphan")]
    stats.update(
        {
            "services": {
                "active": len([s for s in services if s["status"] in ("starting", "running")]),
                "total": len(services),
            },
            "runs": {"last_24h": len(last24), "failed_24h": len(failed24)},
            "volumes": {"total": len(volumes), "orphan": len(orphans)},
        }
    )
    alerts: list[str] = []
    disk = stats.get("disk") or {}
    if isinstance(disk.get("percent"), (int, float)) and disk["percent"] >= 85:
        alerts.append(f"磁盘占用 {disk['percent']:.0f}%（{disk['path']}），接近写满")
    if stats.get("volumes", {}).get("orphan"):
        alerts.append(f"有 {stats['volumes']['orphan']} 个孤儿数据卷待清理")
    stats["alerts"] = alerts
    return stats


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _host_stats() -> dict:
    """读 /proc 拿 CPU / 内存，用 shutil 拿磁盘。"""
    out: dict = {"cpu": {}, "memory": {}, "disk": {}}
    try:
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        out["cpu"] = {"percent": round(100.0 * (1 - idle / total), 1) if total else None}
    except (OSError, ValueError, IndexError):
        out["cpu"] = {"percent": None}
    try:
        out["cpu"]["cores"] = os.cpu_count()
        out["cpu"]["load"] = list(os.getloadavg())
    except OSError:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key in ("MemTotal", "MemAvailable", "MemFree", "Cached"):
                    mem[key] = int(value.strip().split()[0]) * 1024
        total_b = mem.get("MemTotal", 0)
        avail_b = mem.get("MemAvailable", 0)
        out["memory"] = {
            "total_bytes": total_b,
            "available_bytes": avail_b,
            "used_bytes": total_b - avail_b,
            "percent": round(100.0 * (total_b - avail_b) / total_b, 1) if total_b else None,
        }
    except (OSError, ValueError, IndexError):
        out["memory"] = {}
    try:
        usage = shutil.disk_usage(str(BASE_DIR))
        out["disk"] = {
            "path": str(BASE_DIR),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent": round(100.0 * usage.used / usage.total, 1) if usage.total else None,
        }
    except OSError:
        out["disk"] = {}
    return out


# ─────────────────────────── 镜像 / 模板 / 事件 ───────────────────────────


@api.get("/images")
def images_list() -> list[dict]:
    """已发现的镜像（只读视图 + 管理信息）。"""
    from sandbox.images import images_in_use, managed_images

    managed = managed_images()
    in_use = images_in_use()
    out = []
    for name, path in sorted(available_images().items()):
        out.append(
            {
                "name": name,
                "path": path,
                # 目录实际占用（du -sb）；不能用 shutil.disk_usage —— 那是整个盘的用量
                "size_bytes": dir_size(path),
                "managed": name in managed,          # 是否归 berth 管（可删）
                "deletable": name in managed and name not in in_use,
                "in_use_by": in_use.get(name, []),   # 哪些服务在用
                "is_base": path == str(Path(ROOTFS)),
            }
        )
    return out


@api.post("/images", status_code=202)
def images_build(req: ImageBuildRequest) -> dict:
    """**制作**一个沙箱镜像（后台跑，立即返回 build id）。

    这是宿主 root 级操作（chroot + apt），默认关闭：
    要开得设 `BERTH_ALLOW_IMAGE_BUILD=1` 并重启 daemon。
    """
    from sandbox.images import start_image_build

    try:
        return start_image_build(
            name=req.name,
            base=req.base,
            packages=req.packages,
            pip_packages=req.pip_packages,
            commands=req.commands,
            force=req.force,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@api.get("/images/builds")
def images_builds(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
    """镜像构建记录（最近的）。"""
    from state.db import list_image_builds

    return list_image_builds(limit=limit)


@api.get("/images/builds/{build_id}")
def images_build_get(build_id: str) -> dict:
    """某个构建的状态 + 输出日志。"""
    from state.db import get_image_build

    build = get_image_build(build_id)
    if not build:
        raise HTTPException(status_code=404, detail=f"没有这个构建: {build_id}")
    return build


@api.delete("/images/{name}")
def images_delete(name: str) -> dict:
    """**删除**一个镜像（只删 berth 自己管的 rootfs-* 目录）。

    拒绝：base 镜像、正被服务使用的镜像、构建中的镜像。
    """
    from sandbox.images import delete_image

    try:
        return delete_image(name)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@api.get("/templates")
def templates_list() -> list[str]:
    """模板名列表。"""
    return list_templates()


@api.get("/templates/{name}")
def templates_get(name: str) -> dict:
    """模板内容。"""
    try:
        return load_template(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@api.post("/templates/validate")
async def templates_validate(request: Request) -> dict:
    """校验一份模板（YAML 文本或 JSON 对象）。"""
    body = await request.body()
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            data = json.loads(body.decode("utf-8") or "{}")
        else:
            import yaml

            data = yaml.safe_load(body.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析失败: {e}")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="模板必须是映射（key: value）")
    try:
        validate_template(data.get("name") or "(inline)", data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"valid": True, "template": data}


@api.get("/events")
def events_list(
    limit: int = Query(100, ge=1, le=1000),
    action: str | None = None,
    target: str | None = None,
) -> list[dict]:
    """操作审计记录。"""
    return list_events(limit=limit, action=action, target=target)


# ─────────────────────────── 挂载 ───────────────────────────

app.include_router(api)

# UI（静态页，随 berth 一起部署；不新增进程、不引入 Node 构建链）
if config.ui_enabled() and config.ui_dir().is_dir():
    class UiStatic(StaticFiles):
        """UI 资源总是回源校验（改了 app.js 刷新就生效，不用清缓存）。"""

        def file_response(self, *args, **kwargs):
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/ui", UiStatic(directory=str(config.ui_dir()), html=True), name="ui")


def main() -> None:
    """`python3 server.py` 启动（默认只监听 127.0.0.1）。"""
    import uvicorn

    uvicorn.run(
        "server:app",
        host=config.bind_host(),
        port=config.bind_port(),
        log_level=os.environ.get("BERTH_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
