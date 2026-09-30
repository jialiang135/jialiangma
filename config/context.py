"""
组合根：把接缝和实现装到一起
==============================

`config/ports.py` 定义"需要什么能力"，`config/providers.py` 提供实现，
本模块负责**装配**，并且是全进程唯一的取用点。

为什么是"服务定位器"而不是构造注入
----------------------------------
LangGraph 的节点签名是固定的 ``(state) -> dict``（见 agent/graph_workflow.py），
没法把上下文当参数传进去。所以接缝只能在"模块全局"和"一个全局对象"之间选 ——
后者把替换点从 N 个收敛成 1 个：

    # 以前：测试得知道每个消费方怎么绑定的（实测有 3 种打法）
    monkeypatch.setattr(ca, "get_deepseek_llm", ...)          # 打消费者的绑定名
    monkeypatch.setattr("config.settings.get_deepseek_llm")   # 打源头
    monkeypatch.setattr(retriever, "rerank_with_dashscope")   # 还是打消费者

    # 现在：一个地方
    set_context(AppContext(chat=FakeChat(), embed=FakeEmbed()))

测试请用 tests/conftest.py 里的 ``app_context`` 夹具，它负责收尾复位。
"""

from __future__ import annotations

from config.ports import ChatModelProvider, EmbeddingProvider
from config.providers import DashScopeProvider, DeepSeekChatProvider
from config.settings import Settings
from config.settings import settings as _default_settings


class AppContext:
    """
    进程内的依赖装配点。

    三个字段都是可替换的：``settings`` 让测试指向临时目录，``chat`` / ``embed``
    让测试注入假模型。默认装配就是生产用的那套。
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        chat: ChatModelProvider | None = None,
        embed: EmbeddingProvider | None = None,
    ) -> None:
        self.settings: Settings = settings or _default_settings
        self.chat: ChatModelProvider = (
            chat if chat is not None else DeepSeekChatProvider(self.settings)
        )
        self.embed: EmbeddingProvider = (
            embed if embed is not None else DashScopeProvider(self.settings)
        )


_context: AppContext = AppContext()


def get_context() -> AppContext:
    """取当前上下文。所有需要外部模型的地方都从这里拿。"""
    return _context


def set_context(ctx: AppContext) -> None:
    """替换上下文（测试注入假实现用）。"""
    global _context
    _context = ctx


def reset_context() -> None:
    """恢复默认装配（测试收尾用）。"""
    global _context
    _context = AppContext()
