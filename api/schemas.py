"""HTTP API 的请求/响应模型。

向后兼容：所有新字段都是可选的，老客户端的请求体照样能用。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NetworkPolicy(BaseModel):
    """网络策略（B2）。

    - `host`：不限制（默认，保持现状）
    - `none`：完全断网（`IPAddressDeny=any`）
    - `allow`：白名单（`IPAddressDeny=any` + `IPAddressAllow=...`），元素是 IP / CIDR / 域名
    """

    mode: str = Field("host", description="host / none / allow")
    allow: list[str] = Field(default_factory=list, description="IP / CIDR / 域名白名单")

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        v = (v or "host").strip().lower()
        if v not in ("host", "none", "allow"):
            raise ValueError(f"network.mode 只能是 host / none / allow（收到 {v!r}）")
        return v

    def to_dict(self) -> dict:
        return {"mode": self.mode, "allow": list(self.allow)}


class MountSpec(BaseModel):
    """只读挂载（B3 / B11）。source 必须落在 BERTH_MOUNT_ROOTS 白名单里。"""

    source: str = Field(..., description="宿主路径（白名单内）")
    target: str = Field(..., description="沙箱内路径")
    read_only: bool = Field(True, description="是否只读（默认只读）")


class InitSpec(BaseModel):
    """服务初始化内容（B5）。

    - `sql`：SQL 文本（用模板里的 init 命令执行，`{sql}` 占位符）
    - `script`：脚本（模板 init 命令里的 `{script}` 占位符；模板没声明则用 sh -c）
    - `file`：服务 workspace（或白名单目录）里的文件，内容当 SQL / 脚本
    """

    sql: str | None = None
    script: str | None = None
    file: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout: int | None = Field(None, ge=1, le=3600)
    image: str | None = Field(None, description="执行初始化用的镜像（默认服务镜像）")

    def is_empty(self) -> bool:
        return not (self.sql or self.script or self.file)


class Overrides(BaseModel):
    """模板覆盖（B5 / B11）。"""

    env: dict[str, str] | None = None
    args: list[str] | None = None
    command: list[str] | None = None
    resources: dict[str, Any] | None = None
    ports: list[dict[str, Any]] | None = None
    network: dict[str, Any] | None = None
    mounts: list[dict[str, Any]] | None = None
    healthcheck: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class RunRequest(BaseModel):
    """POST /scripts/run 请求体。code 和 cmd 二选一（服务端校验）。"""

    model_config = ConfigDict(populate_by_name=True)

    code: str | None = Field(None, description="源码字符串（配 language 或 shebang）")
    cmd: list[str] | None = Field(None, description='直接执行的 argv，如 ["/bin/ls", "-la"]')
    language: str | None = Field(
        None, description="python（默认）/ bash / sh；其它解释器写 shebang（需镜像里有）"
    )
    image: str = Field("base", description="镜像名（base/redis/mysql…，见 images.yaml / 自动发现）")
    timeout: int = Field(30, ge=1, le=10**9, description="超时秒数（上限见 /system/info）")
    task_id: str | None = Field(
        None, description="任务标识；同一 task_id 共享 /workspace，缺省自动生成"
    )
    memory_mb: int = Field(256, ge=16, le=10**9, description="内存上限 MB")
    cpu_percent: int = Field(50, ge=1, le=10**7, description="CPU 配额百分比")
    processes: int | None = Field(
        None, ge=1, le=10**7, description="进程数上限（cgroup TasksMax）"
    )
    async_: bool = Field(
        False, alias="async", description="异步执行：立即返回 run id，不阻塞"
    )
    network: NetworkPolicy | str | None = Field(
        None, description="网络策略：'host' / 'none' / {mode, allow}"
    )
    mounts: list[MountSpec] | None = Field(
        None, description="只读挂载宿主目录（白名单内）"
    )
    labels: dict[str, str] | None = Field(
        None, description="标签（只是元数据：分组/检索，不参与授权判定）"
    )
    owner: str | None = Field(
        None,
        description="归属的 user（默认取令牌所属的 user；超级管理员可指定，"
        "留空 = 全局共享资源）",
    )

    @field_validator("network", mode="before")
    @classmethod
    def _network(cls, v: Any):
        if v is None:
            return None
        if isinstance(v, str):
            return {"mode": v}
        return v

    def network_policy(self) -> NetworkPolicy:
        if self.network is None:
            return NetworkPolicy()
        if isinstance(self.network, NetworkPolicy):
            return self.network
        return NetworkPolicy(**self.network)


class TokenRequest(BaseModel):
    """POST /tokens 请求体：签发访问令牌（绑定一个 user）。"""

    name: str = Field(..., description="令牌名（审计里显示）")
    user: str = Field(..., description="令牌归属的 user（租户/项目）；不存在会自动建")
    ttl: int = Field(0, ge=0, le=31536000, description="有效期（秒），0 = 永不过期")


class BootstrapRequest(BaseModel):
    """POST /tokens/bootstrap 请求体：用一次性引导码换令牌。"""

    code: str = Field(..., description="引导码（berth 启动时生成，写在本地文件里）")
    name: str = Field(..., description="令牌名（审计里显示）")
    user: str = Field(..., description="令牌归属的 user（不存在会自动建）")
    ttl: int = Field(0, ge=0, le=31536000, description="有效期（秒），0 = 永不过期")


class UserRequest(BaseModel):
    """POST /users 请求体：建一个 user（租户/项目）。"""

    name: str = Field(..., description="user 名（业务标识，如 acme / shop）")
    notes: str | None = Field(None, description="备注")
    quota_runs: int = Field(0, ge=0, description="并发任务数上限，0 = 不限")
    quota_memory_mb: int = Field(0, ge=0, description="内存上限（MB），0 = 不限")
    quota_services: int = Field(0, ge=0, description="服务数上限，0 = 不限")


class ImageBuildRequest(BaseModel):
    """POST /images 请求体：制作一个沙箱镜像（rootfs-<name>）。"""

    name: str = Field(..., description="镜像名（小写字母/数字/点/短横线，1-32 字符）")
    base: str = Field("base", description="从哪个镜像复制（默认 base）")
    packages: list[str] = Field(default_factory=list, description="apt 安装的包")
    pip_packages: list[str] = Field(default_factory=list, description="pip 安装的包")
    commands: list[str] = Field(default_factory=list, description="在 chroot 里执行的命令")
    force: bool = Field(False, description="目标已存在时重建")


class EnsureRequest(BaseModel):
    """PUT /services/{ref} 请求体（名字在路径里）。"""

    model_config = ConfigDict(populate_by_name=True)

    template: str | None = Field(None, description="模板名，如 redis / mysql / postgres")
    spec: dict[str, Any] | None = Field(
        None, description="内联模板（自定义被测服务用；与 template 二选一）"
    )
    overrides: Overrides | dict[str, Any] | None = Field(
        None, description="模板覆盖：env / args / command / resources / ports / network / mounts"
    )
    init: InitSpec | dict[str, Any] | None = Field(
        None, description="健康检查通过后执行的初始化（SQL / 脚本 / workspace 文件）"
    )
    labels: dict[str, str] | None = Field(
        None, description="标签（只是元数据：分组/检索，不参与授权判定）"
    )
    owner: str | None = Field(
        None,
        description="归属的 user（默认取令牌所属的 user；超级管理员可指定，"
        "留空 = 全局共享资源）",
    )
    task_id: str | None = Field(
        None, description="关联的任务；服务 workspace 与该任务共享（可读 init.file）"
    )

    @field_validator("overrides", mode="before")
    @classmethod
    def _overrides(cls, v: Any):
        return v

    def overrides_dict(self) -> dict:
        if self.overrides is None:
            return {}
        if isinstance(self.overrides, Overrides):
            return self.overrides.to_dict()
        return Overrides(**(self.overrides or {})).to_dict()

    def init_spec(self) -> InitSpec:
        if self.init is None:
            return InitSpec()
        if isinstance(self.init, InitSpec):
            return self.init
        return InitSpec(**(self.init or {}))
