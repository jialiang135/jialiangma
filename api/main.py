"""
FastAPI 主入口 + 全局中间件
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
import time

from api.routes.auth_routes import router as auth_router
from api.routes.chat_routes import router as chat_router
from api.routes.kb_routes import router as kb_router
from api.routes.eval_routes import router as eval_router
from api.routes.token_routes import router as token_router
from api.routes.export_routes import router as export_router
from core.database import init_database, create_admin_user
from core.auth import hash_password
from config.settings import settings


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用"""
    app = FastAPI(
        title="个人数字分身 · 多Agent私有RAG系统",
        description="基于 LangGraph + DeepSeek V4 Pro 的多智能体知识库问答系统",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    # --- CORS 中间件 ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
    app.include_router(eval_router)
    app.include_router(token_router)
    app.include_router(export_router)

    # --- 健康检查 ---
    @app.get("/api/health")
    async def health_check():
        return {"status": "ok", "version": "1.0.0"}

    # --- 启动初始化（lifespan 替代废弃的 on_event） ---
    @app.on_event("startup")
    async def startup():
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
        logger.info("系统就绪 ✓")

    return app


# 全局 app 实例
app = create_app()
