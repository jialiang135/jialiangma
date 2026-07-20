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

    # --- 阿里云 DashScope ---
    dashscope_api_key: str = "sk-your-dashscope-api-key-here"
    embedding_model: str = "text-embedding-v4"
    rerank_model: str = "gte-rerank"

    # --- 管理员 ---
    admin_username: str = "admin"
    admin_password: str = "admin123456"

    # --- JWT ---
    jwt_secret_key: str = "change-me-to-a-random-secret-string"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    # --- 路径 ---
    chroma_persist_dir: str = "./assets/chroma_db"
    upload_dir: str = "./assets/upload_docs"
    test_data_dir: str = "./assets/test_data"
    log_dir: str = "./logs"
    log_level: str = "INFO"

    # --- 服务 ---
    host: str = "0.0.0.0"
    port: int = 7860

    model_config = {
        "env_file": os.path.join(os.path.dirname(__file__), ".env"),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
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
# 模型客户端封装
# ========================================

def get_deepseek_llm(temperature: float = 0.3, streaming: bool = True):
    """获取 DeepSeek V4 Pro 大模型客户端"""
    from langchain_deepseek import ChatDeepSeek

    return ChatDeepSeek(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        api_base=settings.deepseek_base_url,
        temperature=temperature,
        streaming=streaming,
        max_tokens=4096,
    )


def get_dashscope_embeddings():
    """获取阿里云 DashScope Embedding 客户端"""
    from langchain_community.embeddings import DashScopeEmbeddings

    return DashScopeEmbeddings(
        model=settings.embedding_model,
        dashscope_api_key=settings.dashscope_api_key,
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
                    "text": item["document"],
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
