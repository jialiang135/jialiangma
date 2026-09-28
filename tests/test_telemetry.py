"""
链路追踪测试
============

重点是**降级行为**：没装 OTel、或没配导出端时，应用必须照常运行 ——
可观测性不该成为单点故障。
"""

import pytest


class TestSpan:
    def test_span_is_a_context_manager_either_way(self):
        """无论追踪是否启用，span() 都要能当上下文管理器用。"""
        from core.telemetry import span

        with span("t.noop", a=1) as sp:
            assert sp is None or hasattr(sp, "set_attribute")

    def test_attributes_of_various_types_are_accepted(self):
        """OTel 属性只接受基础类型，复杂对象要转字符串 —— 不能因此报错。"""
        from core.telemetry import span

        with span("t.attrs", s="x", n=1, f=1.5, b=True, none=None, lst=[1, 2], dct={"a": 1}):
            pass

    def test_exception_propagates_and_is_recorded(self):
        """span 要记录异常，但**不能吞掉**它 —— 原有错误处理不能被改变。"""
        from core.telemetry import span

        with pytest.raises(ValueError), span("t.err"):
            raise ValueError("boom")

    def test_nested_spans(self):
        from core.telemetry import span

        with span("outer"), span("inner"):
            pass


class TestSetup:
    def test_setup_is_idempotent_and_reports_availability(self):
        from core.telemetry import setup_telemetry

        first = setup_telemetry()
        second = setup_telemetry()
        assert first == second  # 重复调用不改变结论
        assert isinstance(first, bool)

    def test_span_never_raises_without_setup(self):
        """即使从未调用 setup_telemetry，span() 也不能报错
        （比如后台线程里先于 lifespan 用到）。"""
        from core.telemetry import span

        with span("t.before_setup"):
            pass

    def test_shutdown_is_safe_to_call(self):
        from core.telemetry import shutdown_telemetry

        shutdown_telemetry()
        shutdown_telemetry()  # 重复调用也要安全
