"""
熔断降级模块
============

当 LLM / Embedding 等外部 API 连续失败时自动熔断，避免对着已经挂掉的服务
反复重试（既拖慢请求，也可能触发对方的限流）。

原实现的三个问题（本次一并修掉）
--------------------------------
1. ``time.sleep(wait)`` 写在 ``async def wrapper`` 里 —— **阻塞整个事件循环**，
   且退避是 1s/2s/4s 量级，卡住的代价很大。改为 ``await asyncio.sleep``。
2. ``getattr(kwargs, 'query', '')`` —— ``kwargs`` 是 dict，``getattr`` 永远
   返回 ``''``，降级回答永远拿不到原始问题。改为 ``kwargs.get('query', '')``。
3. **装饰器从未被任何代码使用** —— 两个全局熔断器实例只被管理接口读取用于
   展示，``on_failure()`` 永远不触发，面板上永远是 CLOSED。
   现在真正接入了 LLM 调用与 Embedding 调用。

同时补了一个"半开态只放行一个探测请求"的正确语义：原实现 ``can_pass`` 在
半开态会递增计数但失败时不释放，容易卡死。
"""

import asyncio
import threading
import time
from enum import Enum
from functools import wraps

from loguru import logger


class CircuitState(Enum):
    CLOSED = "closed"  # 正常通行
    OPEN = "open"  # 熔断打开，拒绝请求
    HALF_OPEN = "half_open"  # 半开，尝试放行少量探测请求


class CircuitOpenError(RuntimeError):
    """熔断打开，请求被拒绝。"""


class CircuitBreaker:
    """熔断器 —— 线程安全，同时支持异步与同步调用。"""

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,  # 连续失败 N 次后打开熔断
        recovery_timeout: float = 60.0,  # 熔断后 N 秒进入半开状态
        half_open_max: int = 1,  # 半开状态允许的试探请求数
        backoff_base: float = 2.0,  # 指数退避基数
        max_retries: int = 3,  # 最大重试次数
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max = half_open_max
        self.backoff_base = backoff_base
        self.max_retries = max_retries

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._half_open_inflight = 0
        self._lock = threading.Lock()

    # ── 状态 ──

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        """调用方需持有锁。打开态且超过恢复时间 → 进入半开。"""
        if (
            self._state == CircuitState.OPEN
            and time.time() - self._last_failure_time >= self.recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            self._half_open_inflight = 0
            logger.info("🔧 熔断器 [{}] 进入半开状态，尝试恢复", self.name)

    def on_success(self) -> None:
        with self._lock:
            if self._state != CircuitState.CLOSED:
                logger.info("✅ 熔断器 [{}] 恢复正常（CLOSED）", self.name)
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_inflight = 0

    def on_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning("⚠️ 熔断器 [{}] 半开试探失败，重新熔断", self.name)
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.error(
                    "🚨 熔断器 [{}] 已打开！连续失败 {} 次，{}s 后尝试恢复",
                    self.name,
                    self._failure_count,
                    self.recovery_timeout,
                )

    def can_pass(self) -> bool:
        """检查是否允许通过（半开态限量放行）。"""
        with self._lock:
            self._maybe_half_open()
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_inflight < self.half_open_max:
                    self._half_open_inflight += 1
                    return True
                return False
            return False

    def snapshot(self) -> dict:
        """当前状态快照（供管理接口展示）。"""
        with self._lock:
            self._maybe_half_open()
            return {
                "name": self.name,
                "state": self._state.value,
                "failure_count": self._failure_count,
                "failure_threshold": self.failure_threshold,
                "can_pass": self._can_pass_locked(),
                "half_open_max": self.half_open_max,
                "recovery_timeout": self.recovery_timeout,
            }

    def _can_pass_locked(self) -> bool:
        """不加锁的 can_pass（调用方已持锁，仅用于快照展示）。"""
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.HALF_OPEN:
            return self._half_open_inflight < self.half_open_max
        return False

    # ── 调用 ──

    async def call(self, func, *args, **kwargs):
        """
        执行受保护的**异步**调用：带重试与指数退避，失败累计触发熔断。

        Raises:
            CircuitOpenError: 熔断打开，直接拒绝（不重试）。
            原始异常:         重试耗尽后抛出，由调用方决定降级策略。
        """
        if not self.can_pass():
            logger.warning("熔断器 [{}] 拒绝请求", self.name)
            raise CircuitOpenError(f"服务暂时不可用（熔断器 {self.name} 已打开）")

        last_exception = None
        for attempt in range(self.max_retries + 1):
            try:
                result = await func(*args, **kwargs)
                self.on_success()
                return result
            except Exception as e:
                last_exception = e
                logger.warning(
                    "熔断器 [{}] 第 {}/{} 次失败: {}",
                    self.name,
                    attempt + 1,
                    self.max_retries + 1,
                    e,
                )
                if attempt < self.max_retries:
                    # 必须是 asyncio.sleep：time.sleep 会阻塞整个事件循环
                    await asyncio.sleep(self.backoff_base**attempt)

        self.on_failure()
        raise last_exception

    def call_sync(self, func, *args, **kwargs):
        """
        执行受保护的**同步**调用（供 embedding 这类同步链路使用，
        它们本身已在 ``asyncio.to_thread`` 里，所以这里用 time.sleep 是安全的）。
        """
        if not self.can_pass():
            logger.warning("熔断器 [{}] 拒绝请求", self.name)
            raise CircuitOpenError(f"服务暂时不可用（熔断器 {self.name} 已打开）")

        last_exception = None
        for attempt in range(self.max_retries + 1):
            try:
                result = func(*args, **kwargs)
                self.on_success()
                return result
            except Exception as e:
                last_exception = e
                logger.warning(
                    "熔断器 [{}] 第 {}/{} 次失败: {}",
                    self.name,
                    attempt + 1,
                    self.max_retries + 1,
                    e,
                )
                if attempt < self.max_retries:
                    time.sleep(self.backoff_base**attempt)

        self.on_failure()
        raise last_exception

    # ── 降级文案 ──

    def get_fallback_response(self, original_query: str = "") -> str:
        """返回降级兜底回答。"""
        prefix = f"（原始问题：{original_query[:50]}…）\n\n" if original_query else ""
        return (
            prefix + "抱歉，AI 服务暂时不可用，请稍后重试。\n\n"
            "可能的原因：\n"
            "• 大模型 API 调用暂时失败\n"
            "• 系统正在自动恢复中\n\n"
            "建议：\n"
            "1. 等待 1-2 分钟后重试\n"
            "2. 联系管理员检查 API 密钥配置\n"
            "3. 查看系统监控面板了解详情"
        )


# 全局熔断器实例
# 注意：LLM 的 max_retries 调低到 2 —— 单次 LLM 调用本身就要数秒到数十秒，
# 重试 3 次会把用户等待时间放大到不可接受；熔断的价值在于"快速失败"。
llm_circuit_breaker = CircuitBreaker(
    name="deepseek_llm",
    failure_threshold=5,
    recovery_timeout=60.0,
    max_retries=2,
)

embedding_circuit_breaker = CircuitBreaker(
    name="dashscope_embedding",
    failure_threshold=5,
    recovery_timeout=60.0,
    # 不在此层重试：重试由 core/embeddings.py 里的 tenacity 负责，
    # 这里只负责"连续失败到阈值就快速失败"
    max_retries=0,
)


def with_circuit_breaker(cb: CircuitBreaker, fallback_value=None):
    """
    装饰器：给**异步**函数加熔断保护。

    熔断打开或重试耗尽时返回 ``fallback_value``（未提供则抛出原异常）。
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await cb.call(func, *args, **kwargs)
            except CircuitOpenError:
                if fallback_value is not None:
                    return fallback_value
                # 兜底回答要带上原始问题 —— 原实现用 getattr(kwargs, 'query', '')
                # 取不到值（kwargs 是 dict），降级文案里永远没有问题原文
                return cb.get_fallback_response(str(kwargs.get("query", ""))[:60])
            except Exception:
                if fallback_value is not None:
                    return fallback_value
                raise

        return wrapper

    return decorator
