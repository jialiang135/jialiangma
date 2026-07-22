"""
熔断降级模块
当 LLM API 调用连续失败时自动熔断，返回降级兜底回答
"""
import time
import threading
from enum import Enum
from functools import wraps
from loguru import logger


class CircuitState(Enum):
    CLOSED = "closed"          # 正常通行
    OPEN = "open"              # 熔断打开，拒绝请求
    HALF_OPEN = "half_open"    # 半开，尝试放行一个请求


class CircuitBreaker:
    """熔断器 —— 线程安全"""

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,       # 连续失败 N 次后打开熔断
        recovery_timeout: float = 60.0,   # 熔断后 N 秒进入半开状态
        half_open_max: int = 1,           # 半开状态允许的试探请求数
        backoff_base: float = 2.0,        # 指数退避基数
        max_retries: int = 3,             # 最大重试次数
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
        self._half_open_count = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            # 如果熔断打开且过了恢复时间，进入半开状态
            if (self._state == CircuitState.OPEN and
                    time.time() - self._last_failure_time >= self.recovery_timeout):
                self._state = CircuitState.HALF_OPEN
                self._half_open_count = 0
                logger.info(f"🔧 熔断器 [{self.name}] 进入半开状态，尝试恢复")
            return self._state

    def on_success(self):
        """调用成功后重置"""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_count = 0

    def on_failure(self):
        """调用失败后记录"""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning(f"⚠️ 熔断器 [{self.name}] 半开试探失败，重新熔断")
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.error(
                    f"🚨 熔断器 [{self.name}] 已打开！连续失败 {self._failure_count} 次，"
                    f"{self.recovery_timeout}s 后尝试恢复"
                )

    def can_pass(self) -> bool:
        """检查是否允许通过"""
        s = self.state
        if s == CircuitState.CLOSED:
            return True
        if s == CircuitState.HALF_OPEN:
            with self._lock:
                if self._half_open_count < self.half_open_max:
                    self._half_open_count += 1
                    return True
            return False
        return False

    def get_fallback_response(self, original_query: str = "") -> str:
        """返回降级兜底回答"""
        return (
            "抱歉，AI 服务暂时不可用，请稍后重试。\n\n"
            "可能的原因：\n"
            "• 大模型 API 调用暂时失败\n"
            "• 系统正在自动恢复中\n\n"
            "建议：\n"
            "1. 等待 1-2 分钟后重试\n"
            "2. 联系管理员检查 API 密钥配置\n"
            "3. 查看系统监控面板了解详情"
        )


# 全局 LLM 熔断器实例
llm_circuit_breaker = CircuitBreaker(
    name="deepseek_llm",
    failure_threshold=5,
    recovery_timeout=60.0,
    max_retries=3,
)

embedding_circuit_breaker = CircuitBreaker(
    name="dashscope_embedding",
    failure_threshold=5,
    recovery_timeout=60.0,
    max_retries=2,
)


def with_circuit_breaker(cb: CircuitBreaker, fallback_value=None):
    """装饰器：对异步函数添加熔断保护"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if not cb.can_pass():
                logger.warning(f"熔断器 [{cb.name}] 拒绝请求，返回降级结果")
                if fallback_value is not None:
                    return fallback_value
                if callable(cb.get_fallback_response):
                    return cb.get_fallback_response(getattr(kwargs, 'query', ''))
                return cb.get_fallback_response()

            last_exception = None
            for attempt in range(cb.max_retries + 1):
                try:
                    result = await func(*args, **kwargs)
                    cb.on_success()
                    return result
                except Exception as e:
                    last_exception = e
                    logger.warning(
                        f"熔断器 [{cb.name}] 第 {attempt + 1}/{cb.max_retries + 1} 次失败: {e}"
                    )
                    if attempt < cb.max_retries:
                        wait = cb.backoff_base ** attempt
                        time.sleep(wait)

            cb.on_failure()
            if fallback_value is not None:
                return fallback_value
            raise last_exception

        return wrapper
    return decorator
