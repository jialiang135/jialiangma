"""
全局配置模块
使用 pydantic-settings 读取 .env，封装所有模型客户端
"""
import os
from pathlib import Path
from pydantic_settings import BaseSettings
from loguru import logger


# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.resolve()


class Settings(BaseSettings):
    """全局配置，自动从 .env 文件加载"""

    # --- DeepSeek ---
    deepseek_api_key: str = "sk-your-deepseek-api-key-here"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    # 单次回答的输出 token 上限。**推理模型下这个预算由"思考"和"答案"共享**：
    # 思考消耗的 token 也算在内（实测某次问答 completion=310 中 reasoning=217）。
    # 给太小会把答案挤空（max_tokens=32 时 content 直接是空串），
    # 因此留出充足余量。端点实测接受到 32768。
    llm_max_tokens: int = 8192

    # --- 阿里云 DashScope ---
    dashscope_api_key: str = "sk-your-dashscope-api-key-here"
    embedding_model: str = "text-embedding-v4"
    rerank_model: str = "gte-rerank-v2"

    # --- 管理员 ---
    admin_username: str = "admin"
    admin_password: str = "admin123456"

    # --- JWT ---
    jwt_secret_key: str = "personal-agent-default-jwt-secret-change-in-production-env"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    # --- 路径 ---
    chroma_persist_dir: str = "./assets/chroma_db"
    upload_dir: str = "./assets/upload_docs"
    test_data_dir: str = "./assets/test_data"
    log_dir: str = "./logs"
    log_level: str = "INFO"
    # 数据库文件。原先是硬编码在 core/database.py 里的常量，
    # 导致测试无法隔离、只能操作真实库（跑一次测试就污染一次）。
    # 改为配置项后，测试可以指向临时目录。
    db_path: str = "./assets/personal_agent.db"

    # --- 服务 ---
    host: str = "0.0.0.0"
    port: int = 7860

    # --- 安全 ---
    cors_origins: str = "*"  # 生产环境改为具体域名，如 "https://your-domain.com"
    rate_limit_per_minute: int = 60  # 每 IP 每分钟最大请求数
    password_min_length: int = 8  # 密码最小长度
    max_login_attempts: int = 5  # 最大登录失败次数
    login_lockout_minutes: int = 30  # 登录锁定时间（分钟）
    trust_proxy_headers: bool = True  # 是否信任 X-Forwarded-For / X-Real-IP（部署在反向代理后时为 True）
    # 允许以默认密钥/口令启动。仅供本地开发与 CI 使用，生产必须为 False
    allow_insecure_defaults: bool = False

    # --- 上传限制 ---
    max_upload_size_mb: int = 50  # 单文件大小上限（须小于 nginx client_max_body_size）

    # --- 访问控制 ---
    # 是否允许公开注册。私有知识库场景默认关闭：注册后虽拿不到 owner_id=1 的知识库，
    # 但能调用 LLM，公网部署下等于把 API 额度开放给任意人。
    allow_registration: bool = False

    # --- Redis（会话缓存 + 向量缓存 + 异步队列）---
    redis_url: str = "redis://localhost:6379/0"

    # --- 异步任务队列 ---
    # 默认用进程内线程池：单机部署下 RQ 需要额外 worker 进程，且对 Redis
    # 版本有要求（RQ 2.x 需 Redis >= 5），收益为零而故障面很大。
    # 需要跨进程/横向扩展时再打开，并务必部署独立 worker。
    use_rq_queue: bool = False
    worker_threads: int = 4  # 线程池并发数（文档解析 + 向量化任务）

    # --- 文档处理 ---
    chunk_size: int = 1000
    chunk_overlap: int = 200
    # 是否用结构感知分块（Markdown 标题 / 中文编号章节 / 段落 → 定长兜底）。
    # 简历、项目文档这类有层级的材料，按结构切块能保住"章节语义"，
    # 检索命中率明显好于纯定长切分。原实现里这个分块器从未被启用过。
    use_semantic_splitter: bool = True
    # 检索是否启用 BM25+向量混合检索（RRF 融合）与结果缓存。
    # 原实现两者都写好了，但默认关闭且没有任何调用点传过参数 —— 等于死代码。
    use_hybrid_search: bool = True
    use_search_cache: bool = True

    model_config = {
        "env_file": os.path.join(os.path.dirname(__file__), ".env"),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",  # 忽略 .env 中未定义的字段，避免多余环境变量导致启动失败
    }

    def resolve_path(self, relative_path: str) -> Path:
        """将相对路径转为基于项目根目录的绝对路径"""
        p = Path(relative_path)
        if p.is_absolute():
            return p
        return (PROJECT_ROOT / p).resolve()


# 全局单例
settings = Settings()

# 确保存储目录存在
for dir_attr in ["upload_dir", "chroma_persist_dir", "test_data_dir", "log_dir"]:
    dir_path = settings.resolve_path(getattr(settings, dir_attr))
    dir_path.mkdir(parents=True, exist_ok=True)

# 配置 Loguru 日志
log_path = settings.resolve_path(settings.log_dir)
logger.add(
    log_path / "app_{time:YYYY-MM-DD}.log",
    level=settings.log_level,
    rotation="10 MB",
    retention="30 days",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
)


# ========================================
# 启动期安全校验
# ========================================

# 代码仓库里的默认值 —— 一旦原样用于生产，等于把密钥公开在 GitHub 上
_INSECURE_DEFAULTS = {
    "jwt_secret_key": "personal-agent-default-jwt-secret-change-in-production-env",
    "admin_password": "admin123456",
}


def validate_security_settings() -> list[str]:
    """
    检查是否仍在使用仓库内置的默认密钥/口令。

    Returns:
        仍在使用的字段名列表（空列表表示全部已改）。

    Raises:
        RuntimeError: 使用了默认值且未显式允许（``ALLOW_INSECURE_DEFAULTS=true``）。
    """
    insecure = [k for k, v in _INSECURE_DEFAULTS.items() if getattr(settings, k) == v]
    if not insecure:
        return []

    if settings.allow_insecure_defaults:
        logger.warning(
            "⚠️ 正在使用默认的 {} —— 仅允许用于本地开发/CI，请勿部署到公网",
            "、".join(insecure),
        )
        return insecure

    raise RuntimeError(
        "检测到未修改的默认安全配置: "
        + "、".join(insecure)
        + "\n这些值来自代码仓库，任何人可见，等同于没有保护。"
        "\n请在 config/.env 中改为随机值（例如 JWT_SECRET_KEY 用 "
        "`python -c \"import secrets;print(secrets.token_urlsafe(48))\"` 生成）。"
        "\n本地开发/CI 可设置 ALLOW_INSECURE_DEFAULTS=true 跳过此检查。"
    )


validate_security_settings()


# ========================================
# 模型客户端封装
# ========================================

# LLM 客户端缓存。
# 必须缓存：ChatDeepSeek 内部持有一个 httpx 异步客户端，而原实现每次调用都
# 新建一个 —— 每个对话请求都会新建客户端且从不关闭，既是连接/内存泄漏，
# 退出时还会抛出 "Event loop is closed" 的未处理异常（close 发生在循环关闭后）。
_llm_cache: dict[tuple, object] = {}


def get_deepseek_llm(temperature: float = 0.3, streaming: bool = True):
    """
    获取 DeepSeek 大模型客户端（按参数缓存，同一进程内复用）。

    注意 ``max_tokens`` 对推理模型是"思考 + 答案"的共享预算，详见
    ``llm_max_tokens`` 的说明。
    """
    key = (temperature, streaming)
    cached = _llm_cache.get(key)
    if cached is None:
        from langchain_deepseek import ChatDeepSeek

        cached = ChatDeepSeek(
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            api_base=settings.deepseek_base_url,
            temperature=temperature,
            streaming=streaming,
            max_tokens=settings.llm_max_tokens,
        )
        _llm_cache[key] = cached
    return cached


async def close_llm_clients() -> None:
    """
    关闭缓存的 LLM 客户端（应用退出时调用）。

    不显式关闭的话，httpx 客户端会在事件循环关闭后才被回收，
    从而抛出 "Task exception was never retrieved: Event loop is closed"。
    """
    for llm in list(_llm_cache.values()):
        for attr in ("root_async_client", "async_client", "root_client", "client"):
            client = getattr(llm, attr, None)
            if client is None or not hasattr(client, "close"):
                continue
            try:
                result = client.close()
                if hasattr(result, "__await__"):
                    await result
            except Exception as e:  # noqa: BLE001 - 退出清理失败不该影响关闭流程
                logger.debug("关闭 LLM 客户端 {} 失败: {}", attr, e)
    _llm_cache.clear()


def get_dashscope_embeddings():
    """
    获取阿里云 DashScope Embedding 客户端。

    实现放在 core/embeddings.py（自研，不再依赖 langchain-community）：
    该包在全项目只被用到这一个类，却要拖入整个 legacy 包及其 langchain-classic 依赖。
    """
    from core.embeddings import DashScopeEmbeddings

    return DashScopeEmbeddings(
        model=settings.embedding_model,
        api_key=settings.dashscope_api_key,
    )


def rerank_with_dashscope(query: str, documents: list[str], top_n: int = 5) -> list[dict]:
    """
    使用阿里云 DashScope rerank API 对检索结果重排
    返回: [{"index": int, "score": float, "text": str}, ...]
    """
    import dashscope

    if not documents:
        return []

    try:
        result = dashscope.TextReRank.call(
            api_key=settings.dashscope_api_key,
            model=settings.rerank_model,
            query=query,
            documents=documents,
            top_n=min(top_n, len(documents)),
            return_documents=True,
        )
        if result.status_code == 200 and result.output:
            return [
                {
                    "index": item["index"],
                    "score": item["relevance_score"],
                    "text": item["document"] if isinstance(item["document"], str) else item["document"]["text"],
                }
                for item in result.output["results"]
            ]
        else:
            logger.warning(f"Rerank API 返回异常: {result.status_code} - {result.message}")
            # 降级：返回原始排序
            return [
                {"index": i, "score": 1.0, "text": doc}
                for i, doc in enumerate(documents[:top_n])
            ]
    except Exception as e:
        logger.error(f"Rerank API 调用失败: {e}")
        # 降级
        return [
            {"index": i, "score": 1.0, "text": doc}
            for i, doc in enumerate(documents[:top_n])
        ]
