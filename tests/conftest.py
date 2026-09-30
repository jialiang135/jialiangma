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
