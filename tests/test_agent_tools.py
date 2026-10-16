"""Behavioral tests for every user-facing LangChain tool implementation."""

from types import SimpleNamespace

from tests.conftest import run_async


def _invoke(tool, **kwargs):
    return run_async(tool.ainvoke(kwargs))


def test_search_tool_handles_empty_and_relevant_retrieval(monkeypatch):
    import agent.tools as tools

    monkeypatch.setattr(
        tools,
        "retrieve",
        lambda **_kwargs: {"documents": [], "count": 0, "context": ""},
    )
    empty = _invoke(tools.search_knowledge_base, query="missing", owner_id=4)
    assert "未找到相关内容" in empty
    assert "不要编造" in empty

    monkeypatch.setattr(
        tools,
        "retrieve",
        lambda **kwargs: {
            "documents": [{"source": "resume.pdf", "score": 0.91}],
            "count": 1,
            "query": kwargs["query"],
        },
    )
    monkeypatch.setattr(tools, "format_context_for_prompt", lambda _result: "evidence body")
    found = _invoke(tools.search_knowledge_base, query="skills", owner_id=4, top_k=2)
    assert "1 个相关片段" in found
    assert "resume.pdf" in found and "0.91" in found
    assert found.endswith("evidence body")


def test_file_list_tool_handles_empty_and_formats_file_metadata(monkeypatch):
    import agent.tools as tools

    async def no_files(_owner_id):
        return []

    monkeypatch.setattr(tools, "get_files_by_owner", no_files)
    monkeypatch.setattr(tools, "get_collection_stats", lambda _owner_id: {"total_chunks": 0})
    assert "还没有上传" in _invoke(tools.list_my_files, owner_id=3)

    async def files(_owner_id):
        return [
            {
                "filename": "resume.pdf",
                "file_size": 2048,
                "chunk_count": 3,
                "created_at": "2026-10-15T12:00:00",
            },
            {"filename": "unknown.txt"},
        ]

    monkeypatch.setattr(tools, "get_files_by_owner", files)
    monkeypatch.setattr(tools, "get_collection_stats", lambda _owner_id: {"total_chunks": 8})
    listed = _invoke(tools.list_my_files, owner_id=3)
    assert "文件数: 2" in listed
    assert "向量块总数: 8" in listed
    assert "resume.pdf" in listed and "2.0KB" in listed and "3 块" in listed
    assert "2026-10-15" in listed
    assert "unknown.txt" in listed and "0.0KB" in listed


def test_summary_tool_handles_empty_files_evidence_and_missing_evidence(monkeypatch):
    import agent.tools as tools

    async def no_files(_owner_id):
        return []

    monkeypatch.setattr(tools, "get_files_by_owner", no_files)
    monkeypatch.setattr(tools, "get_collection_stats", lambda _owner_id: {"total_chunks": 0})
    assert "无法生成摘要" in _invoke(tools.get_kb_summary, owner_id=3)

    async def files(_owner_id):
        return [{"filename": "plan.pdf"}, {"filename": "empty.txt"}]

    results = iter(
        [
            {"documents": [{"content": " first "}, {"content": "second"}, {"content": "ignored"}]},
            {"documents": []},
        ]
    )
    queries = []

    def retrieve(**kwargs):
        queries.append(kwargs)
        return next(results)

    monkeypatch.setattr(tools, "get_files_by_owner", files)
    monkeypatch.setattr(tools, "get_collection_stats", lambda _owner_id: {"total_chunks": 5})
    monkeypatch.setattr(tools, "retrieve", retrieve)
    summary = _invoke(tools.get_kb_summary, owner_id=3)
    assert "2 个文件" in summary and "5 个向量块" in summary
    assert "### plan.pdf" in summary and "first" in summary and "second" in summary
    assert "ignored" not in summary
    assert "### empty.txt" in summary and "内容较少" in summary
    assert "plan" in queries[0]["query"] and "empty" in queries[1]["query"]
    assert [query["top_k_rerank"] for query in queries] == [3, 3]


def test_chat_context_formats_newest_first_input_as_oldest_first_output(monkeypatch):
    import agent.tools as tools

    async def no_history(_owner_id, limit, offset):
        assert (limit, offset) == (10, 0)
        return []

    monkeypatch.setattr(tools, "get_chat_history", no_history)
    assert "没有历史对话记录" in _invoke(tools.get_chat_context, owner_id=3)

    async def history(_owner_id, limit, offset):
        return [
            {"question": "new question", "answer": "new answer"},
            {"question": "old question", "answer": "old answer"},
        ]

    monkeypatch.setattr(tools, "get_chat_history", history)
    context = _invoke(tools.get_chat_context, owner_id=3)
    assert context.index("old question") < context.index("new question")
    assert "## 最近 2 条" in context
    assert "old answer..." in context

    async def long_history(_owner_id, limit, offset):
        return [{"question": "q" * 250, "answer": "a" * 350}]

    monkeypatch.setattr(tools, "get_chat_history", long_history)
    truncated = _invoke(tools.get_chat_context, owner_id=3)
    assert "q" * 200 in truncated and "q" * 201 not in truncated
    assert "a" * 300 in truncated and "a" * 301 not in truncated


def test_answer_verification_distinguishes_unsupported_high_and_low_scores(monkeypatch):
    import agent.tools as tools

    monkeypatch.setattr(
        tools,
        "retrieve",
        lambda **_kwargs: {"documents": [], "count": 0},
    )
    unsupported = _invoke(tools.verify_answer_against_kb, claim="unsupported claim", owner_id=2)
    assert "未找到支撑" in unsupported
    assert "可能为幻觉" in unsupported

    monkeypatch.setattr(
        tools,
        "retrieve",
        lambda **_kwargs: {
            "documents": [{"score": 0.5, "source": "guide.md", "content": "supported text"}],
            "count": 1,
        },
    )
    supported = _invoke(tools.verify_answer_against_kb, claim="claim", owner_id=2)
    assert "高相关度匹配" in supported and "guide.md" in supported
    assert "可以使用" in supported

    monkeypatch.setattr(
        tools,
        "retrieve",
        lambda **_kwargs: {
            "documents": [{"score": 0.49, "source": "guide.md", "content": "partial text"}],
            "count": 1,
        },
    )
    low_score = _invoke(tools.verify_answer_against_kb, claim="claim", owner_id=2)
    assert "匹配度较低" in low_score
    assert "谨慎使用" in low_score


def test_registry_queries_filter_metadata_and_return_missing_values():
    from core.tool_registry import ToolRegistry

    registry = ToolRegistry()

    def first():
        """First tool documentation."""

    def second():
        return None

    registry.register("first", category="documents", parameters={"type": "object"})(first)
    registry.register("second", category="admin")(second)

    assert registry.get_tool("first") is first
    assert registry.get_tool("missing") is None
    assert registry.get_tool_info("missing") is None
    assert registry.get_tool_info("first")["description"] == "First tool documentation."
    assert registry.list_tools(category="documents") == [
        {
            "name": "first",
            "description": "First tool documentation.",
            "category": "documents",
            "parameters": {"type": "object"},
        }
    ]
    assert {tool["name"] for tool in registry.list_tools()} == {"first", "second"}
    assert registry.list_categories() == ["admin", "documents"]
    assert len(registry) == 2


def test_tools_register_callable_implementations_and_tolerate_bad_schemas(monkeypatch):
    import agent.tools as tools
    import core.tool_registry as registry_module
    from core.tool_registry import ToolRegistry

    registry = ToolRegistry()
    monkeypatch.setattr(registry_module, "tool_registry", registry)

    class BrokenSchema:
        def model_json_schema(self):
            raise RuntimeError("schema unavailable")

    def good_impl():
        return None

    fake_tools = [
        SimpleNamespace(
            name="schema-error",
            description="first",
            args_schema=BrokenSchema(),
            coroutine=good_impl,
            func=None,
        ),
        SimpleNamespace(
            name="no-implementation",
            description="missing",
            args_schema=None,
            coroutine=None,
            func=None,
        ),
        SimpleNamespace(
            name="sync-fallback",
            description="sync",
            args_schema=None,
            coroutine=None,
            func=good_impl,
        ),
        SimpleNamespace(
            name="schema-success",
            description="schema",
            args_schema=SimpleNamespace(model_json_schema=lambda: {"type": "object"}),
            coroutine=good_impl,
            func=None,
        ),
        SimpleNamespace(
            name="doc-fallback",
            description="",
            args_schema=None,
            coroutine=good_impl,
            func=None,
        ),
    ]
    for name, fake in zip(
        ("search_knowledge_base", "get_kb_summary", "list_my_files", "get_chat_context", "verify_answer_against_kb"),
        fake_tools,
        strict=True,
    ):
        monkeypatch.setattr(tools, name, fake)

    tools._register_all_tools()

    assert registry.get_tool("schema-error") is good_impl
    assert registry.get_tool("no-implementation") is None
    assert registry.get_tool("sync-fallback") is good_impl
    assert registry.get_tool_info("schema-success")["parameters"] == {"type": "object"}
    assert registry.get_tool_info("doc-fallback")["description"] == ""
    assert len(registry) == 4
