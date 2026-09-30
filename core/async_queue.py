"""
异步任务队列
============

文档入库、评测这类长任务异步化，避免阻塞 API 请求。

为什么用**进程内线程池**，不引入 Redis / RQ
-------------------------------------------
这里曾经有一整套 RQ（Redis Queue）实现，包括连接探测、多字段 HSET 能力探测、
入队重试、失败回落。**已删除**，理由不是"没写完"，而是三个实测出来的结论：

1. **本部署用不到 RQ 提供的东西。** RQ 买到的是"跨进程共享 + 可横向扩展 +
   重启不丢任务"。而本系统是单容器单进程、2 核 4G；入库那个活儿是
   CPU + 外部 API 混合，瓶颈在 DashScope embedding 的 RTT，不在进程数。
   线程池 4 个并行已经够。

2. **成本是真的。** 多一个容器（128M 常驻）、多一个启动依赖、多一个失败面。
   而一旦真用上，Redis 就从"白跑"变成**单点故障**：它挂 = 上传直接坏。

3. **最要命的是"预留"会咬人。** 上层代码（`Dockerfile` 只起 FastAPI、
   `docker-compose.yml` 没有 worker 服务）决定了 RQ 路径**没有消费端**。
   而能力探测只能验 Redis 版本，**验不出"有没有 worker 在消费"**。
   于是 `USE_RQ_QUEUE=true` 一填，探测全过 → 任务入队 → 永远没人消费 →
   `upload_tasks` 卡在 pending，前端轮询永不结束。这是一个静默故障。

所以：池子留在进程内，**任务不丢、线程数可控、零外部依赖**。

要换成真正的分布式队列时（出现多副本部署、或"分钟级任务不能因重启丢失"
这类需求），正确做法不是把这个文件改回去，而是让任务提交走一个可替换的
接口 —— 目前只有一个实现，抽象它属于仪式感。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import count

from loguru import logger

from config.settings import settings


class QueueUnavailableError(RuntimeError):
    """任务无法提交（队列已关闭）。调用方应据此向用户报错，而不是假装已提交。"""


class AsyncQueue:
    """
    有界线程池任务队列。

    为什么是**有界**：批量上传时若每个文件裸起一个线程，会一起去抢 CPU 与
    SQLite 写锁，反而更慢。线程数固定为 ``WORKER_THREADS``，超出的任务在
    池里排队。
    """

    def __init__(self) -> None:
        self._seq = count(1)
        self._lock = threading.Lock()
        # 三个计数用于观测真实的积压情况（见 get_queue_size）
        self._submitted = 0  # 累计提交
        self._started = 0  # 已被线程取走开始执行
        self._active = 0  # 正在执行

        self._capacity = max(1, settings.worker_threads)
        self._executor = ThreadPoolExecutor(
            max_workers=self._capacity,
            thread_name_prefix="task",
        )
        logger.info("异步队列: 进程内线程池（{} 线程）", settings.worker_threads)

    # ── 提交 ──

    def enqueue(self, func, *args, **kwargs) -> str:
        """
        提交异步任务，返回任务 ID。

        Raises:
            QueueUnavailableError: 任务**未能**提交（线程池已关闭，应用正在退出）。
                调用方必须向用户如实报错，不能吞掉异常假装已提交 —— 那样前端会
                去轮询一个永远不会完成的任务。
        """
        try:
            self._executor.submit(self._run, func, args, kwargs)
        except RuntimeError as e:
            raise QueueUnavailableError(f"任务队列不可用: {e}") from e

        with self._lock:
            self._submitted += 1
            task_id = f"task_{next(self._seq)}"
        logger.info("任务提交: {} ({})", func.__name__, task_id)
        return task_id

    def _run(self, func, args, kwargs) -> None:
        with self._lock:
            self._started += 1
            self._active += 1
        try:
            func(*args, **kwargs)
        except Exception as e:
            # 任务的失败状态由任务函数自己写进 upload_tasks / eval_reports；
            # 这里只保证异常不静默丢失
            logger.error("异步任务失败: {} - {}", getattr(func, "__name__", func), e)
        finally:
            with self._lock:
                self._active -= 1

    # ── 观测 ──

    def get_queue_size(self) -> int:
        """**等待中**的任务数（已提交但还没被线程取走）。

        注意别把"累计提交数"当成积压 —— 曾经就是这个错：原实现在没有任务
        执行时返回累计提交总数，于是管理后台的"队列积压"只增不减，看起来
        像队列堵死了。
        """
        with self._lock:
            return self._submitted - self._started

    def get_active_count(self) -> int:
        """正在执行的任务数。"""
        with self._lock:
            return self._active

    @property
    def capacity(self) -> int:
        """并发上限（线程数）。"""
        return self._capacity

    def shutdown(self, wait: bool = False) -> None:
        """应用退出时调用，停止接受新任务。"""
        self._executor.shutdown(wait=wait, cancel_futures=not wait)


# 全局单例
async_queue = AsyncQueue()
