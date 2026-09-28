"""
个人数字分身 · 多Agent私有RAG系统 — 启动入口
FastAPI 主服务 + Vue 3 前端静态文件托管
"""
import sys
import os
from pathlib import Path

# 确保项目根目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.main import app as fastapi_app
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger


# 项目根目录
BASE_DIR = Path(__file__).parent
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"


def mount_frontend():
    """挂载 Vue 3 前端静态文件"""
    if not FRONTEND_DIST.exists():
        logger.warning(f"前端构建目录不存在: {FRONTEND_DIST}")
        logger.warning("请先运行: cd frontend && npm install && npm run build")
        return

    index_path = FRONTEND_DIST / "index.html"

    # 静态资源（JS/CSS/字体等）
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.exists():
        fastapi_app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend_assets")

    # SPA 页面路由
    @fastapi_app.get("/")
    @fastapi_app.get("/chat")
    @fastapi_app.get("/knowledge")
    @fastapi_app.get("/admin")
    @fastapi_app.get("/eval")
    async def serve_spa():
        if index_path.exists():
            return FileResponse(str(index_path))
        return {"message": "前端未构建"}

    logger.info(f"Vue 3 前端已挂载: {FRONTEND_DIST}")


# 挂载前端
mount_frontend()

# 创建 app 实例（供 uvicorn 引用）
app = fastapi_app

if __name__ == "__main__":
    import socket
    import uvicorn
    from uvicorn import Config
    from config.settings import settings

    # 获取本机局域网 IP（用于手机/其他设备访问）
    def get_lan_ip() -> str | None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip if ip != "127.0.0.1" else None
        except Exception:
            return None

    lan_ip = get_lan_ip()

    # 替换 uvicorn 默认的 "Uvicorn running on http://0.0.0.0:7860"
    # 改为显示实际可用的浏览器地址
    class FriendlyServer(uvicorn.Server):
        def _log_started_message(self, listeners):
            lines = [
                "=" * 60,
                "🚀 个人数字分身 · 多Agent私有RAG系统 启动成功！",
                "=" * 60,
                f"   👉 本机访问:  http://127.0.0.1:{self.config.port}",
                *([f"   👉 手机访问:  http://{lan_ip}:{self.config.port}  (同WiFi下)"] if lan_ip else []),
                f"   📖 API 文档:  http://127.0.0.1:{self.config.port}/api/docs",
                f"   🔑 管理员:    {settings.admin_username}  （密码见 config/.env 的 ADMIN_PASSWORD）",
                "=" * 60,
            ]
            for line in lines:
                logger.info(line)

    config = Config(
        app,
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
    server = FriendlyServer(config=config)
    server.run()
