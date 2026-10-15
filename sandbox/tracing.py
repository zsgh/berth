"""OpenTelemetry trace，上报 Phoenix（任务 5.1）。

埋点：
    script.run     一次脚本执行（task_id / exit_code / duration / artifacts）
    service.start  一次服务启动（template / service_id / port / status）
    service.stop   一次服务停止（service_id / status）

环境变量：
    BERTH_TRACING=0    关闭上报（默认开启）
    BERTH_PHOENIX_ENDPOINT     收集端点，默认 http://localhost:6006/v1/traces
    BERTH_PHOENIX_PROJECT      Phoenix 项目名，默认 berth

设计：Phoenix 挂掉/包缺失时自动退化为 no-op，绝不影响主流程。
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager

log = logging.getLogger(__name__)

_tracer = None
_initialized = False


class _NullSpan:
    def set_attribute(self, *args, **kwargs):  # noqa: D102
        pass


def _reachable(endpoint: str) -> bool:
    """端点能不能连（Phoenix 没起时不要反复刷导出错误）。"""
    import socket
    from urllib.parse import urlparse

    u = urlparse(endpoint)
    host = u.hostname or "localhost"
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _init() -> None:
    global _tracer, _initialized
    if _initialized:
        return
    _initialized = True
    if os.environ.get("BERTH_TRACING", "1") == "0":
        log.info("tracing 已关闭（BERTH_TRACING=0）")
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        project = os.environ.get("BERTH_PHOENIX_PROJECT", "berth")
        endpoint = os.environ.get("BERTH_PHOENIX_ENDPOINT", "http://localhost:6006/v1/traces")
        if not _reachable(endpoint):
            log.info("tracing 端点不可达（%s），跳过上报（设 BERTH_TRACING=0 可静默）", endpoint)
            return
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": "berth",
                    # Phoenix 按这个属性把 trace 归到项目
                    "openinference.project.name": project,
                }
            )
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=5))
        )
        trace.set_tracer_provider(provider)
        _tracer = provider.get_tracer("berth")
        log.info("tracing 已启用: %s → %s", project, endpoint)
    except Exception:
        log.exception("tracing 初始化失败，退化为 no-op")


@contextmanager
def span(span_name: str, **attributes):
    """用法：

    with span("script.run", task_id=tid) as s:
        ...
        s.set_attribute("exit_code", 0)
    """
    _init()
    if _tracer is None:
        yield _NullSpan()
        return
    with _tracer.start_as_current_span(span_name) as s:
        for k, v in attributes.items():
            s.set_attribute(k, v)
        yield s
