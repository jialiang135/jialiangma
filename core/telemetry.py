"""
OpenTelemetry 埋点
==================

为什么是手写 span 而不是自动插桩
--------------------------------
自动插桩包（``opentelemetry-instrumentation-fastapi``）能给出"HTTP 请求耗时"，
但那和 Prometheus 已有的指标高度重叠。真正缺的是**链路内部的耗时**：

- 一次问答里，检索花了多久、LLM 花了多久、各占多少
- 一次文档入库里，解析/分块/向量化各花多久（OCR 的 PDF 可能是分钟级）
- 一次评测里每条问题花了多久

这些只有手写 span 才能看到。而且本机环境的 OTLP exporter（1.44.0）与
SDK（1.41.1）版本不一致，再引入插桩包只会让依赖更乱。

设计
----
- **可选依赖**：没装 OTel 时全部降级为 no-op，应用照常运行
- **可选导出**：设了 ``OTEL_EXPORTER_OTLP_ENDPOINT`` 才真正导出；
  否则只用 SDK 的默认（不导出）配置，便于本地调试时挂 console exporter
- 应用退出时 flush，避免最后一批 span 丢失

为什么用 SimpleSpanProcessor 而不是 BatchSpanProcessor
------------------------------------------------------
Batch 是攒批后定时导出（默认 5 秒一批），高流量下更省资源。但本项目流量很小，
而 batch 的代价是**进程被强杀时最后一批 span 全丢**（Windows 上 Popen.terminate()
就是硬杀，连 lifespan 的关闭钩子都不会执行）。小流量下逐 span 立即导出的
开销可以忽略，换来的是"看到的 trace 一定是完整的"。流量上来后再换回 batch。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

from loguru import logger

# 可选依赖：装了就启用，没装就静默降级
try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - 环境相关
    _OTEL_AVAILABLE = False

_tracer: Any = None


def setup_telemetry(service_name: str = "personal-agent") -> bool:
    """
    初始化 TracerProvider。

    Returns:
        是否真正启用了追踪（未安装 OTel 时返回 False，不影响应用启动）。
    """
    global _tracer
    if not _OTEL_AVAILABLE:
        logger.info("OpenTelemetry 未安装，链路追踪已跳过（功能不受影响）")
        return False

    if _tracer is not None:
        return True

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        # 只在真的配了收集端时才导入 exporter（它可能版本不匹配）
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            logger.info("链路追踪已启用: 导出到 {}", endpoint)
        except Exception as e:
            logger.warning("OTLP exporter 不可用，链路追踪仅本地记录: {}", e)
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif os.environ.get("OTEL_CONSOLE_EXPORT", "").lower() in ("1", "true"):
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        logger.info("链路追踪已启用: 输出到控制台")
    else:
        logger.info("链路追踪已启用（未配置导出端，span 只在本进程内记录）")

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    return True


@contextmanager
def span(name: str, **attributes: Any):
    """
    创建一个 span；未启用追踪时是 no-op（调用方无需判断）。

    用法::

        with span("retrieve", owner_id=1, top_k=10):
            ...

    异常会被记录到 span 上并继续向上抛，不影响原有错误处理。
    """
    if _tracer is None:
        yield None
        return

    with _tracer.start_as_current_span(name) as sp:
        for key, value in attributes.items():
            # OTel 属性只接受基础类型，复杂对象转成字符串
            if isinstance(value, (str, bool, int, float)) or value is None:
                sp.set_attribute(key, value)
            else:
                sp.set_attribute(key, str(value)[:200])
        try:
            yield sp
        except Exception as e:
            sp.record_exception(e)
            sp.set_status(trace.Status(trace.StatusCode.ERROR, str(e)[:200]))
            raise


def shutdown_telemetry() -> None:
    """应用退出时 flush 未发送的 span。"""
    if not _OTEL_AVAILABLE or _tracer is None:
        return
    try:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
            logger.info("链路追踪已 flush 并关闭")
    except Exception as e:
        logger.debug("关闭链路追踪失败: {}", e)
