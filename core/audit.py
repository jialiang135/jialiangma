"""
审计日志模块
记录所有关键操作：登录、查询、文档操作、配置变更
"""

import time

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

# 建表语句已收归 core/database.py 的 AuditLog 模型，
# 这里不再自己建表 —— 表结构的唯一来源应当是数据层。
from core.database import insert_audit_log


async def log_audit(
    action: str,
    user_id: int | None = None,
    username: str | None = None,
    resource: str | None = None,
    detail: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    duration_ms: float | None = None,
    status: str = "success",
) -> None:
    """记录一条审计日志（异步写入）。"""
    await insert_audit_log(
        action=action,
        user_id=user_id,
        username=username,
        resource=resource,
        detail=detail,
        ip_address=ip_address,
        user_agent=user_agent,
        duration_ms=duration_ms,
        status=status,
    )


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
        #
        # 审计写入**绝不能影响业务请求**：原实现直接 await，一旦审计表写失败
        # （SQLite busy、磁盘满、表结构变更），异常会在 call_next 成功返回之后
        # 抛出，客户端拿到 500 —— 而操作其实已经成功了。
        # 这是"日志把成功变成失败"的典型，所以这里吞掉异常，但**留下错误日志**，
        # 而不是静默丢弃（否则审计坏了也没人知道）。
        if request.url.path != "/api/health":
            try:
                await log_audit(
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
            except Exception as e:
                logger.error(
                    "[Audit] 审计写入失败（不影响本次请求）: {} {} - {}",
                    request.method,
                    request.url.path,
                    e,
                )

        return response
