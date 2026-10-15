import pytest


def test_run_async_from_thread_closes_coroutine_when_loop_unavailable(monkeypatch):
    from importlib import import_module

    db_engine = import_module("core.db.engine")

    async def operation():
        return None

    coro = operation()
    monkeypatch.setattr(db_engine, "_main_loop", None)

    with pytest.raises(RuntimeError, match="主事件循环未就绪"):
        db_engine.run_async_from_thread(coro)

    assert coro.cr_frame is None
