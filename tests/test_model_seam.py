"""
模型接缝（DIP）与惰性缓存的测试
================================

这两样都是**重构做出来的东西，本身必须有测试**：

1. `get_context()` 是替换模型的**唯一**入口，坏了会让一票用例静默失效；
2. `_clear_lazy_state()` 声称"一次清干净所有惰性缓存"，漏掉一个就是串味。

不测它们的话，"接缝只有一处"就只是一句话。
"""

import inspect

from config.settings import PROJECT_ROOT


class TestLazyStateReset:
    """`_clear_lazy_state()` 必须真的把所有惰性单例丢掉。"""

    def test_drops_every_cached_singleton(self, reset_lazy_state):
        from agent.graph_workflow import get_agent_graph
        from rag.bm25_search import _bm25_manager
        from rag.search_cache import _cache
        from rag.vector_store import get_vector_store

        # 先热一遍
        graph = get_agent_graph()
        store = get_vector_store()
        assert get_agent_graph() is graph, "Agent 图应该是缓存的单例"
        assert get_vector_store() is store, "向量库应该是缓存的单例"

        # 夹具（reset_lazy_state）在 setup 时已清过一次，这里再清一次做断言
        from tests.conftest import _clear_lazy_state

        _clear_lazy_state()

        assert get_agent_graph() is not graph, "Agent 图没被清掉"
        assert get_vector_store() is not store, "向量库没被清掉"
        assert _cache._cache == {}, "检索结果缓存没被清掉"
        assert _bm25_manager._indices == {}, "BM25 索引缓存没被清掉"


class TestContextInjection:
    """注入的 provider 要能被消费方看到（消费方每次从 get_context() 现取）。"""

    def test_injected_provider_is_what_the_context_hands_out(self, app_context):
        class _FakeEmbed:
            marker = "fake"

            def embeddings(self):
                return object()

            def rerank(self, _q, _docs, top_n=5):
                return [{"index": 0, "score": None, "text": "", "degraded": True}]

        fake = _FakeEmbed()
        app_context.embed = fake

        from config.context import get_context

        assert get_context().embed is fake, "注入没生效"
        # 消费方拿到的就是这个假实现（rerank 的返回被换掉了）
        out = get_context().embed.rerank("q", ["a"], top_n=1)
        assert out[0]["score"] is None and out[0]["degraded"] is True

    def test_injected_provider_reaches_the_vector_store(self, app_context):
        """真正走一遍消费方：向量库应该用注入的 Embedding，而不是真去构造 DashScope。"""
        import rag.vector_store as vs

        called = {"n": 0}

        class _FakeEmbeddings:
            def embed_documents(self, texts):
                return [[0.0, 1.0] for _ in texts]

            def embed_query(self, _text):
                return [0.0, 1.0]

        class _FakeEmbed:
            def embeddings(self):
                called["n"] += 1
                return _FakeEmbeddings()

            def rerank(self, _q, _docs, _top_n=5):
                return []

        app_context.embed = _FakeEmbed()
        vs.reset_vector_store()

        vs.get_vector_store()

        assert called["n"] >= 1, "向量库没有向注入的 provider 取 embedding"


class TestSeamHasOneEntryPoint:
    """
    接缝只有一处：消费方从 `get_context()` 现取，不直接依赖具体实现。

    这条纯防回归 —— DIP 的全部好处都建立在"替换点唯一"上。一旦有人图省事写了
    `from config.providers import DashScopeProvider`，替换点就又多一个，而那正是
    这次重构要消灭的东西（改造前有 3 种打补丁打法）。
    """

    CONSUMERS = (
        "rag/vector_store.py",
        "rag/retriever.py",
        "agent/chat_agent.py",
        "core/eval_runner.py",
        "api/main.py",
    )
    CONCRETE = ("DeepSeekChatProvider", "DashScopeProvider")
    OLD_FACTORIES = (
        "get_deepseek_llm",
        "get_dashscope_embeddings",
        "rerank_with_dashscope",
        "close_llm_clients",
    )

    def test_consumers_go_through_get_context(self):
        problems: list[str] = []
        for rel in self.CONSUMERS:
            src = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            # 只看代码，不看注释/文档字符串里的举例
            code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
            if "get_context()" not in code:
                problems.append(f"{rel}: 没有走 get_context()")
            for name in self.CONCRETE:
                if f"import {name}" in code or f"import ({name}" in code:
                    problems.append(f"{rel}: 直接依赖了具体实现 {name}")
            for old in self.OLD_FACTORIES:
                if f"from config.settings import {old}" in code:
                    problems.append(f"{rel}: 还在用旧工厂 {old}")
        assert not problems, "接缝出现第二个入口:\n" + "\n".join(problems)

    def test_settings_no_longer_exposes_model_factories(self):
        """旧工厂不该还在，否则会有人继续用那条路。"""
        import config.settings as s

        left = [n for n in self.OLD_FACTORIES if hasattr(s, n)]
        assert not left, f"config.settings 里还留着: {left}"

    def test_ports_are_protocols_not_bases(self):
        """接缝是 Protocol：假实现不必继承任何东西（结构化子类型）。"""
        from config import ports

        for proto in (ports.ChatModelProvider, ports.EmbeddingProvider):
            assert getattr(proto, "_is_protocol", False), f"{proto} 不是 Protocol"
        assert inspect.isclass(ports.ChatModelProvider)
