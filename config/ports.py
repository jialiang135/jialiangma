"""
外部模型依赖的**接缝**（Protocol）
==================================

这个项目依赖两个外部模型服务：DeepSeek（对话）和 DashScope（Embedding +
Rerank）。它们的调用点散在 rag / agent / core 里，原先各自 `from config.settings
import get_xxx` 直接拿**具体工厂**，于是：

- 测试里替换假实现有 **3 种不同打法**（打消费者的绑定名、打源头
  `config.settings.xxx`、打模块名），改一处实现要跟着改一票测试；
- 换 provider 得动所有调用点；
- 一个进程里跑不了两套配置（评测想换模型做不到）。

这里把"需要什么能力"定义成协议，实现在 `config/providers.py`，由
`config/context.py` 在组合根装配。调用方只认协议，测试注入假实现也只有
**一个位置**（`get_context()` 上的 provider）。

为什么放在 config/ 而不是 core/
-------------------------------
`core/` 已经有函数内 import `rag/`（kb_tasks、eval_runner），若把上下文放进
core，`rag/` 再依赖它就成了循环。config/ 是所有层都已经依赖的最底层，
放这里不新增任何依赖边。

为什么用 Protocol 而不是抽象基类
--------------------------------
这两个接缝都不需要继承关系，只需"长得像"。结构化子类型让假实现（测试里的
几十行小类）不必去继承任何东西。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # 只为类型标注，避免在 config/ 里引入运行时依赖
    from langchain_core.embeddings import Embeddings
    from langchain_core.language_models import BaseChatModel


@runtime_checkable
class ChatModelProvider(Protocol):
    """对话模型（LLM）接缝。"""

    def chat_model(
        self,
        *,
        temperature: float = 0.3,
        streaming: bool = True,
        max_tokens: int | None = None,
    ) -> BaseChatModel:
        """
        取一个对话模型客户端。``max_tokens=None`` 表示用配置里的默认值。

        为什么要能单独指定：默认预算是**思考和答案共享**的，对"回答用户问题"够用，
        但**评测的裁判**要输出"全部断言 + 逐条判定"的长 JSON，思考一挤就生成不完
        —— ragas 会抛 ``LLMDidNotFinishException`` 并把该样本记成 NaN（实测过，
        见 ``core/eval_runner.py`` 里裁判模型的注释）。所以裁判必须能拿到一块
        独立、更大的预算。

        实现方**必须缓存**：底层客户端持有 httpx 连接池，每次新建不关是连接泄漏，
        退出时还会抛 "Event loop is closed"（详见 providers 里的说明）。
        """
        ...

    async def aclose(self) -> None:
        """关闭缓存的客户端（应用退出时调用）。"""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embedding + Rerank 接缝（本项目的实现都来自 DashScope）。"""

    def embeddings(self) -> Embeddings:
        """取 Embedding 客户端（向量库用它把文本转成向量）。"""
        ...

    def rerank(self, query: str, documents: list[str], top_n: int = 5) -> list[dict[str, Any]]:
        """
        对候选文档重排。

        Returns:
            ``[{"index": int, "score": float | None, "text": str, "degraded": bool}, ...]``

            ``score`` 为 ``None`` 表示"这次没有真正评分"（降级），调用方据此
            **跳过**相关性阈值过滤，而不是被假分数骗过。
        """
        ...
