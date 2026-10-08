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
# 只有**管理侧**的读需要审计（它触及他人数据）。auth 的读不用：
# `/api/auth/requirements` 是登录页渲染表单时拉的公开配置，每个访客都拉，
# `/api/auth/me` 是前端加载时的会话自检 —— 都不是审计事件。
_AUDIT_READ_PREFIXES = ("/api/admin",)


def should_audit(method: str, path: str, status_code: int | None = None) -> bool:
    """
    这次请求值不值得进审计日志。

    规则（可解释、可测）：
    - 监控探针 / 静态资源 / SPA 外壳 → 不记（机械请求，无审计语义）
    - **写操作**（POST/PUT/PATCH/DELETE）→ 记（会改变状态）
    - `/api/auth/*` → **成功不记、失败才记**（见下）
    - `/api/admin/*`（管理员看全量数据）→ 记（触及他人数据）
    - 其余读取（含 `/api/auth/*` 的读）→ 不记

    为什么 auth 只记失败：登录/注册**成功**时路由自己会写一条语义审计
    （`log_audit("login", user_id=..., username=..., ip_address=...)`），信息比
    中间件的通用条目全；而中间件那条在登录时**还没有 token、拿不到用户身份**
    （实测库里两条并存，中间件那条 user_id 是空的）—— 留着就是每登录一次多一条垃圾。

    但**失败必须由中间件记**：路由在"密码错/账号锁定"时直接抛异常，不写审计，
    于是失败登录会一条记录都没有 —— 而那正是最该审计的（爆破检测）。
    """
    # **只审计我们自己的 API 面**。非 /api/ 的路径一律不记，这一步同时挡掉两类噪声：
    #
    #   1. 自家前端的机械请求：SPA 外壳 `/`（每次打开页面一条）、静态资源
    #      `/assets/*`、`/favicon.ico`；
    #   2. **互联网扫描器的探测**：公网 IP 暴露后必然被扫，实测日志里有
    #      `/index.php`（PHP 漏洞探测）、`/SDK/webLanguage`、`/robots.txt`
    #      —— 都不是我们的路由，却每条都进审计表，把真事件淹没。
    if not path.startswith("/api/"):
        return False

    if path.startswith("/api/auth") and method in _WRITE_METHODS:
        return status_code is not None and status_code >= 400

    if method in _WRITE_METHODS:
        return True
    return path.startswith(_AUDIT_READ_PREFIXES)


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
        if should_audit(request.method, request.url.path, response.status_code):
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


# HTTP 方法与"这条记录是不是机器请求"的判定，供**历史清理**复用同一条规则。
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def is_http_noise(action: str, status: str) -> bool:
    """
    这条**已有的**审计记录是不是"机械请求噪声"（供历史清理用）。

    与 `should_audit` 的关系：那个决定"要不要写"，这个决定"要不要删"。
    **必须同源** —— 各写一套必然漂移（删掉的类型还在写、或反过来），
    所以这里直接调 `should_audit`，不重复实现规则。

    语义事件（`login` / `register` / `delete_file`…）的 action 不是
    "METHOD /path" 形态，**一律不算噪声** —— 它们才是审计日志的本体。
    """
    parts = action.split(" ", 1)
    if len(parts) != 2 or not parts[1].startswith("/"):
        return False
    method, path = parts
    if method not in _HTTP_METHODS:
        return False
    return not should_audit(method, path, 200 if status == "success" else 400)
