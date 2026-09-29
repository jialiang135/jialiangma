"""
Token 成本计算测试
==================

**这些用例是为了防"token 在记、钱没在算"复发。**

真实状况（2026-09-29 从库里查出来的）：

    deepseek-flash  : 34 次调用, 成本 0.0        ← 当时实际使用的模型
    deepseek-v4-pro : 29 次调用, 成本 0.04266    ← 历史记录

内置费率表里只有 `deepseek-v4-pro` / `deepseek-chat`，**没有当时配置的
`deepseek-flash`** —— 于是每一次调用都命中"未知模型"、成本记 0。
管理页显示的费用全部来自更早的历史记录，看上去"有数"，其实是空的。

处理原则是**不编造**：查不到费率就记 0，但必须让这个鸿沟**可见**
（`unmapped_models` 会出现在统计接口里），而不是让人把 0 误读成"没花钱"。
"""

import json

import pytest

from core import token_tracker as tt


class TestCalculateCost:
    def test_known_model(self):
        """内置表里的模型按费率算"""
        cost = tt.calculate_cost("deepseek-v4-pro", 1_000_000, 1_000_000)
        assert cost == pytest.approx(0.28 + 1.10, abs=1e-6)

    def test_unknown_model_is_zero_and_recorded(self):
        """
        查不到费率的模型：成本记 0，**并且被记录下来**。

        记下来是关键 —— 否则这个 0 会被当成"没花钱"，
        而不是"我们不知道花了多少"。
        """
        tt._unmapped_models.discard("no-such-model")
        cost = tt.calculate_cost("no-such-model", 1_000_000, 1_000_000)

        assert cost == 0.0
        assert "no-such-model" in tt._unmapped_models, "未知模型没有被记录下来"

    def test_cached_tokens_reduce_cost(self):
        """
        命中 prompt 缓存的输入更便宜，要从全价输入里扣掉。

        原实现记了 `cached_tokens` 但**没从成本里扣**，成本偏高。
        """
        model = "deepseek-v4-pro"
        full = tt.calculate_cost(model, 100_000, 0, cached_tokens=0)
        cached = tt.calculate_cost(model, 100_000, 0, cached_tokens=100_000)

        assert full > 0
        # 给了 cached_input 费率才算钱；没给时缓存部分按 0 计（宁可低估）
        assert cached <= full
        if "cached_input" not in tt.COST_RATES.get(model, {}):
            assert cached == 0.0

    def test_cached_tokens_clamped(self):
        """缓存 token 数超过输入数时不该算出负成本"""
        cost = tt.calculate_cost("deepseek-v4-pro", 100, 0, cached_tokens=999_999)
        assert cost >= 0


class TestCostRateConfig:
    """费率可配 —— 不同渠道定价不同，不该写死在代码里"""

    def test_override_adds_model(self, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(
            settings,
            "llm_cost_rates",
            json.dumps({"deepseek-flash": {"input": 1.0, "output": 2.0}}),
        )
        rates = tt._load_cost_rates()

        assert "deepseek-flash" in rates, "配置的模型没进费率表"
        assert rates["deepseek-flash"]["input"] == 1.0
        # 内置的不能被覆盖掉
        assert "deepseek-v4-pro" in rates

    def test_override_supports_cached_rate(self, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(
            settings,
            "llm_cost_rates",
            json.dumps({"m": {"input": 1.0, "output": 1.0, "cached_input": 0.1}}),
        )
        rates = tt._load_cost_rates()
        assert rates["m"]["cached_input"] == 0.1

    def test_bad_json_does_not_crash(self, monkeypatch):
        """配置写错了不能把应用带崩，退回内置表并记日志"""
        from config.settings import settings

        monkeypatch.setattr(settings, "llm_cost_rates", "{ 这不是 JSON")
        rates = tt._load_cost_rates()
        assert "deepseek-v4-pro" in rates

    def test_malformed_entry_skipped(self, monkeypatch):
        """条目格式不对（缺 input/output）时跳过它，别把整张表带坏"""
        from config.settings import settings

        monkeypatch.setattr(
            settings,
            "llm_cost_rates",
            json.dumps({"good": {"input": 1, "output": 2}, "bad": {"nope": 1}}),
        )
        rates = tt._load_cost_rates()
        assert "good" in rates
        assert "bad" not in rates
