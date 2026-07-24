"""
FastAPI 主入口 + 全局中间件
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
import time
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from api.routes.auth_routes import router as auth_router
from api.routes.chat_routes import router as chat_router
from api.routes.kb_routes import router as kb_router
from api.routes.token_routes import router as token_router

from api.routes.tool_routes import router as tool_router
from api.routes.admin_routes import router as admin_router
from core.database import init_database, create_admin_user
from core.auth import hash_password
from config.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理（替代废弃的 on_event）"""
    logger.info("=" * 60)
    logger.info("个人数字分身 · 多Agent私有RAG系统 启动中...")
    logger.info("=" * 60)
    init_database()
    create_admin_user(settings.admin_username, hash_password(settings.admin_password))
    logger.info(f"DeepSeek 模型: {settings.deepseek_model}")
    logger.info(f"Embedding 模型: {settings.embedding_model}")
    logger.info(f"Rerank 模型: {settings.rerank_model}")
    logger.info(f"ChromaDB 路径: {settings.chroma_persist_dir}")
    logger.info(f"上传文件路径: {settings.upload_dir}")

    # 初始化会话管理器
    from core.session_manager import session_manager
    logger.info(f"会话管理器: {'Redis' if session_manager._redis else '内存'} 模式")

    # 启动定时任务调度器
    from core.scheduler import start_scheduler
    start_scheduler()

    logger.info("系统就绪 ✓")
    yield
    # 优雅关闭
    from core.scheduler import stop_scheduler
    stop_scheduler()

    import asyncio
    try:
        pending = asyncio.all_tasks()
        for task in pending:
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except Exception:
        pass
    logger.info("系统关闭")


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用"""
    app = FastAPI(
        title="个人数字分身 · 多Agent私有RAG系统",
        description="基于 LangGraph + DeepSeek V4 Pro 的多智能体知识库问答系统",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        lifespan=lifespan,
    )

    # --- CORS 中间件 ---
    from config.settings import settings as app_settings
    cors_origins = app_settings.cors_origins
    if cors_origins == "*":
        cors_origins_list = ["*"]
    else:
        cors_origins_list = [o.strip() for o in cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- 请求限流（IP 级别，每分钟 N 次） ---
    limiter = Limiter(key_func=get_remote_address, default_limits=[f"{app_settings.rate_limit_per_minute}/minute"])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # --- 请求日志中间件 ---
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time
        logger.info(
            f"{request.method} {request.url.path} → {response.status_code} "
            f"({duration:.3f}s)"
        )
        return response

    # --- 审计日志中间件 ---
    from core.audit import AuditMiddleware, init_audit_table
    init_audit_table()
    app.add_middleware(AuditMiddleware)

    # --- 全局异常处理 ---
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(f"未处理的异常: {request.method} {request.url.path} - {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": f"服务器内部错误: {str(exc)[:500]}",
                "data": None,
            },
        )

    # --- 注册路由 ---
    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(kb_router)
    app.include_router(token_router)

    app.include_router(tool_router)
    app.include_router(admin_router)

    # --- 健康检查 ---
    @app.get("/api/health")
    async def health_check():
        return {"status": "ok", "version": "1.0.4-ssh-agent-fix"}

    # Prometheus 指标监控（可选，未安装则跳过）
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
        Instrumentator().instrument(app).expose(app, include_in_schema=False)
    except ImportError:
        pass

    return app


# 全局 app 实例
app = create_app()
