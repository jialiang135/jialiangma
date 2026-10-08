"""
核心模块与安全防护测试
======================

这里覆盖的都是"写错了会出安全事故或静默故障"的地方：

- 路径穿越防护（P0 安全修复，必须有长期回归测试，不能只靠一次性验证脚本）
- 真实客户端 IP 提取（取错段会让限流与审计失效）
- 流式思考增量的提取（取不到会让推理模型的思考静默丢失）
- 熔断器状态机（接错会让它变成装饰品）
- 时间格式与 UTC 语义（错 8 小时会让登录锁定和日统计失真）

全部为纯单元测试，不联网、不依赖外部服务。
"""

import asyncio
import time
from datetime import UTC
from pathlib import Path

import pytest

# ============================================================
# 1. 路径穿越防护（core/paths.py）
# ============================================================


class TestSafeFilename:
    """上传文件名净化 —— 这是路径穿越的唯一防线。"""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("../../../../config/settings.py", "settings.py"),
            ("..\\..\\..\\..\\main.py", "main.py"),
            ("/etc/passwd", "passwd"),
            ("....//....//app/main.py", "main.py"),
            ("/etc/../etc/shadow", "shadow"),
            ("..\\..\\x.txt", "x.txt"),
        ],
    )
    def test_strips_directory_components(self, raw, expected):
        from core.paths import safe_filename

        assert safe_filename(raw) == expected

    @pytest.mark.parametrize("raw", ["..", "...", "", "   ", "\t\n"])
    def test_empty_or_dot_only_falls_back(self, raw):
        from core.paths import safe_filename

        assert safe_filename(raw) == "unnamed"
        assert safe_filename(raw, fallback="x.txt") == "x.txt"

    def test_normal_names_survive_intact(self):
        """正常文件（含中文、空格、连字符）不该被改名 —— 净化不能误伤。"""
        from core.paths import safe_filename

        for name in ["报告-2026年9月.docx", "resume v2.pdf", "a_b-c(1).txt"]:
            assert safe_filename(name) == name

    def test_fullwidth_slash_is_neutralized(self):
        """全角斜杠不能被当成普通字符带进文件名。"""
        from core.paths import safe_filename

        result = safe_filename("／etc／passwd")
        assert "/" not in result and "／" not in result

    def test_windows_reserved_names(self):
        """Windows 保留设备名会导致创建失败或异常行为。"""
        from core.paths import safe_filename

        assert safe_filename("CON.txt") == "CON_.txt"
        assert safe_filename("com1") == "com1_"
        assert safe_filename("NUL") == "NUL_"

    def test_control_characters_removed(self):
        from core.paths import safe_filename

        assert "\x00" not in safe_filename("file\x00name.txt")

    def test_length_is_bounded_and_keeps_extension(self):
        from core.paths import MAX_FILENAME_LENGTH, safe_filename

        result = safe_filename("a" * 300 + ".txt")
        assert len(result) <= MAX_FILENAME_LENGTH
        assert result.endswith(".txt")

    def test_result_never_contains_separators(self):
        """**核心不变量**：净化结果永远不含路径分隔符。"""
        from core.paths import safe_filename

        payloads = [
            "../../x",
            "..\\..\\x",
            "/x",
            "\\x",
            "a/b/c",
            "a\\b\\c",
            "....//....//x",
            "x/../../../y",
            "／／x",
        ]
        for p in payloads:
            cleaned = safe_filename(p)
            assert "/" not in cleaned, f"{p!r} -> {cleaned!r}"
            assert "\\" not in cleaned, f"{p!r} -> {cleaned!r}"


class TestEnsureWithin:
    """路径包含性校验 —— 用于删除操作，防"任意文件删除"。"""

    def test_inside_path_passes(self, tmp_path):
        from core.paths import ensure_within

        inside = tmp_path / "a.txt"
        inside.write_text("x", encoding="utf-8")
        assert ensure_within(tmp_path, inside) == inside.resolve()

    @pytest.mark.parametrize("escape", ["../outside.txt", "sub/../../outside.txt"])
    def test_traversal_raises(self, tmp_path, escape):
        from core.paths import ensure_within

        with pytest.raises(ValueError):
            ensure_within(tmp_path, tmp_path / escape)

    def test_absolute_outside_raises(self, tmp_path):
        from core.paths import ensure_within

        with pytest.raises(ValueError):
            ensure_within(tmp_path, Path("/etc/passwd"))


class TestRemoveWithin:
    def test_deletes_inside_file(self, tmp_path):
        from core.paths import remove_within

        target = tmp_path / "f.txt"
        target.write_text("x", encoding="utf-8")
        assert remove_within(tmp_path, target) is True
        assert not target.exists()

    def test_refuses_outside_file(self, tmp_path):
        """**关键安全行为**：越界路径必须拒绝删除，且文件应完好。"""
        from core.paths import remove_within

        outside = tmp_path.parent / "must_survive.txt"
        outside.write_text("keep", encoding="utf-8")
        try:
            assert remove_within(tmp_path, outside) is False
            assert outside.exists() and outside.read_text(encoding="utf-8") == "keep"
        finally:
            outside.unlink(missing_ok=True)

    def test_refuses_directory(self, tmp_path):
        from core.paths import remove_within

        sub = tmp_path / "sub"
        sub.mkdir()
        assert remove_within(tmp_path, sub) is False
        assert sub.exists()

    def test_missing_file_returns_false(self, tmp_path):
        from core.paths import remove_within

        assert remove_within(tmp_path, tmp_path / "nope.txt") is False

    def test_safe_join_composes_both_checks(self, tmp_path):
        from core.paths import safe_join

        joined = safe_join(tmp_path, "../../../evil.txt")
        assert joined.parent == tmp_path.resolve()
        assert joined.name == "evil.txt"


# ============================================================
# 2. 客户端 IP 提取（core/net.py）
# ============================================================


class TestClientIp:
    """取错段会让限流与审计失效（IP 段是可被客户端伪造的）。"""

    class _Req:
        def __init__(self, headers=None, client_host="10.0.0.1"):
            self.headers = headers or {}
            self.client = type("C", (), {"host": client_host})()

    def test_prefers_x_real_ip(self, monkeypatch):
        """X-Real-IP 由 Nginx 用 $remote_addr 覆写，客户端注入不了。"""
        from core import net

        monkeypatch.setattr(net.settings, "trust_proxy_headers", True)
        req = self._Req({"X-Real-IP": "1.2.3.4", "X-Forwarded-For": "9.9.9.9"})
        assert net.client_ip(req) == "1.2.3.4"

    def test_uses_last_xff_entry(self, monkeypatch):
        """XFF 是追加语义：最后一段才是我们代理加的真实对端，
        第一段是客户端自称、可任意伪造。"""
        from core import net

        monkeypatch.setattr(net.settings, "trust_proxy_headers", True)
        req = self._Req({"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 3.3.3.3"})
        assert net.client_ip(req) == "3.3.3.3"

    def test_ignores_proxy_headers_when_untrusted(self, monkeypatch):
        """直连部署时不能信任这些头，否则伪造 X-Real-IP 就能绕过限流。"""
        from core import net

        monkeypatch.setattr(net.settings, "trust_proxy_headers", False)
        req = self._Req({"X-Real-IP": "1.2.3.4", "X-Forwarded-For": "9.9.9.9"})
        assert net.client_ip(req) == "10.0.0.1"

    def test_falls_back_to_socket_peer(self, monkeypatch):
        from core import net

        monkeypatch.setattr(net.settings, "trust_proxy_headers", True)
        assert net.client_ip(self._Req()) == "10.0.0.1"


# ============================================================
# 3. 流式思考增量提取（core/llm.py）
# ============================================================


class TestExtractReasoningDelta:
    """
    取不到思考增量的后果是**静默丢失**（推理模型的思考文本不会报错，
    只是前端一直收不到），因此这里锁住两种承载方式。
    """

    def test_reads_additional_kwargs(self):
        from langchain_core.messages import AIMessageChunk

        from core.llm import extract_reasoning_delta

        chunk = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "想想"})
        assert extract_reasoning_delta(chunk) == "想想"

    def test_reads_content_blocks(self):
        from langchain_core.messages import AIMessageChunk

        from core.llm import extract_reasoning_delta

        chunk = AIMessageChunk(
            content="",
            content_blocks=[
                {"type": "reasoning", "reasoning": "推理块"},
            ],
        )
        assert extract_reasoning_delta(chunk) == "推理块"

    def test_plain_chunk_returns_empty(self):
        from langchain_core.messages import AIMessageChunk

        from core.llm import extract_reasoning_delta

        assert extract_reasoning_delta(AIMessageChunk(content="答案")) == ""

    def test_none_is_safe(self):
        from core.llm import extract_reasoning_delta

        assert extract_reasoning_delta(None) == ""


class TestExtractUsage:
    def test_extracts_reasoning_and_cache_details(self):
        from langchain_core.messages import AIMessage

        from core.llm import extract_usage

        msg = AIMessage(
            content="x",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "input_token_details": {"cache_read": 80},
                "output_token_details": {"reasoning": 30},
            },
        )
        usage = extract_usage(msg)
        assert usage["reasoning_tokens"] == 30
        assert usage["cached_tokens"] == 80
        assert usage["total_tokens"] == 150

    def test_missing_usage_returns_empty(self):
        from langchain_core.messages import AIMessage

        from core.llm import extract_usage

        assert extract_usage(AIMessage(content="x")) == {}


# ============================================================
# 4. 熔断器（core/circuit_breaker.py）
# ============================================================


class TestCircuitBreaker:
    def test_opens_after_threshold(self):
        from core.circuit_breaker import CircuitBreaker

        async def run():
            cb = CircuitBreaker(name="t", failure_threshold=3, max_retries=0)

            async def boom():
                raise RuntimeError("上游挂了")

            for _ in range(3):
                with pytest.raises(RuntimeError):
                    await cb.call(boom)
            return cb

        cb = asyncio.run(run())
        assert cb.state.value == "open"

    def test_open_circuit_fails_fast_without_calling_upstream(self):
        from core.circuit_breaker import CircuitBreaker, CircuitOpenError

        async def run():
            cb = CircuitBreaker(name="t", failure_threshold=1, max_retries=0)
            calls = {"n": 0}

            async def boom():
                calls["n"] += 1
                raise RuntimeError("x")

            with pytest.raises(RuntimeError):
                await cb.call(boom)
            before = calls["n"]
            with pytest.raises(CircuitOpenError):
                await cb.call(boom)
            return calls["n"] == before

        assert asyncio.run(run()) is True

    def test_backoff_does_not_block_event_loop(self):
        """
        原实现用 time.sleep 做退避，会**阻塞整个事件循环**。
        这里用一个独立心跳任务来证明循环仍然活着。
        """
        from core.circuit_breaker import CircuitBreaker

        async def run():
            cb = CircuitBreaker(
                name="t", failure_threshold=99, recovery_timeout=60, max_retries=2, backoff_base=2.0
            )

            async def boom():
                raise RuntimeError("x")

            ticks = {"n": 0}
            stop = False

            async def heartbeat():
                while not stop:
                    ticks["n"] += 1
                    await asyncio.sleep(0.05)

            hb = asyncio.create_task(heartbeat())
            with pytest.raises(RuntimeError):
                await cb.call(boom)
            stop = True
            await hb
            return ticks["n"]

        # 退避共 1s + 2s = 3s，心跳若被饿死则计数会接近 0
        assert asyncio.run(run()) > 20

    def test_half_open_recovery(self):
        from core.circuit_breaker import CircuitBreaker

        async def run():
            cb = CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=0.1, max_retries=0)

            async def boom():
                raise RuntimeError("x")

            with pytest.raises(RuntimeError):
                await cb.call(boom)
            assert cb.state.value == "open"

            await asyncio.sleep(0.15)
            assert cb.state.value == "half_open"

            async def ok():
                return "ok"

            assert await cb.call(ok) == "ok"
            return cb.state.value

        assert asyncio.run(run()) == "closed"

    def test_fallback_includes_original_query(self):
        """
        原实现用 getattr(kwargs, 'query') 取原始问题（kwargs 是 dict，
        永远取到空串），降级文案里从来带不上问题原文。
        """
        from core.circuit_breaker import CircuitBreaker, with_circuit_breaker

        async def run():
            cb = CircuitBreaker(name="t", failure_threshold=1, max_retries=0)

            async def boom():
                raise RuntimeError("x")

            with pytest.raises(RuntimeError):
                await cb.call(boom)

            @with_circuit_breaker(cb)
            async def protected(query: str = ""):
                return "ok"

            return await protected(query="星轨调度器是什么")

        out = asyncio.run(run())
        assert "暂时不可用" in out
        assert "星轨调度器" in out

    def test_snapshot_fields(self):
        from core.circuit_breaker import CircuitBreaker

        snap = CircuitBreaker(name="x").snapshot()
        for key in ("name", "state", "failure_count", "failure_threshold", "can_pass"):
            assert key in snap
        assert snap["state"] == "closed" and snap["can_pass"] is True


# ============================================================
# 5. 时间格式与 UTC 语义（core/database.py）
# ============================================================


class TestUTCDateTime:
    """
    库里 created_at 是 SQLite CURRENT_TIMESTAMP 写的 UTC 文本
    'YYYY-MM-DD HH:MM:SS'。格式或时区不一致会让：
    - 字符串比较与 date() 聚合出现难察觉的偏差
    - 登录锁定窗口与"今日统计"整体错 8 小时
    """

    def test_roundtrip_format(self):
        from datetime import datetime

        from core.db.base import UTCDateTime

        td = UTCDateTime()
        stored = td.process_bind_param(datetime(2026, 9, 28, 7, 36, 52), None)
        assert stored == "2026-09-28 07:36:52"
        assert len(stored) == 19 and stored[10] == " "

    def test_aware_datetime_converted_to_utc(self):
        from datetime import datetime, timedelta, timezone

        from core.db.base import UTCDateTime

        td = UTCDateTime()
        beijing = timezone(timedelta(hours=8))
        stored = td.process_bind_param(datetime(2026, 9, 28, 15, 36, 52, tzinfo=beijing), None)
        assert stored == "2026-09-28 07:36:52"

    def test_parse_back(self):
        from datetime import datetime

        from core.db.base import UTCDateTime

        parsed = UTCDateTime().process_result_value("2026-09-28 07:36:52", None)
        assert parsed == datetime(2026, 9, 28, 7, 36, 52)

    def test_none_is_transparent(self):
        from core.db.base import UTCDateTime

        td = UTCDateTime()
        assert td.process_bind_param(None, None) is None
        assert td.process_result_value(None, None) is None

    def test_utcnow_matches_sqlite_semantics(self):
        """utcnow() 必须是 UTC（naive），否则与库中既有数据差 8 小时。"""
        from datetime import datetime, timezone

        from core.db.base import utcnow

        diff = abs((utcnow() - datetime.now(UTC).replace(tzinfo=None)).total_seconds())
        assert diff < 5


# ============================================================
# 6. 令牌计量（core/token_tracker.py）
# ============================================================


class TestCostCalculation:
    def test_unknown_model_costs_zero_not_fabricated(self):
        """费率表里没有的模型记为 0 并告警 —— 编造成本数字比没有更糟。"""
        from core.token_tracker import calculate_cost

        assert calculate_cost("some-unknown-model", 1_000_000, 1_000_000) == 0.0

    def test_known_model_computes(self):
        from core.token_tracker import COST_RATES, calculate_cost

        if "deepseek-chat" in COST_RATES:
            cost = calculate_cost("deepseek-chat", 1_000_000, 0)
            assert cost == pytest.approx(COST_RATES["deepseek-chat"]["input"])


# ============================================================
# 7. 工具注册表（core/tool_registry.py）
# ============================================================


class TestToolRegistry:
    def test_agent_tools_are_registered(self):
        """注册表原先从未被调用，/api/tools 恒返回空。"""
        import agent.tools
        from core.tool_registry import tool_registry

        tools = tool_registry.list_tools()
        names = {t["name"] for t in tools}
        assert {
            "search_knowledge_base",
            "list_my_files",
            "get_kb_summary",
            "get_chat_context",
            "verify_answer_against_kb",
        } <= names

    def test_registered_tools_have_description_and_schema(self):
        import agent.tools
        from core.tool_registry import tool_registry

        for tool in tool_registry.list_tools():
            assert tool["description"], f"{tool['name']} 缺描述"
            assert tool["parameters"], f"{tool['name']} 缺参数 schema"

    def test_categories_available(self):
        import agent.tools
        from core.tool_registry import tool_registry

        assert len(tool_registry.list_categories()) >= 3


class TestAuditScope:
    """
    审计日志只记**有语义的操作**，不是"收到过哪些 HTTP 请求"。

    真实数据：线上 10 天攒了 17,470 行，其中 **9,897 行（56%）是 Prometheus 抓
    `/metrics`**，另有 1,993 行是 SPA 首页、173 行是静态 JS。真正的审计事件
    （登录、改角色、删用户、删文件）淹没在噪声里，而每条噪声都是一次 SQLite 写入。
    """

    def test_skips_monitoring_and_static(self):
        from core.audit import should_audit

        for path in ("/metrics", "/nginx-health", "/api/health", "/assets/index-x.js", "/"):
            assert not should_audit("GET", path), f"{path} 不该进审计"

    def test_records_write_operations(self):
        from core.audit import should_audit

        for method, path in (
            ("POST", "/api/kb/upload"),
            ("DELETE", "/api/kb/files/7"),
            ("PUT", "/api/kb/chunking"),
            ("POST", "/api/chat/stream"),
        ):
            assert should_audit(method, path), f"{method} {path} 是写操作，必须记"

    def test_records_auth_and_admin_reads(self):
        """登录/注册要记；管理员读全量数据也要记（触及他人数据）。"""
        from core.audit import should_audit

        assert should_audit("POST", "/api/auth/login")
        assert should_audit("POST", "/api/auth/register")
        assert should_audit("GET", "/api/admin/users")
        assert should_audit("GET", "/api/admin/audit-logs")

    def test_ignores_plain_reads(self):
        """读自己的数据不是审计事件 —— 这是把噪声压下去的关键。"""
        from core.audit import should_audit

        for path in (
            "/api/chat/conversations",
            "/api/kb/files",
            "/api/eval/reports/6",
            "/api/token/stats",
        ):
            assert not should_audit("GET", path), f"GET {path} 不该进审计"
