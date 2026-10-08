"""
审计日志模块
记录所有关键操作：登录、查询、文档操作、配置变更
"""

import time

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

# 建表语句已收归 core/database.py 的 AuditLog 模型，
# 这里不再自己建表 —— 表结构的唯一来源应当是数据层。
from core.db.audit import insert_audit_log


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


# ── 审计范围 ──
#
# **审计日志记的是"谁改了什么、谁碰了别人的数据"**，不是"收到过哪些 HTTP 请求"。
#
# 原实现只跳过 `/api/health`，其余一律入库 —— 实测线上 10 天攒了 17,470 行，其中：
#     GET /metrics                 9,897 行（56%）  ← Prometheus 每 15 秒抓一次
#     GET /                        1,993 行        ← SPA 首页
#     GET /assets/index-*.js         173 行        ← 静态资源
# 真正的审计事件（登录、改角色、删用户、删文件）淹没在这些噪声里，
# 而每一条噪声都是一次 SQLite 写入（写放大），审计表还会无限膨胀。
#
# 现在的规则：**写操作 + 认证 + 管理接口**。读自己的数据不入库。
_SKIP_PREFIXES = ("/assets/", "/metrics", "/nginx-health", "/favicon")
_SKIP_EXACT = ("/", "/index.html", "/docs", "/redoc", "/openapi.json")
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_AUDIT_PATH_PREFIXES = ("/api/auth", "/api/admin")


def should_audit(method: str, path: str) -> bool:
    """
    这次请求值不值得进审计日志。

    规则（可解释、可测）：
    - 监控探针 / 静态资源 / SPA 外壳 → 不记（机械请求，无审计语义）
    - **写操作**（POST/PUT/PATCH/DELETE）→ 记（会改变状态）
    - `/api/auth/*`（登录、注册、改密）→ 记
    - `/api/admin/*`（管理员看全量数据）→ 记（触及他人数据）
    - 其余读取 → 不记
    """
    if path in _SKIP_EXACT or path.startswith(_SKIP_PREFIXES):
        return False
    if method in _WRITE_METHODS:
        return True
    return path.startswith(_AUDIT_PATH_PREFIXES)


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
        if should_audit(request.method, request.url.path):
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
