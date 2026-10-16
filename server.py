"""berth：沙箱服务 HTTP 入口。

    uvicorn server:app --host 0.0.0.0 --port 9000

脚本执行：
    POST   /scripts/run              跑代码（code+language）或命令（cmd）；image 选镜像
    GET    /scripts                  最近执行列表（?limit=100）
    GET    /scripts/{task_id}        某次执行的历史记录
    POST   /scripts/{task_id}/clear  清空任务 workspace

长驻服务（名字即身份，{ref} 可以是服务名或 svc-id）：
    PUT    /services/{ref}           ensure：不存在则建，停了原样再起，在跑直接返回
    GET    /services                 列表（?all=true 含历史；?status= 过滤）
    GET    /services/{ref}           详情（含内存/CPU/磁盘用量）
    POST   /services/{ref}/stop      停止（保留记录与数据）
    POST   /services/{ref}/start     再起（复用数据卷）
    DELETE /services/{ref}           停止并删除记录（?volumes=true 连数据卷一起删）

数据卷：
    GET    /volumes                  所有数据卷及占用（含孤儿卷）

其他：
    GET    /health                   健康检查
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from api.schemas import EnsureRequest, RunRequest
from sandbox.overlayfs import run_script
from sandbox.paths import BASE_DIR
from sandbox.paths import sudo as sudo_rm
from sandbox.service import (
    ServiceConflictError,
    cleanup_orphans,
    delete_service,
    get_service,
    list_services,
    list_volumes,
    recover_services,
    start_existing_service,
    start_service,
    stop_service,
)
from state.db import get_run, init_db, list_runs


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动：初始化 DB → 恢复服务状态 → 清理孤儿资源。"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    init_db()
    recover_services()
    cleanup_orphans()
    yield


app = FastAPI(title="berth", version="0.2.0", lifespan=lifespan)


# ─────────────────────────── 基础 ───────────────────────────


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ─────────────────────────── 脚本 ───────────────────────────


@app.post("/scripts/run")
def scripts_run(req: RunRequest) -> dict:
    """跑一次性代码/命令：overlayfs 沙箱执行 → 返回结果 → 自动清理。

    code 和 cmd 二选一：
        {"code": "print(1)"}                       # 默认 python
        {"code": "echo hi", "language": "bash"}    # 或 sh / 首行 shebang
        {"cmd": ["/bin/ls", "-la"]}                # 直接执行 argv
        {"code": "...", "image": "redis"}          # 选镜像（base/redis/mysql/自定义）
    """
    try:
        return run_script(
            req.code,
            cmd=req.cmd,
            language=req.language,
            image=req.image,
            task_id=req.task_id,
            timeout=req.timeout,
            memory_mb=req.memory_mb,
            cpu_percent=req.cpu_percent,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/scripts")
def scripts_list(limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
    """最近执行列表。"""
    return list_runs(limit)


@app.get("/scripts/{task_id}")
def scripts_get(task_id: str) -> dict:
    """某次执行的历史记录。"""
    record = get_run(task_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"no run for task_id={task_id}")
    return record


@app.post("/scripts/{task_id}/clear")
def scripts_clear(task_id: str) -> dict:
    """清空任务的 workspace（下次执行是全新环境）。"""
    ws = BASE_DIR / "scripts" / task_id
    if ws.exists():
        # workspace 里有沙箱 uid(1000) 的文件，用 root 删
        sudo_rm("rm", "-rf", ws, check=False)
    return {"status": "cleared", "task_id": task_id}


# ─────────────────────────── 长驻服务 ───────────────────────────


@app.put("/services/{ref}")
def services_ensure(ref: str, req: EnsureRequest) -> dict:
    """ensure 语义（幂等）：不存在则创建；已停止则原样再起（复用数据卷）；
    已在运行直接返回现有实例；名字被不同模板占用返回 409。
    """
    try:
        return start_service(req.template, name=ref)
    except ServiceConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))


@app.get("/services")
def services_list(
    all: bool = False, status: str | None = None, usage: bool = True
) -> list[dict]:
    """服务列表：默认只列活跃服务（?all=true 含历史，?usage=false 跳过用量统计）。"""
    return list_services(include_inactive=all, status=status, with_usage=usage)


@app.get("/services/{ref}")
def services_get(ref: str) -> dict:
    svc = get_service(ref)
    if not svc:
        raise HTTPException(status_code=404, detail=f"no such service: {ref}")
    return svc


@app.post("/services/{ref}/stop")
def services_stop(ref: str) -> dict:
    """停止服务（保留记录与数据卷）。"""
    try:
        return stop_service(ref)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/services/{ref}/start")
def services_start_existing(ref: str) -> dict:
    """把已停止的服务原样再起（复用数据卷，端口优先复用）。"""
    try:
        return start_existing_service(ref)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/services/{ref}")
def services_delete(ref: str, volumes: bool = False) -> dict:
    """停止并删除服务记录；?volumes=true 连数据卷一起删（docker rm -v 语义）。"""
    try:
        return delete_service(ref, volumes=volumes)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/volumes")
def volumes_list() -> list[dict]:
    """所有数据卷一览：归属、占用字节数、是否孤儿（记录已删但卷还在）。"""
    return list_volumes()
