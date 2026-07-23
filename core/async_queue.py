"""
异步任务队列
文档入库、长任务异步化，避免阻塞 API
基于 Python RQ（Redis Queue，轻量级，无需单独部署）
"""
import threading
import time
from loguru import logger

try:
    from redis import Redis
    from rq import Queue, Retry
    RQ_AVAILABLE = True
except ImportError:
    RQ_AVAILABLE = False


class AsyncQueue:
    """
    异步任务队列管理器
    - Redis 可用：使用 RQ（生产级别）
    - Redis 不可用：使用线程池 fallback
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self._rq_queue = None
        self._redis = None
        self._threads: list[threading.Thread] = []

        if RQ_AVAILABLE:
            try:
                self._redis = Redis.from_url(redis_url)
                self._redis.ping()
                self._rq_queue = Queue("personal_agent", connection=self._redis)
                logger.info("✅ 异步队列已连接 Redis (RQ)")
            except Exception as e:
                logger.warning(f"⚠️ Redis 连接失败，使用线程池 fallback: {e}")
                self._redis = None

    def enqueue(self, func, *args, **kwargs):
        """
        提交异步任务
        用法: async_queue.enqueue(process_document, file_path, owner_id)
        """
        if self._rq_queue:
            job = self._rq_queue.enqueue(func, *args, **kwargs,
                                         retry=Retry(max=3, interval=5))
            logger.info(f"📨 任务入队: {func.__name__} (job={job.id})")
            return job.id
        else:
            # 线程池 fallback
            t = threading.Thread(target=self._thread_wrapper, args=(func, args, kwargs), daemon=True)
            t.start()
            self._threads = [t for t in self._threads if t.is_alive()] + [t]
            logger.info(f"📨 任务提交(线程): {func.__name__}")
            return f"thread_{id(t)}"

    def _thread_wrapper(self, func, args, kwargs):
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error(f"❌ 异步任务失败: {func.__name__} - {e}")

    def get_queue_size(self) -> int:
        """获取队列积压数量"""
        if self._rq_queue:
            return self._rq_queue.count
        return len([t for t in self._threads if t.is_alive()])

    def health_check(self) -> bool:
        """检查队列是否可用"""
        if self._rq_queue:
            try:
                self._redis.ping()
                return True
            except Exception:
                return False
        return True  # 线程池始终可用


# 全局单例（使用 settings 中的 Redis URL，Docker 中自动指向 redis 服务）
from config.settings import settings as _settings
async_queue = AsyncQueue(redis_url=_settings.redis_url)
