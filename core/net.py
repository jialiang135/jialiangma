"""
网络工具
========

统一提取"真实客户端 IP"，供限流、审计、登录失败计数共用。

为什么不能直接用 ``request.client.host``
----------------------------------------
部署在反向代理（Nginx）之后时，``request.client.host`` 是**代理容器的地址**
（例如 Docker 网络里的 ``172.x.x.x``）。所有请求看起来来自同一个 IP，
于是"每 IP 每分钟 N 次"的限流退化成**全站共享一个计数器**——真实用户
会被别人的请求量拖累，攻击者也只受全局限额约束。

为什么取 X-Forwarded-For 的**最后**一段而不是第一段
----------------------------------------------------
``X-Forwarded-For`` 是**追加**语义：客户端若自己带一个
``X-Forwarded-For: 1.2.3.4``，Nginx 的 ``$proxy_add_x_forwarded_for``
会把它保留在前面，再追加真实对端地址。所以：

- 第一段 = 客户端自称的值，**可任意伪造**（审计日志若记它，等于记假 IP）
- 最后一段 = 我们的代理追加的真实对端

同理 ``X-Real-IP``（Nginx 用 ``$remote_addr`` 覆写）比 XFF 更可靠，
因为它是覆写而非追加，客户端无法注入。因此优先顺序为：
``X-Real-IP`` → ``X-Forwarded-For`` 最后一段 → ``request.client.host``。

前提是**请求确实经过我们可控的代理**，否则客户端可以直接伪造
``X-Real-IP`` 绕过限流 —— 这就是 ``trust_proxy_headers`` 开关的意义。
"""
from __future__ import annotations

from fastapi import Request

from config.settings import settings

UNKNOWN_IP = "unknown"


def client_ip(request: Request) -> str:
    """
    提取真实客户端 IP。

    直接暴露在本机（无反向代理）时应把 ``TRUST_PROXY_HEADERS`` 设为 false，
    否则客户端可以自带 ``X-Real-IP`` 伪造来源、绕过基于 IP 的限流。
    """
    if not settings.trust_proxy_headers:
        return request.client.host if request.client else UNKNOWN_IP

    # 1. X-Real-IP：Nginx 用 $remote_addr 覆写，客户端无法注入
    real_ip = request.headers.get("X-Real-IP")
    if real_ip and real_ip.strip():
        return real_ip.strip()

    # 2. X-Forwarded-For 的最后一段：由我们的代理追加，前面的都可能是伪造
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]

    # 3. 兜底
    return request.client.host if request.client else UNKNOWN_IP
