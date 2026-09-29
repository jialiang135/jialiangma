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
- **导出方式按「端点 → 控制台 → 显式关闭」三级决定**（见 ``_resolve_export_mode``）：
  1. 配了 ``OTEL_EXPORTER_OTLP_ENDPOINT`` → 发往 OTLP 收集端；
  2. 否则**默认降级为控制台导出**（span 打到 stdout，``docker logs`` 可见）——
     这是刻意的：SDK 出厂默认是"注册 0 个 SpanProcessor"，span 生成后无处可去、
     **静默蒸发**，而运维侧完全看不出来。默认给一个 console exporter，
     才保证"没配 OTLP 时至少还留得下痕迹"；
  3. 只有**显式**设了 ``OTEL_CONSOLE_EXPORT=false/0/no/off`` 才真正不导 ——
     此时必须打 WARNING 讲清"span 会被丢弃、不留任何痕迹"，让"没配导出端"
     这件事在日志里是**可知**的，而不是一句轻描淡写的 INFO。
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

#: 实际生效的导出方式（"otlp" / "console" / "none"），供诊断与测试读取。
_export_mode: str | None = None

#: 显式关闭控制台导出时接受的字面量（大小写不敏感）。
#: 注意 **不包含空串** —— 未设置时按"默认降级到控制台"处理。
_DISABLE_VALUES = {"0", "false", "no", "off"}


def _resolve_export_mode(endpoint: str, console_flag: str) -> str:
    """
    决定 span 的导出方式。**纯函数**，便于单测。

    Args:
        endpoint:     ``OTEL_EXPORTER_OTLP_ENDPOINT`` 的原始值。
        console_flag: ``OTEL_CONSOLE_EXPORT`` 的原始值（未设置时为空串）。

    Returns:
        ``"otlp"`` / ``"console"`` / ``"none"``

    语义：
        - 配了端点 → ``otlp``；
        - 没配端点、也没显式关闭控制台 → ``console``（默认，保证 span 有落点）；
        - 没配端点、且显式设了关闭值（``false``/``0``/``no``/``off``）→ ``none``。
          ``none`` 时调用方**必须**打 WARNING，说明 span 会被丢弃。
    """
    if endpoint.strip():
        return "otlp"
    if console_flag.strip().lower() in _DISABLE_VALUES:
        return "none"
    return "console"


def setup_telemetry(service_name: str = "personal-agent") -> bool:
    """
    初始化 TracerProvider。

    Returns:
        是否真正启用了追踪（未安装 OTel 时返回 False，不影响应用启动）。
    """
    global _tracer, _export_mode
    if not _OTEL_AVAILABLE:
        logger.info("OpenTelemetry 未安装，链路追踪已跳过（功能不受影响）")
        return False

    if _tracer is not None:
        return True

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    console_flag = os.environ.get("OTEL_CONSOLE_EXPORT", "")
    mode = _resolve_export_mode(endpoint, console_flag)

    if mode == "otlp":
        # 只在真的配了收集端时才导入 exporter（它可能版本不匹配）
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(
                SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint.strip()))
            )
            logger.info("链路追踪已启用: 导出到 OTLP 端点 {}", endpoint.strip())
        except Exception as e:
            # 端点配了、但 exporter 导入/初始化失败（缺依赖、版本不符）。
            # **绝不能静默丢弃** —— 降级到控制台并明确告警，让运维看得出
            # "你以为在发 OTLP，其实一个都没发出去"。
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
            mode = "console"
            logger.warning(
                "OTLP 导出端已配置但 exporter 不可用（{}）—— 已降级为**控制台导出**："
                "span 会打到 stdout（docker logs 可见），但**不会**发往 {}。"
                "请修复 opentelemetry-exporter-otlp 依赖。",
                e,
                endpoint.strip(),
            )
    elif mode == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        # 刻意用 WARNING 而不是 INFO：默认控制台导出只是"没有后端"的兜底，
        # 运维必须一眼看出当前 trace 只进了本机 stdout、没进任何后端。
        logger.warning(
            "未配置 OTLP 导出端（OTEL_EXPORTER_OTLP_ENDPOINT）—— 链路追踪已降级为"
            "**控制台导出**：span 打到 stdout（docker logs 可见），不会进入任何 trace 后端。"
            "生产环境请配置 OTEL_EXPORTER_OTLP_ENDPOINT；"
            "若确定不需要 trace，设 OTEL_CONSOLE_EXPORT=false 关闭（届时 span 会被丢弃）。"
        )
    else:  # mode == "none"
        # 没有注册任何 SpanProcessor → span 生成后立即被丢弃。
        # 这是唯一会让 span "静默蒸发"的路径，所以必须用 WARNING 讲清楚。
        logger.warning(
            "OTEL_CONSOLE_EXPORT 已显式关闭，且未配置 OTEL_EXPORTER_OTLP_ENDPOINT —— "
            "**链路追踪产生的 span 会被直接丢弃，不会留下任何痕迹**。"
            "如需保留：设置 OTEL_EXPORTER_OTLP_ENDPOINT（发送到收集端），"
            "或 OTEL_CONSOLE_EXPORT=true（打到 stdout / docker logs）。"
        )

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    _export_mode = mode
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
