"""HTTP API 的请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    """POST /scripts/run 请求体。code 和 cmd 二选一（服务端校验）。"""

    code: str | None = Field(None, description="源码字符串（配 language 或 shebang）")
    cmd: list[str] | None = Field(None, description='直接执行的 argv，如 ["/bin/ls", "-la"]')
    language: str | None = Field(
        None, description="python（默认）/ bash / sh；其它解释器写 shebang（需镜像里有）"
    )
    image: str = Field("base", description="镜像名（base/redis/mysql…，见 images.yaml / 自动发现）")
    timeout: int = Field(30, ge=1, le=600, description="超时秒数")
    task_id: str | None = Field(
        None, description="任务标识；同一 task_id 共享 /workspace，缺省自动生成"
    )
    memory_mb: int = Field(256, ge=16, le=4096, description="内存上限 MB")
    cpu_percent: int = Field(50, ge=1, le=400, description="CPU 配额百分比")


class EnsureRequest(BaseModel):
    """PUT /services/{ref} 请求体（名字在路径里）。"""

    template: str = Field(..., description="模板名，如 redis / mysql")
