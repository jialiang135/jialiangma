"""
语音合成（TTS）测试
===================

分两层：

1. **纯函数**：``strip_markdown`` / ``split_sentences`` —— 这两个决定了
   "念出来的是什么"，出错的后果很直观（念星号、念代码、念到一半断句）。
2. **WebSocket 协议**：认证、Markdown 剥离时机、错误路径。
   这一层把 ``synthesize_stream`` 换成假实现，**不调 DashScope**，
   所以是快速用例（不需要 ``-m slow``）。

真实链路的验证不在这里，而在线下：
``scripts/tts_check.py``（合成本身）和 ``scripts/tts_ws_check.py``（服务端 WS）。
"""

import pytest
from fastapi.testclient import TestClient

from core.tts import split_sentences, strip_markdown

# ============================================================
# 纯函数层
# ============================================================


class TestStripMarkdown:
    """朗读前的剥离：能读的保留，纯排版符号丢弃。"""

    def test_emphasis_removed(self):
        assert strip_markdown("**加粗**和*斜体*") == "加粗和斜体"

    def test_heading_and_bullet_marks_removed(self):
        assert strip_markdown("## 标题\n- 第一项\n- 第二项") == "标题\n第一项\n第二项"

    def test_inline_code_keeps_content(self):
        # 技术名词要读出来，只丢反引号
        assert strip_markdown("用 `LangGraph` 编排") == "用 LangGraph 编排"

    def test_fenced_code_block_dropped(self):
        assert strip_markdown("说明：\n```python\nprint(1)\n```\n结束") == "说明：\n结束"

    def test_link_keeps_text_image_dropped(self):
        assert strip_markdown("见[文档](http://x.com)和![图](a.png)") == "见文档和"

    def test_table_pipe_becomes_separator(self):
        # 竖线会被念成停顿外的怪音，换成顿号更接近"列举"的读法
        assert "|" not in strip_markdown("| 姓名 | 年龄 |\n|---|---|\n| 张三 | 24 |")

    def test_empty_input(self):
        assert strip_markdown("") == ""


class TestSplitSentences:
    """切句：太碎会多花合成请求，不断句会一口气念不完。"""

    def test_splits_on_chinese_punctuation(self):
        assert split_sentences("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]

    def test_short_fragment_merged_with_next(self):
        # "对。" 单独发一次合成请求不划算，应与后句合并
        result = split_sentences("对。这个问题我想从两方面回答。")
        assert result == ["对。这个问题我想从两方面回答。"]

    def test_decimal_point_not_split(self):
        # "3.5" 不能切在点上
        result = split_sentences("版本是 3.5 版本")
        assert result == ["版本是 3.5 版本"]

    def test_empty_input(self):
        assert split_sentences("") == []


# ============================================================
# WebSocket 协议层
# ============================================================


@pytest.fixture(scope="module")
def ws_client():
    """TestClient —— 走真实 ASGI 栈，WS 路由/鉴权都是真跑的。"""
    from api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def routes(monkeypatch):
    """拿到路由模块，并把"是否可用"固定为可用（测试环境未必有真实 Key）。"""
    import api.routes.tts_routes as mod

    monkeypatch.setattr(mod, "tts_available", lambda: (True, "test"))
    return mod


def _token() -> str:
    from core.auth import create_access_token

    return create_access_token(1, "admin")


def _fake_stream(captured: list[str] | None = None, chunks=(b"ID3-fake", b"audio-2")):
    """假合成器：记录被合成的文本，产出固定音频块。"""

    async def _gen(text: str):
        if captured is not None:
            captured.append(text)
        for c in chunks:
            yield c

    return _gen


def _connect_auth(client: TestClient):
    ws = client.websocket_connect("/api/tts/stream")
    ws.__enter__()
    ws.send_json({"type": "auth", "token": _token()})
    ready = ws.receive_json()
    assert ready == {"type": "ready"}
    return ws


class TestTtsWebSocket:
    def test_happy_path(self, ws_client, routes, monkeypatch):
        """认证 → speak → started → 音频帧 → done 的完整闭环。"""
        monkeypatch.setattr(routes, "synthesize_stream", _fake_stream())
        ws = _connect_auth(ws_client)
        try:
            ws.send_json({"type": "speak", "id": 1, "text": "你好"})
            assert ws.receive_json() == {"type": "started", "id": 1}
            assert ws.receive_bytes() == b"ID3-fake"
            assert ws.receive_bytes() == b"audio-2"
            assert ws.receive_json() == {"type": "done", "id": 1}
        finally:
            ws.__exit__(None, None, None)

    def test_markdown_stripped_before_synthesis(self, ws_client, routes, monkeypatch):
        """送进合成器的必须是剥离后的文本 —— 否则会把 ** 念出来。"""
        captured: list[str] = []
        monkeypatch.setattr(routes, "synthesize_stream", _fake_stream(captured))
        ws = _connect_auth(ws_client)
        try:
            ws.send_json({"type": "speak", "id": 1, "text": "**加粗**和`代码`"})
            # 中间的音频是二进制帧，必须按固定顺序取，不能用 receive_json 循环
            assert ws.receive_json() == {"type": "started", "id": 1}
            assert ws.receive_bytes() == b"ID3-fake"
            assert ws.receive_bytes() == b"audio-2"
            assert ws.receive_json() == {"type": "done", "id": 1}
            assert captured == ["加粗和代码"]
        finally:
            ws.__exit__(None, None, None)

    def test_fully_unreadable_text_skipped_without_error(self, ws_client, routes, monkeypatch):
        """整句都是代码块 → 剥离后为空，应直接 done，而不是报错或空白等待。"""
        called = []
        monkeypatch.setattr(routes, "synthesize_stream", _fake_stream(called))
        ws = _connect_auth(ws_client)
        try:
            ws.send_json({"type": "speak", "id": 7, "text": "```\nmkdir -p /tmp\n```"})
            assert ws.receive_json() == {"type": "done", "id": 7}
            assert called == []
        finally:
            ws.__exit__(None, None, None)

    def test_bad_token_rejected(self, ws_client, routes):
        """坏 token 必须被拒，否则端点等于没鉴权。"""
        ws = ws_client.websocket_connect("/api/tts/stream")
        ws.__enter__()
        try:
            ws.send_json({"type": "auth", "token": "not-a-real-token"})
            msg = ws.receive_json()
            assert msg["type"] == "error"
        finally:
            ws.__exit__(None, None, None)

    def test_first_message_must_be_auth(self, ws_client, routes):
        """首条不是 auth 就拒绝 —— token 不放 query，认证只能靠第一条消息。"""
        ws = ws_client.websocket_connect("/api/tts/stream")
        ws.__enter__()
        try:
            ws.send_json({"type": "speak", "id": 1, "text": "你好"})
            msg = ws.receive_json()
            assert msg["type"] == "error"
        finally:
            ws.__exit__(None, None, None)

    def test_unavailable_reports_reason(self, ws_client, monkeypatch):
        """服务不可用时要给出原因，而不是静默断开。"""
        import api.routes.tts_routes as mod

        monkeypatch.setattr(mod, "tts_available", lambda: (False, "Key 未配置"))
        ws = ws_client.websocket_connect("/api/tts/stream")
        ws.__enter__()
        try:
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "Key 未配置" in msg["message"]
        finally:
            ws.__exit__(None, None, None)

    def test_ping_pong(self, ws_client, routes):
        ws = _connect_auth(ws_client)
        try:
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
        finally:
            ws.__exit__(None, None, None)
