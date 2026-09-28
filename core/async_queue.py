"""
异步任务队列
============

文档入库、长任务异步化，避免阻塞 API。

为什么默认用**进程内线程池**而不是 RQ
--------------------------------------
原实现只要 ``redis.ping()`` 成功就走 RQ 入队，这带来两个真实故障：

1. **Redis 版本不兼容**：RQ 2.x 需要 Redis >= 5（多字段 ``HSET`` 需要
   Redis >= 4）。本机是 Redis 3.2.100 时 ``ping`` 通过、``enqueue``
   抛 ``wrong number of arguments for 'hset' command`` —— 上传直接失败。
2. **部署缺 worker**：``Dockerfile`` 只起 FastAPI，``docker-compose.yml``
   没有 worker 服务。入队到 Redis 的任务**永远没人消费**，
   ``upload_tasks`` 卡在 pending，前端轮询永不结束。

单机部署下 RQ 带来的收益（跨进程、可横向扩展）用不上，却引入了两类
静默故障。因此默认改为**有界线程池**：任务不丢、线程数可控、零外部依赖。

RQ 仍可通过 ``USE_RQ_QUEUE=true`` 显式启用（需要部署独立 worker），
启用时会做一次能力探测，探测失败则回落到线程池并明确告警。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import count

from loguru import logger

from config.settings import settings

try:
    from redis import Redis
    from rq import Queue, Retry

    RQ_AVAILABLE = True
except ImportError:
    RQ_AVAILABLE = False


class QueueUnavailableError(RuntimeError):
    """任务无法提交（队列不可用）。调用方应据此向用户报错，而不是假装已提交。"""


class AsyncQueue:
    """
    异步任务队列管理器。

    - 默认：有界 ``ThreadPoolExecutor``（线程数 = ``WORKER_THREADS``）
    - ``USE_RQ_QUEUE=true`` 且能力探测通过：使用 RQ
    """

    def __init__(self) -> None:
        self._rq_queue = None
        self._redis = None
        self._seq = count(1)
        self._lock = threading.Lock()
        self._submitted = 0
        self._active = 0

        # 有界线程池：限制并发解析/向量化任务数，避免批量上传时
        # 每个文件裸起一个线程去抢 CPU 与 SQLite 写锁
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, settings.worker_threads),
            thread_name_prefix="task",
        )

        if settings.use_rq_queue:
            self._try_init_rq()
        else:
            logger.info(
                "异步队列: 进程内线程池（{} 线程）。如需 RQ，请设 USE_RQ_QUEUE=true 并部署 worker",
                settings.worker_threads,
            )

    # ── RQ 初始化 ──

    def _try_init_rq(self) -> None:
        """尝试启用 RQ；任何一步失败都回落到线程池并说明原因。"""
        if not RQ_AVAILABLE:
            logger.warning("USE_RQ_QUEUE=true 但 rq/redis 未安装，回落线程池")
            return
        try:
            self._redis = Redis.from_url(settings.redis_url)
            self._redis.ping()
        except Exception as e:
            logger.warning("Redis 连接失败，回落线程池: {}", e)
            self._redis = None
            return

        # 能力探测：Redis 3.x 上 HSET 不接受多字段映射，RQ 2.x 会直接入队失败。
        # 只 ping 不探测版本是原实现的漏洞 —— 连得上但用不了。
        probe_key = "__rq_capability_probe__"
        try:
            self._redis.hset(probe_key, mapping={"a": "1", "b": "2"})
        except Exception as e:
            logger.warning(
                "Redis 不支持多字段 HSET（版本过低），RQ 不可用，回落线程池: {}", e
            )
            self._redis = None
            return
        finally:
            try:
                self._redis.delete(probe_key)
            except Exception:
                pass
        if self._redis is None:
            return

        self._rq_queue = Queue("personal_agent", connection=self._redis)
        logger.info("异步队列: Redis (RQ)。请确认已部署独立 worker 进程")

    # ── 提交 ──

    def enqueue(self, func, *args, **kwargs) -> str:
        """
        提交异步任务，返回任务 ID。

        Raises:
            QueueUnavailableError: 任务**未能**提交。调用方必须向用户如实报错，
                不能吞掉异常假装已提交（原实现就是吞掉后返回 200，导致
                前端轮询一个永远不会完成的任务）。
        """
        if self._rq_queue is not None:
            try:
                job = self._rq_queue.enqueue(
                    func, *args, retry=Retry(max=3, interval=5), **kwargs
                )
                logger.info("任务入队(RQ): {} (job={})", func.__name__, job.id)
                return job.id
            except Exception as e:
                # RQ 运行时故障（Redis 掉线/权限/版本）→ 回落到线程池而不是丢任务
                logger.error(
                    "RQ 入队失败，回落线程池: {} - {}", func.__name__, e
                )

        try:
            self._executor.submit(self._run, func, args, kwargs)
        except RuntimeError as e:
            # 线程池已关闭（应用正在退出）
            raise QueueUnavailableError(f"任务队列不可用: {e}") from e

        with self._lock:
            self._submitted += 1
            task_id = f"task_{next(self._seq)}"
        logger.info("任务提交(线程池): {} ({})", func.__name__, task_id)
        return task_id

    def _run(self, func, args, kwargs) -> None:
        with self._lock:
            self._active += 1
        try:
            func(*args, **kwargs)
        except Exception as e:
            # 任务的失败状态由任务函数自己写进 upload_tasks；
            # 这里只保证异常不静默丢失
            logger.error("异步任务失败: {} - {}", getattr(func, "__name__", func), e)
        finally:
            with self._lock:
                self._active -= 1

    # ── 观测 ──

    def get_queue_size(self) -> int:
        """等待中 + 执行中的任务数。"""
        if self._rq_queue is not None:
            try:
                return self._rq_queue.count
            except Exception:
                return 0
        with self._lock:
            return self._submitted - self._active if self._active else self._submitted

    def health_check(self) -> bool:
        if self._rq_queue is not None:
            try:
                self._redis.ping()
                return True
            except Exception:
                return False
        return True

    def backend_name(self) -> str:
        """当前后端名称，供管理接口展示。"""
        return "rq" if self._rq_queue is not None else "thread_pool"

    def shutdown(self, wait: bool = False) -> None:
        """应用退出时调用，停止接受新任务。"""
        self._executor.shutdown(wait=wait, cancel_futures=not wait)


# 全局单例
async_queue = AsyncQueue()
