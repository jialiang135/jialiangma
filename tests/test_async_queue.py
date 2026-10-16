"""Thread-pool queue submission, counters, shutdown, and failure behavior."""

import threading
from types import SimpleNamespace

import pytest


def _new_queue(monkeypatch, workers=1):
    import core.async_queue as queue_module

    monkeypatch.setattr(queue_module, "settings", SimpleNamespace(worker_threads=workers))
    return queue_module.AsyncQueue()


def test_queue_runs_work_with_bounded_capacity_and_tracks_waiting_tasks(monkeypatch):
    queue = _new_queue(monkeypatch, workers=1)
    started = threading.Event()
    release = threading.Event()
    completed = []

    def first(value):
        started.set()
        assert release.wait(timeout=3)
        completed.append(value)

    first_id = queue.enqueue(first, "first")
    assert started.wait(timeout=1)
    assert queue.get_active_count() == 1

    second_id = queue.enqueue(completed.append, "second")
    assert first_id == "task_1"
    assert second_id == "task_2"
    assert queue.get_queue_size() == 1
    assert queue.capacity == 1

    release.set()
    queue.shutdown(wait=True)
    assert completed == ["first", "second"]
    assert queue.get_queue_size() == 0
    assert queue.get_active_count() == 0


def test_queue_recovers_counters_and_reports_closed_submission(monkeypatch):
    from core.async_queue import QueueUnavailableError

    queue = _new_queue(monkeypatch)
    queue.shutdown(wait=True)
    with pytest.raises(QueueUnavailableError, match="cannot schedule new futures after shutdown"):
        queue.enqueue(lambda: None)
    assert queue.get_queue_size() == 0


def test_fast_task_cannot_make_queue_depth_negative(monkeypatch):
    queue = _new_queue(monkeypatch)
    original_executor = queue._executor
    observed_depths = []

    class InlineExecutor:
        def submit(self, function, *args, **kwargs):
            function(*args, **kwargs)
            observed_depths.append(queue.get_queue_size())

    queue._executor = InlineExecutor()
    try:
        assert queue.enqueue(lambda: None) == "task_1"
        assert observed_depths == [0]
        assert queue.get_queue_size() == 0
    finally:
        original_executor.shutdown(wait=True)


def test_callable_objects_are_logged_and_executed(monkeypatch):
    queue = _new_queue(monkeypatch)
    completed = threading.Event()

    class Task:
        def __call__(self):
            completed.set()

    assert queue.enqueue(Task()) == "task_1"
    queue.shutdown(wait=True)
    assert completed.is_set()


def test_worker_exception_does_not_leave_active_count_stuck(monkeypatch):
    queue = _new_queue(monkeypatch)
    started = threading.Event()

    def fail():
        started.set()
        raise ValueError("task failure")

    queue.enqueue(fail)
    assert started.wait(timeout=1)
    queue.shutdown(wait=True)
    assert queue.get_active_count() == 0
    assert queue.get_queue_size() == 0


def test_nonpositive_configured_worker_count_still_has_one_worker(monkeypatch):
    queue = _new_queue(monkeypatch, workers=0)
    assert queue.capacity == 1
    queue.shutdown(wait=True)
