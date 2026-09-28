"""
审计日志模块
记录所有关键操作：登录、查询、文档操作、配置变更
"""
import time
import sqlite3
from pathlib import Path
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from config.settings import settings, PROJECT_ROOT


DB_PATH = PROJECT_ROOT / "assets" / "personal_agent.db"


def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_audit_table():
    """创建审计日志表"""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            action TEXT NOT NULL,
            resource TEXT,
            detail TEXT,
            ip_address TEXT,
            user_agent TEXT,
            duration_ms REAL,
            status TEXT DEFAULT 'success',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)")
    conn.commit()
    conn.close()


def log_audit(
    action: str,
    user_id: int = None,
    username: str = None,
    resource: str = None,
    detail: str = None,
    ip_address: str = None,
    user_agent: str = None,
    duration_ms: float = None,
    status: str = "success",
):
    """记录一条审计日志"""
    try:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO audit_log (user_id, username, action, resource, detail,
               ip_address, user_agent, duration_ms, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, username, action, resource, detail, ip_address, user_agent, duration_ms, status),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"审计日志写入失败: {e}")


class AuditMiddleware(BaseHTTPMiddleware):
    """自动记录所有 HTTP API 请求到审计日志"""

    async def dispatch(self, request, call_next):
        start = time.time()

        # 提取客户端 IP（统一走 core.net，避免各处重复实现取错段）
        from core.net import client_ip

        ip = client_ip(request)

        # 提取用户信息
        user_id = None
        username = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                from core.auth import decode_token
                payload = decode_token(auth_header[7:])
                if payload:
                    user_id = payload.get("sub")
                    username = payload.get("username")
            except Exception:
                pass

        response = await call_next(request)
        duration = (time.time() - start) * 1000

        # 跳过健康检查的审计，避免日志噪音
        if request.url.path != "/api/health":
            log_audit(
                action=f"{request.method} {request.url.path}",
                user_id=user_id,
                username=username,
                resource=request.url.path,
                detail=f"status={response.status_code}",
                ip_address=ip,
                user_agent=request.headers.get("User-Agent", ""),
                duration_ms=round(duration, 2),
                status="success" if response.status_code < 400 else "failure",
            )

        return response
