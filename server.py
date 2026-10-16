"""berth：沙箱服务 HTTP 入口。

    uvicorn server:app --host 0.0.0.0 --port 9000

脚本执行：
    POST   /scripts/run        跑一段代码（沙箱里执行，返回 stdout/stderr/artifacts）
    GET    /scripts            最近执行列表（?limit=100）
    GET    /scripts/{task_id}  某次执行的历史记录
    POST   /scripts/{task_id}/clear  清空任务 workspace

长驻服务：
    POST   /services           启动服务（mysql/redis 等模板）→ 返回连接信息
    GET    /services           列出所有服务
    GET    /services/{id}      服务详情
    DELETE /services/{id}      停止并清理

其他：
    GET    /health             健康检查
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from api.schemas import RunRequest, ServiceRequest
from sandbox.overlayfs import run_script
from sandbox.paths import BASE_DIR
from sandbox.paths import sudo as sudo_rm
from sandbox.service import (
    cleanup_orphans,
    get_service,
    list_services,
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


app = FastAPI(title="berth", version="0.1.0", lifespan=lifespan)


# ─────────────────────────── 基础 ───────────────────────────


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ─────────────────────────── 脚本 ───────────────────────────


@app.post("/scripts/run")
def scripts_run(req: RunRequest) -> dict:
    """跑一次性脚本：overlayfs 沙箱执行 → 返回结果 → 自动清理。"""
    try:
        return run_script(
            req.code,
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


@app.post("/services")
def services_start(req: ServiceRequest) -> dict:
    """按模板启动一个长驻服务（systemd 托管）。"""
    try:
        return start_service(req.template, req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))


@app.post("/services/{service_id}/start")
def services_start_existing(service_id: str) -> dict:
    """把已停止的服务原样再起（复用数据卷，端口重新分配）。"""
    try:
        return start_existing_service(service_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/services")
def services_list(all: bool = False, status: str | None = None) -> list[dict]:
    """服务列表：默认只列活跃服务（?all=true 可看历史，?status= 可精确过滤）。"""
    return list_services(include_inactive=all, status=status)


@app.get("/services/{service_id}")
def services_get(service_id: str) -> dict:
    svc = get_service(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"no such service: {service_id}")
    return svc


@app.delete("/services/{service_id}")
def services_stop(service_id: str, purge: bool = False) -> dict:
    """停止并清理服务；purge=true 时连历史记录一并删除。"""
    try:
        return stop_service(service_id, purge=purge)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
