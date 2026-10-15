"""HTTP API 的请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    """POST /scripts/run 请求体。"""

    code: str = Field(..., description="要执行的 Python 代码")
    timeout: int = Field(30, ge=1, le=600, description="超时秒数")
    task_id: str | None = Field(
        None, description="任务标识；同一 task_id 共享 /workspace，缺省自动生成"
    )
    memory_mb: int = Field(256, ge=16, le=4096, description="内存上限 MB")
    cpu_percent: int = Field(50, ge=1, le=400, description="CPU 配额百分比")


class ServiceRequest(BaseModel):
    """POST /services 请求体。"""

    template: str = Field(..., description="模板名，如 redis")
    name: str | None = Field(None, description="人类可读的服务名")
