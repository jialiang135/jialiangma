"""Single-file ingestion rollback and background-task status propagation."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest


def _setup_ingestion(monkeypatch, tmp_path, *, add_documents, existing=None, insert=None):
    import core.kb_tasks as tasks
    import rag.document_loader as loader
    import rag.vector_store as vector_store

    source = tmp_path / "upload.tmp"
    source.write_text("source content", encoding="utf-8")
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    outcome = SimpleNamespace(
        status="ok",
        content="parsed content",
        filepath=str(source),
        filename="report.txt",
        pages=[],
        reason="ok",
        detail="",
    )
    chunking = SimpleNamespace(use_semantic_splitter=False, chunk_size=100, chunk_overlap=0, separators=[])
    operations = {"added": [], "deleted": [], "inserted": [], "updates": []}

    monkeypatch.setattr(
        tasks,
        "settings",
        SimpleNamespace(upload_dir="uploads", resolve_path=lambda _value: upload_dir),
    )
    monkeypatch.setattr(tasks, "resolve_chunking_config", lambda _owner: chunking)
    monkeypatch.setattr(tasks, "_split_loaded_document", lambda *_args: [{"content": "chunk", "page": None}])
    monkeypatch.setattr(tasks, "span", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(loader, "load_document_detailed", lambda *_args: outcome)
    monkeypatch.setattr(tasks, "run_async_from_thread", lambda coro: asyncio.run(coro))

    async def find_existing(owner_id, file_hash, filename):
        assert (owner_id, filename) == (7, "report.txt")
        assert file_hash
        return existing

    async def insert_record(**kwargs):
        operations["inserted"].append(kwargs)
        if insert:
            return insert()
        return 42

    async def update_count(file_id, count):
        operations["updates"].append(("count", file_id, count))

    async def update_hash(file_id, file_hash):
        operations["updates"].append(("hash", file_id, file_hash))

    monkeypatch.setattr(tasks, "find_file_by_hash_or_name", find_existing)
    monkeypatch.setattr(tasks, "insert_file_record", insert_record)
    monkeypatch.setattr(tasks, "update_file_chunk_count", update_count)
    monkeypatch.setattr(tasks, "update_file_hash", update_hash)
    monkeypatch.setattr(
        vector_store,
        "add_documents",
        lambda chunks, metadatas: operations["added"].append((chunks, metadatas))
        or add_documents(chunks, metadatas),
    )
    monkeypatch.setattr(
        vector_store,
        "delete_by_file",
        lambda filename, owner_id: operations["deleted"].append((filename, owner_id)) or 1,
    )
    return tasks, operations, source, upload_dir, outcome


def test_process_file_sync_persists_chunks_and_owner_metadata(monkeypatch, tmp_path):
    tasks, operations, _source, upload_dir, _outcome = _setup_ingestion(
        monkeypatch, tmp_path, add_documents=lambda *_args: None
    )

    result = tasks.process_file_sync(str(tmp_path / "upload.tmp"), "report.txt", 7)
    saved = upload_dir / "report.txt"
    assert result == {"success": True, "filename": "report.txt", "chunks": 1, "file_id": 42}
    assert saved.read_text(encoding="utf-8") == "source content"
    assert operations["added"][0][0] == ["chunk"]
    assert operations["added"][0][1][0]["owner_id"] == 7
    assert operations["added"][0][1][0]["source"] == "report.txt"
    assert operations["inserted"][0]["filepath"] == str(saved)
    assert operations["updates"][0][:3] == ("count", 42, 1)
    assert operations["updates"][1][:2] == ("hash", 42)
    assert operations["deleted"] == []


def test_duplicate_upload_removes_temporary_copy_without_embedding(monkeypatch, tmp_path):
    tasks, operations, _source, upload_dir, _outcome = _setup_ingestion(
        monkeypatch,
        tmp_path,
        existing={"id": 12},
        add_documents=lambda *_args: pytest.fail("duplicate must not embed"),
    )

    result = tasks.process_file_sync(str(tmp_path / "upload.tmp"), "report.txt", 7)
    assert result["success"] is True
    assert result["skipped"] is True
    assert result["existing_id"] == 12
    assert not (upload_dir / "report.txt").exists()
    assert operations["inserted"] == []


def test_embedding_failure_rolls_back_partial_vectors_and_saved_file(monkeypatch, tmp_path):
    def fail_embed(*_args):
        raise RuntimeError("embedding unavailable")

    tasks, operations, _source, upload_dir, _outcome = _setup_ingestion(
        monkeypatch, tmp_path, add_documents=fail_embed
    )

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        tasks.process_file_sync(str(tmp_path / "upload.tmp"), "report.txt", 7)
    assert operations["deleted"] == [("report.txt", 7)]
    assert not (upload_dir / "report.txt").exists()
    assert operations["inserted"] == []


def test_file_record_failure_rolls_back_vectors_and_saved_file(monkeypatch, tmp_path):
    def fail_insert():
        raise RuntimeError("database unavailable")

    tasks, operations, _source, upload_dir, _outcome = _setup_ingestion(
        monkeypatch,
        tmp_path,
        add_documents=lambda *_args: None,
        insert=fail_insert,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        tasks.process_file_sync(str(tmp_path / "upload.tmp"), "report.txt", 7)
    assert len(operations["added"]) == 1
    assert operations["deleted"] == [("report.txt", 7)]
    assert not (upload_dir / "report.txt").exists()


def test_process_file_task_records_success_failure_and_always_removes_upload(tmp_path, monkeypatch):
    import core.kb_tasks as tasks

    calls = []

    async def update(task_id, **kwargs):
        calls.append((task_id, kwargs))

    monkeypatch.setattr(tasks, "update_upload_task", update)
    monkeypatch.setattr(tasks, "run_async_from_thread", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(
        tasks,
        "process_file_sync",
        lambda *_args: {"success": True, "chunks": 3},
    )
    path = tmp_path / "success.tmp"
    path.write_text("x", encoding="utf-8")
    tasks.process_file_task("task-1", str(path), "a.txt", 7)
    assert calls[0] == ("task-1", {"status": "processing", "progress": 30})
    assert calls[1] == ("task-1", {"status": "done", "progress": 100, "chunk_count": 3})
    assert not path.exists()

    calls.clear()
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(tasks, "process_file_sync", lambda *_args: {"success": False, "error": "parse failed"})
    tasks.process_file_task("task-2", str(path), "a.txt", 7)
    assert calls[-1] == (
        "task-2",
        {"status": "failed", "progress": 100, "error": "parse failed"},
    )
    assert not path.exists()


def test_process_file_task_marks_exceptions_failed_and_skips_duplicates(tmp_path, monkeypatch):
    import core.kb_tasks as tasks

    calls = []

    async def update(task_id, **kwargs):
        calls.append((task_id, kwargs))

    monkeypatch.setattr(tasks, "update_upload_task", update)
    monkeypatch.setattr(tasks, "run_async_from_thread", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(
        tasks,
        "process_file_sync",
        lambda *_args: {"success": True, "skipped": True, "message": "already exists"},
    )
    path = tmp_path / "duplicate.tmp"
    path.write_text("x", encoding="utf-8")
    tasks.process_file_task("task-3", str(path), "a.txt", 7)
    assert calls[-1] == (
        "task-3",
        {"status": "skipped", "progress": 100, "error": "already exists"},
    )

    calls.clear()
    path.write_text("x", encoding="utf-8")

    def fail(*_args):
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(tasks, "process_file_sync", fail)
    tasks.process_file_task("task-4", str(path), "a.txt", 7)
    assert calls[-1] == (
        "task-4",
        {"status": "failed", "progress": 100, "error": "worker crashed"},
    )
    assert not path.exists()
