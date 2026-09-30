"""
pytest 全局配置 —— 测试隔离
============================

**为什么必须隔离**：原实现的 ``client`` fixture 直接操作真实的
``assets/personal_agent.db`` 与 ``assets/chroma_db``。跑一次测试就污染一次
真实数据 —— 历史用例里甚至有直接 ``DELETE /api/kb/clear`` 的破坏性调用，
会把真实知识库清空。

做法：在**任何应用模块被导入之前**把路径改到临时目录。
conftest.py 的模块级代码先于测试模块执行，而 ``core.db.engine`` 的 engine 是
在模块导入时构建的，因此必须先改 settings。
"""

import os
import tempfile
from pathlib import Path

import pytest

# 允许使用仓库内置的默认密钥/口令：CI 与本地测试环境都没有 .env 的真实值，
# 而 config.settings 在导入时会做安全检查并拒绝以默认值启动。
os.environ.setdefault("ALLOW_INSECURE_DEFAULTS", "true")

# 本次测试会话专用的临时根目录
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="pytest_personal_agent_"))

from config.settings import settings  # noqa: E402

settings.db_path = str(_TMP_ROOT / "test.db")
settings.upload_dir = str(_TMP_ROOT / "upload_docs")
settings.chroma_persist_dir = str(_TMP_ROOT / "chroma_db")

for _d in (settings.upload_dir, settings.chroma_persist_dir):
    Path(_d).mkdir(parents=True, exist_ok=True)


def pytest_sessionfinish(session, exitstatus):
    """会话结束后清理临时数据。"""
    import shutil

    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


# ------------------------------------------------------------
# 同步测试与异步数据层的桥接
# ------------------------------------------------------------


def run_async(coro):
    """
    在同步测试里执行协程。

    数据层是 async 的，而 ``TestClient`` 与 pytest 用例都是同步的。
    引擎使用 NullPool（每条连接现开现关、不跨调用复用），
    因此每次 ``asyncio.run`` 新建事件循环不会留下跨循环状态。
    """
    import asyncio

    return asyncio.run(coro)


# ------------------------------------------------------------
# 隔离生效性自检
# ------------------------------------------------------------
# 这些断言存在的理由：测试套件里有一个**破坏性用例**（test_clear_noop
# 用管理员 token 调 DELETE /api/kb/clear，会真的清空文件记录 + 向量库 +
# 上传目录）。一旦上面那段路径重定向失效，它就会清掉真实的个人知识库。
#
# 这不是假想——本项目的真实知识库确实被这样清空过：
# 审计日志里 5 次 "DELETE /api/kb/clear" 全部来自 testclient（pytest），
# 最后一次是 2026-09-28。所以这里让"隔离失效"直接变成**测试失败**，
# 而不是静默地删数据。


def pytest_configure(config):
    """会话开始时校验重定向确实生效，否则立即中止。"""
    from config.settings import PROJECT_ROOT, settings

    real_assets = (PROJECT_ROOT / "assets").resolve()

    def _guard(label: str, value: str) -> None:
        resolved = settings.resolve_path(value)
        if real_assets in resolved.parents or resolved == real_assets:
            raise RuntimeError(
                f"测试隔离失效：{label} 仍指向真实目录 {resolved}。"
                f"测试套件包含会清空知识库的破坏性用例，此处必须中止运行，"
                f"否则会删掉真实数据。请检查本文件顶部的重定向是否生效。"
            )

    _guard("db_path", settings.db_path)
    _guard("chroma_persist_dir", settings.chroma_persist_dir)
    _guard("upload_dir", settings.upload_dir)


def _clear_lazy_state() -> None:
    """
    把所有**惰性缓存**一次清干净：向量库、Agent 图、检索缓存、BM25 索引。

    这些缓存各自住在拥有它的模块里（vector_store.py / graph_workflow.py /
    search_cache.py / bm25_search.py）—— 放置本身是对的，不该为了"统一"把它们
    搬到一个中心对象里（那要么让 config 反过来 import rag/agent 成环，要么引入
    一个"谁先 import 谁注册"的隐式登记机制）。

    真正的问题是**"一次清干净"这件事散在测试里**：每个用例得自己记得该调哪个
    ``reset_*``，漏一个就串味。所以收敛到这个函数，供夹具调用。
    """
    from agent.graph_workflow import reset_agent_graph
    from rag.bm25_search import invalidate_bm25_cache
    from rag.search_cache import clear_cache
    from rag.vector_store import reset_vector_store

    reset_vector_store()
    reset_agent_graph()
    clear_cache()
    invalidate_bm25_cache()


@pytest.fixture
def reset_lazy_state():
    """用例前后各清一次惰性缓存（需要"干净起点"的用例显式声明）。"""
    _clear_lazy_state()
    yield
    _clear_lazy_state()


@pytest.fixture
def app_context(reset_lazy_state):
    """
    替换外部模型接缝（对话 / Embedding / Rerank），收尾自动复位。

    这是**唯一**的替换点。改造前测试有 3 种打法（打消费者的绑定名、
    打 `config.settings.xxx` 源头、打模块名），改一处实现要跟着改一票测试；
    现在直接改属性即可 —— 消费方每次都从 `get_context()` 现取。

    用法：
        def test_x(app_context):
            app_context.chat = FakeChat(llm)
            app_context.embed = FakeEmbed(dim=64)

    它**顺带**清了惰性缓存（依赖 `reset_lazy_state`），因为向量库会把
    Embedding 客户端缓存在单例里 —— 只换 provider 不重置，用到的还是旧实例。
    这个坑原来得每个用例自己记得绕。
    """
    from config.context import get_context

    ctx = get_context()
    saved = (ctx.chat, ctx.embed)
    try:
        yield ctx
    finally:
        ctx.chat, ctx.embed = saved
