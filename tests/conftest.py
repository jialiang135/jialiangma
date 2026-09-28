"""
pytest 全局配置 —— 测试隔离
============================

**为什么必须隔离**：原实现的 ``client`` fixture 直接操作真实的
``assets/personal_agent.db`` 与 ``assets/chroma_db``。跑一次测试就污染一次
真实数据 —— 历史用例里甚至有直接 ``DELETE /api/kb/clear`` 的破坏性调用，
会把真实知识库清空。

做法：在**任何应用模块被导入之前**把路径改到临时目录。
conftest.py 的模块级代码先于测试模块执行，而 ``core.database`` 的 engine 是
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


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001
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
