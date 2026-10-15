"""BM25 惰性索引的并发构建与缓存失效回归测试。"""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock


def test_concurrent_first_queries_build_index_once():
    from rag.bm25_search import BM25IndexManager

    manager = BM25IndexManager()
    worker_count = 5
    start = Barrier(worker_count + 1)
    build_started = Event()
    release_build = Event()
    calls = 0
    calls_lock = Lock()
    fake_index = object()
    documents = [{"content": "sample"}]

    def fake_build(_owner_id):
        nonlocal calls
        with calls_lock:
            calls += 1
        build_started.set()
        assert release_build.wait(timeout=3)
        return fake_index, documents

    manager.build_index = fake_build

    def get_index():
        start.wait(timeout=3)
        return manager.get_index(7)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(get_index) for _ in range(worker_count)]
        start.wait(timeout=3)
        assert build_started.wait(timeout=3)
        # Keep the first build in flight while the other requests contend for it.
        time.sleep(0.05)
        release_build.set()
        results = [future.result(timeout=3) for future in futures]

    assert calls == 1
    assert all(index is fake_index and docs is documents for index, docs in results)


def test_invalidation_during_build_discards_stale_index():
    from rag.bm25_search import BM25IndexManager

    manager = BM25IndexManager()
    first_build_started = Event()
    release_first_build = Event()
    calls = 0
    old_index = object()
    fresh_index = object()
    documents = [{"content": "sample"}]

    def fake_build(_owner_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_build_started.set()
            assert release_first_build.wait(timeout=3)
            return old_index, documents
        return fresh_index, documents

    manager.build_index = fake_build

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.get_index, 7)
        assert first_build_started.wait(timeout=3)
        manager.invalidate(7)
        release_first_build.set()
        index, docs = future.result(timeout=3)

    assert calls == 2
    assert index is fresh_index
    assert docs is documents
    assert manager._indices[7][0] is fresh_index
