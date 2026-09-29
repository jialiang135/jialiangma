"""
FastAPI 主入口 + 全局中间件
"""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from api.routes.admin_routes import router as admin_router
from api.routes.auth_routes import router as auth_router
from api.routes.chat_routes import router as chat_router
from api.routes.eval_routes import router as eval_router
from api.routes.kb_routes import router as kb_router
from api.routes.token_routes import router as token_router
from api.routes.tool_routes import router as tool_router
from api.routes.tts_routes import router as tts_router
from config.settings import settings
from core.auth import hash_password
from core.database import create_admin_user, init_database


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理（替代废弃的 on_event）"""
    logger.info("=" * 60)
    logger.info("个人数字分身 · 多Agent私有RAG系统 启动中...")
    logger.info("=" * 60)

    # 记录主事件循环：APScheduler 的后台线程需要把异步 DB 操作提交回来执行
    from config.settings import close_llm_clients
    from core.database import bind_main_loop, dispose_engine
    from core.telemetry import setup_telemetry

    # 链路追踪（未安装 OTel 时自动降级为 no-op，不影响启动）
    setup_telemetry()

    bind_main_loop(asyncio.get_running_loop())

    await init_database()
    await create_admin_user(settings.admin_username, hash_password(settings.admin_password))
    logger.info(f"DeepSeek 模型: {settings.deepseek_model}")
    logger.info(f"Embedding 模型: {settings.embedding_model}")
    logger.info(f"Rerank 模型: {settings.rerank_model}")
    logger.info(f"ChromaDB 路径: {settings.chroma_persist_dir}")
    logger.info(f"上传文件路径: {settings.upload_dir}")
    logger.info(f"数据库路径: {settings.db_path}")

    # 知识库一致性自检
    # 「文件表」与「向量库」会独立漂移，而且**漂移时不报错**：
    # 界面显示"知识库为空"，模型却仍能引用某份文档回答 ——
    # 用户看到的是一个自信的错答案。启动时主动查一次，别等用户发现。
    try:
        from core.database import get_files_by_owner
        from rag.vector_store import get_collection_stats

        kb_files = {f["filename"] for f in await get_files_by_owner(1) if f.get("filename")}
        kb_stats = await asyncio.to_thread(get_collection_stats, 1)
        vec_sources = set(kb_stats.get("files") or [])
        logger.info(
            "知识库: 文件记录 {} 条 / 向量块 {} 个",
            len(kb_files),
            kb_stats.get("total_chunks", 0),
        )
        orphans = sorted(vec_sources - kb_files)
        if orphans:
            logger.warning(
                "⚠️ 知识库不一致：向量库里存在文件表中没有的来源 {} —— "
                "这会造成「界面显示知识库为空，但回答却能引用文档」。"
                "可访问 GET /api/admin/kb-consistency 查看，"
                "POST /api/admin/kb-consistency/repair 清理",
                orphans,
            )
    except Exception as e:
        logger.debug("知识库一致性自检跳过: {}", e)

    # 启动定时任务调度器
    from core.scheduler import start_scheduler

    start_scheduler()

    logger.info("系统就绪 ✓")
    yield
    # 优雅关闭
    from core.async_queue import async_queue
    from core.scheduler import stop_scheduler

    stop_scheduler()
    # 不等待长时间任务跑完（比如正在做 OCR 的文档），避免关闭卡住；
    # 未完成的任务会留在 upload_tasks 里，下次启动可见
    async_queue.shutdown(wait=False)
    # 先关 LLM 的 HTTP 客户端，再释放数据库引擎：
    # 顺序反了的话 httpx 会在事件循环关闭后才被回收，冒出未处理异常
    await close_llm_clients()
    await dispose_engine()

    # flush 未发送的 span（放最后，保证前面的关闭动作也能被追踪到）
    from core.telemetry import shutdown_telemetry

    shutdown_telemetry()
    # 注意：不要在这里 asyncio.all_tasks() 后逐个 cancel —— 那是把所有任务
    # （含 ASGI 框架自身的门户任务、连接处理任务）都取消掉，会让 TestClient
    # 退出和 uvicorn 优雅关闭抛 CancelledError。未完成的请求交给 uvicorn
    # 自己的优雅关闭流程处理。
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
    from core.net import client_ip

    cors_origins = app_settings.cors_origins
    if cors_origins.strip() == "*":
        cors_origins_list = ["*"]
        # 浏览器规范：`Access-Control-Allow-Origin: *` 与 credentials 不能共存。
        # 两者同时设置时，带凭据的跨域请求会被浏览器直接拒绝 —— 配置看似"全开放"，
        # 实际效果是"全都不能用"。这里显式关掉 credentials 让语义自洽。
        allow_credentials = False
        logger.warning(
            "CORS 允许所有来源且已关闭 credentials；生产环境请把 CORS_ORIGINS 设为具体域名"
        )
    else:
        cors_origins_list = [o.strip() for o in cors_origins.split(",") if o.strip()]
        allow_credentials = True

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins_list,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- 请求限流（真实客户端 IP 级别，每分钟 N 次） ---
    # 不能用 slowapi 默认的 get_remote_address：它取 request.client.host，
    # 经 Nginx 反代后拿到的是代理容器 IP，会让全站共享一个计数器、限流实际失效。
    #
    # ⚠️ **必须挂 SlowAPIMiddleware 才会生效**。原来这里只创建了 Limiter、
    # 注册了异常处理器，却没有 add_middleware —— 而 slowapi 的 `default_limits`
    # 是**由中间件执行**的。结果是限流完全没在跑（README 里却写着"slowapi 限流"），
    # 且没有任何测试能发现，因为测试里连一个 429 用例都没有。
    # 现在补上中间件，并用 tests/test_rate_limit.py 守住"它确实在跑"。
    limiter = Limiter(
        key_func=client_ip,
        default_limits=[f"{app_settings.rate_limit_per_minute}/minute"],
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # --- 请求日志中间件 ---
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time
        logger.info(
            f"{request.method} {request.url.path} → {response.status_code} ({duration:.3f}s)"
        )
        return response

    # --- 审计日志中间件 ---
    # 审计表由 core/database.init_database 用模型统一建好，这里不再单独建表
    from core.audit import AuditMiddleware

    app.add_middleware(AuditMiddleware)

    # --- 全局异常处理 ---
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        # 把内部异常细节回传给客户端会泄露实现信息（文件路径、SQL、依赖版本等），
        # 因此只回一个 trace_id，细节写日志，便于用户报错时对齐。
        trace_id = uuid.uuid4().hex[:12]
        logger.opt(exception=exc).error(
            "未处理的异常 [trace={}] {} {}", trace_id, request.method, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": "服务器内部错误，请稍后重试",
                "trace_id": trace_id,
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
    app.include_router(eval_router)
    app.include_router(tts_router)

    # --- 健康检查 ---
    @app.get("/api/health")
    async def health_check():
        return {"status": "ok", "version": "1.0.6-final"}

    # Prometheus 指标监控（可选，未安装则跳过）
    try:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator().instrument(app).expose(app, include_in_schema=False)
    except ImportError:
        pass

    return app


# 全局 app 实例
app = create_app()
