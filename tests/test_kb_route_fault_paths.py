"""Offline failure-path tests for uploads, previews, deletion, and background jobs."""

import asyncio
import io
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from tests.conftest import run_async

ADMIN = {"owner_id": 999, "username": "admin", "role": "admin"}


def _upload(filename, content=b"payload", content_type="application/octet-stream"):
    headers = Headers({"content-type": content_type}) if content_type else Headers()
    return UploadFile(filename=filename, file=io.BytesIO(content), headers=headers)


def _sync_to_thread(func, *args, **kwargs):
    async def run():
        return func(*args, **kwargs)

    return run()


def _local_tempfiles(monkeypatch, routes, tmp_path):
    original = routes.tempfile.NamedTemporaryFile
    monkeypatch.setattr(
        routes.tempfile,
        "NamedTemporaryFile",
        lambda **kwargs: original(dir=tmp_path, **kwargs),
    )


def test_list_files_returns_shared_owner_counts_and_optional_defaults(monkeypatch):
    import api.routes.kb_routes as routes
    import rag.vector_store as vector_store

    seen = []

    async def files(owner_id):
        seen.append(owner_id)
        return [
            {
                "id": 1,
                "owner_id": owner_id,
                "filename": "a.txt",
                "filepath": "/safe/a.txt",
                "created_at": datetime(2025, 1, 1),
            },
            {
                "id": 2,
                "owner_id": owner_id,
                "filename": "b.txt",
                "filepath": "/safe/b.txt",
                "file_size": 10,
                "chunk_count": 3,
                "created_at": datetime(2025, 1, 2),
            },
        ]

    monkeypatch.setattr(routes, "get_files_by_owner", files)
    monkeypatch.setattr(vector_store, "get_collection_stats", lambda owner: {"total_chunks": owner + 2})
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=_sync_to_thread))
    result = run_async(routes.list_files({"owner_id": 999, "role": "user"}))
    assert result.total_files == 2
    assert result.total_chunks == routes._kb_owner(ADMIN) + 2
    assert seen == [routes._kb_owner(ADMIN)]
    assert result.files[0].file_size == 0
    assert result.files[0].chunk_count == 0
    assert result.files[1].file_size == 10
    assert result.files[1].chunk_count == 3


def test_upload_validation_rejects_bad_names_extensions_and_size(monkeypatch):
    import api.routes.kb_routes as routes

    assert routes._validate_upload(_upload(""))[1].startswith("文件名不合法")
    assert routes._validate_upload(_upload("payload.exe"))[1].startswith("不支持的文件格式")

    monkeypatch.setattr(
        routes,
        "settings",
        SimpleNamespace(max_upload_size_mb=0),
    )
    oversized = _upload("large.txt")
    oversized.size = 1
    filename, error = routes._validate_upload(oversized)
    assert filename is None
    assert error == "文件超过 0MB 上限"


def test_mime_mismatch_warns_without_rejecting_and_missing_type_is_quiet(monkeypatch):
    import api.routes.kb_routes as routes

    warnings = []
    monkeypatch.setattr(routes.logger, "warning", lambda *args: warnings.append(args))
    assert routes._validate_upload(_upload("photo.png", content_type="text/plain")) == (
        "photo.png",
        None,
    )
    assert len(warnings) == 1
    assert routes._validate_upload(_upload("notes.txt", content_type=None)) == ("notes.txt", None)
    assert len(warnings) == 1


def test_spool_upload_writes_in_chunks_and_removes_partial_files_on_failure(
    monkeypatch, tmp_path
):
    import api.routes.kb_routes as routes

    _local_tempfiles(monkeypatch, routes, tmp_path)
    path, size = run_async(routes._spool_to_temp(_upload("file.txt", b"abc"), ".txt", 10))
    assert size == 3
    assert Path(path).read_bytes() == b"abc"

    with pytest.raises(HTTPException) as too_large:
        run_async(routes._spool_to_temp(_upload("large.txt", b"12345"), ".txt", 4))
    assert too_large.value.status_code == 413
    assert list(tmp_path.glob("tmp*.txt")) == [Path(path)]

    class BrokenUpload:
        async def read(self, _size):
            raise OSError("client stream failed")

    with pytest.raises(OSError, match="client stream failed"):
        run_async(routes._spool_to_temp(BrokenUpload(), ".bin", 10))
    assert list(tmp_path.iterdir()) == [Path(path)]


def test_clear_upload_dir_skips_missing_dirs_and_continues_after_unlink_error(
    monkeypatch, tmp_path
):
    import api.routes.kb_routes as routes

    routes._clear_upload_dir(tmp_path / "missing")
    retained = tmp_path / "retained.txt"
    removable = tmp_path / "removable.txt"
    nested = tmp_path / "subdir"
    retained.write_text("keep", encoding="utf-8")
    removable.write_text("delete", encoding="utf-8")
    nested.mkdir()
    original_unlink = Path.unlink

    def fail_retained(path, *args, **kwargs):
        if path == retained:
            raise OSError("permission denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_retained)
    routes._clear_upload_dir(tmp_path)
    assert retained.read_text(encoding="utf-8") == "keep"
    assert not removable.exists()
    assert nested.is_dir()


def test_preview_route_success_validation_failure_and_unparseable_file(monkeypatch, tmp_path):
    import api.routes.kb_routes as routes
    import core.kb_tasks as kb_tasks

    current = SimpleNamespace(mode="semantic", chunk_size=800, chunk_overlap=80, separators=None)
    candidate = SimpleNamespace(mode="fixed", chunk_size=400, chunk_overlap=40, separators=None)
    monkeypatch.setattr(routes, "_resolve_readable_file", lambda *_args: _async_value({"path": tmp_path / "doc.txt"}))
    monkeypatch.setattr(routes, "load_chunking_config", lambda _owner: _async_value(current))
    monkeypatch.setattr(routes, "validate_chunking_payload", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(routes, "settings", SimpleNamespace(resolve_path=lambda value: Path(value), upload_dir=str(tmp_path)))
    calls = []

    def preview(path, upload_dir, new_config, saved_config):
        calls.append((path, upload_dir, new_config, saved_config))
        return {"ok": True, "identical": False, "current_count": 1, "candidate_count": 2}

    monkeypatch.setattr(kb_tasks, "preview_split", preview)
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=_sync_to_thread))
    payload = SimpleNamespace(file_id=3, config=None)
    result = run_async(routes.preview_chunking_config(payload, ADMIN))
    assert result == {
        "success": True,
        "data": {"ok": True, "identical": False, "current_count": 1, "candidate_count": 2},
    }
    assert calls[0][1] == str(tmp_path)

    monkeypatch.setattr(
        routes,
        "validate_chunking_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad chunk size")),
    )
    with pytest.raises(HTTPException) as invalid:
        run_async(routes.preview_chunking_config(payload, ADMIN))
    assert invalid.value.status_code == 400

    monkeypatch.setattr(routes, "validate_chunking_payload", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(kb_tasks, "preview_split", lambda *_args: {"ok": False, "reason": "parser failed"})
    with pytest.raises(HTTPException) as failed_parse:
        run_async(routes.preview_chunking_config(payload, ADMIN))
    assert failed_parse.value.status_code == 409
    assert "parser failed" in failed_parse.value.detail


def _async_value(value):
    async def result():
        return value

    return result()


def test_upload_empty_oversized_and_clean_success_paths(monkeypatch, tmp_path):
    import api.routes.kb_routes as routes

    with pytest.raises(HTTPException) as empty:
        run_async(routes.upload_files([], ADMIN))
    assert empty.value.status_code == 400

    _local_tempfiles(monkeypatch, routes, tmp_path)
    monkeypatch.setattr(routes, "_validate_upload", lambda _file: ("large.txt", None))
    monkeypatch.setattr(routes, "settings", SimpleNamespace(max_upload_size_mb=0))
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=_sync_to_thread))
    with pytest.raises(HTTPException) as oversized:
        run_async(routes.upload_files([_upload("large.txt", b"too large")], ADMIN))
    assert oversized.value.status_code == 400
    assert oversized.value.detail["rejected"][0]["reason"] == "文件超过 0MB 上限"
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(routes, "settings", SimpleNamespace(max_upload_size_mb=1))
    monkeypatch.setattr(routes, "compute_file_hash", lambda _path: "new-hash")
    monkeypatch.setattr(routes, "find_file_by_hash_or_name", lambda *_args: _async_value(None))
    monkeypatch.setattr(routes, "create_upload_task", lambda *_args: _async_value(None))

    class Queue:
        def enqueue(self, *_args):
            return None

    monkeypatch.setattr(routes, "async_queue", Queue())
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: "success-uuid")
    success = run_async(routes.upload_files([_upload("valid.txt", b"ok")], ADMIN))
    assert success.data["total_submitted"] == 1
    assert success.data["rejected"] == []
    assert success.data["skipped"] == []
    assert "被拒绝" not in success.message
    assert len(list(tmp_path.iterdir())) == 1


def test_upload_http_exception_after_spooling_cleans_temp_file(monkeypatch, tmp_path):
    import api.routes.kb_routes as routes

    _local_tempfiles(monkeypatch, routes, tmp_path)

    async def reject_hash(_func, *_args, **_kwargs):
        raise HTTPException(status_code=413, detail="upload rejected downstream")

    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=reject_hash))
    with pytest.raises(HTTPException) as error:
        run_async(routes.upload_files([_upload("valid.txt")], ADMIN))
    assert error.value.status_code == 400
    assert error.value.detail["rejected"][0]["reason"] == "upload rejected downstream"
    assert list(tmp_path.iterdir()) == []


def test_upload_spool_failure_without_temp_path_is_reported(monkeypatch):
    import api.routes.kb_routes as routes

    async def fail_spool(*_args):
        raise RuntimeError("disk unavailable")

    monkeypatch.setattr(routes, "_spool_to_temp", fail_spool)
    created = []
    monkeypatch.setattr(routes, "create_upload_task", lambda *_args: created.append(True))
    with pytest.raises(HTTPException) as error:
        run_async(routes.upload_files([_upload("valid.txt")], ADMIN))
    assert error.value.status_code == 400
    assert error.value.detail["rejected"][0]["reason"].startswith("提交失败: disk unavailable")
    assert created == []


def test_upload_hash_failure_cleans_temp_file_without_creating_task(monkeypatch, tmp_path):
    import api.routes.kb_routes as routes

    _local_tempfiles(monkeypatch, routes, tmp_path)
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=_sync_to_thread))
    monkeypatch.setattr(
        routes,
        "compute_file_hash",
        lambda _path: (_ for _ in ()).throw(RuntimeError("disk read failed")),
    )
    creates = []
    monkeypatch.setattr(routes, "create_upload_task", lambda *_args: creates.append(True))

    with pytest.raises(HTTPException) as error:
        run_async(routes.upload_files([_upload("valid.txt")], ADMIN))
    assert error.value.status_code == 400
    assert error.value.detail["rejected"][0]["reason"].startswith("提交失败: disk read failed")
    assert creates == []
    assert list(tmp_path.iterdir()) == []


def test_upload_mixed_success_duplicate_and_invalid_file(monkeypatch, tmp_path):
    import api.routes.kb_routes as routes

    _local_tempfiles(monkeypatch, routes, tmp_path)
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=_sync_to_thread))
    monkeypatch.setattr(routes, "compute_file_hash", lambda _path: "hash")
    monkeypatch.setattr(
        routes,
        "find_file_by_hash_or_name",
        lambda _owner, _hash, name: _async_value({"id": 7} if name == "duplicate.txt" else None),
    )
    created = []
    queued = []

    async def create(task_id, owner_id, filename, size):
        created.append((task_id, owner_id, filename, size))

    class Queue:
        def enqueue(self, *args):
            queued.append(args)

    monkeypatch.setattr(routes, "create_upload_task", create)
    monkeypatch.setattr(routes, "async_queue", Queue())
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: "task-identifier")

    result = run_async(
        routes.upload_files(
            [_upload("duplicate.txt"), _upload("new.txt", b"fresh"), _upload("bad.exe")],
            ADMIN,
        )
    )
    assert result.success is True
    assert result.data["total_submitted"] == 1
    assert len(result.data["task_ids"]) == 1
    assert result.data["skipped"] == ["duplicate.txt（已存在 # 7）"]
    assert result.data["rejected"][0]["filename"] == "bad.exe"
    assert len(created) == 1
    assert created[0][2:] == ("new.txt", 5)
    assert len(queued) == 1
    assert queued[0][0] is routes.process_file_task
    assert Path(queued[0][2]).read_bytes() == b"fresh"
    assert len(list(tmp_path.iterdir())) == 1


def test_upload_all_rejected_and_enqueue_failure_mark_task_failed(monkeypatch, tmp_path):
    import api.routes.kb_routes as routes

    _local_tempfiles(monkeypatch, routes, tmp_path)
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=_sync_to_thread))
    monkeypatch.setattr(routes, "compute_file_hash", lambda _path: "hash")
    monkeypatch.setattr(routes, "find_file_by_hash_or_name", lambda *_args: _async_value(None))
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: "failed-task-id")

    with pytest.raises(HTTPException) as invalid:
        run_async(routes.upload_files([_upload("bad.exe")], ADMIN))
    assert invalid.value.status_code == 400
    assert invalid.value.detail["rejected"][0]["filename"] == "bad.exe"

    async def create(*_args):
        return None

    updates = []

    async def update(task_id, **kwargs):
        updates.append((task_id, kwargs))

    class Queue:
        def enqueue(self, *_args):
            raise RuntimeError("queue offline")

    monkeypatch.setattr(routes, "create_upload_task", create)
    monkeypatch.setattr(routes, "update_upload_task", update)
    monkeypatch.setattr(routes, "async_queue", Queue())
    with pytest.raises(HTTPException) as queue_error:
        run_async(routes.upload_files([_upload("new.txt")], ADMIN))
    assert queue_error.value.status_code == 400
    assert queue_error.value.detail["rejected"][0]["reason"].startswith("提交失败: queue offline")
    assert updates[0][0] == "failed-task-"
    assert updates[0][1]["status"] == "failed"
    assert updates[0][1]["progress"] == 100
    assert list(tmp_path.iterdir()) == []


def test_upload_failure_to_mark_task_failed_is_contained(monkeypatch, tmp_path):
    import api.routes.kb_routes as routes

    _local_tempfiles(monkeypatch, routes, tmp_path)
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=_sync_to_thread))
    monkeypatch.setattr(routes, "compute_file_hash", lambda _path: "hash")
    monkeypatch.setattr(routes, "find_file_by_hash_or_name", lambda *_args: _async_value(None))
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: "failed-task-id")

    async def create(*_args):
        return None

    async def update(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    class Queue:
        def enqueue(self, *_args):
            raise RuntimeError("queue offline")

    monkeypatch.setattr(routes, "create_upload_task", create)
    monkeypatch.setattr(routes, "update_upload_task", update)
    monkeypatch.setattr(routes, "async_queue", Queue())
    with pytest.raises(HTTPException) as error:
        run_async(routes.upload_files([_upload("new.txt")], ADMIN))
    assert error.value.status_code == 400
    assert error.value.detail["rejected"][0]["reason"].startswith("提交失败:")
    assert list(tmp_path.iterdir()) == []


def test_upload_status_not_found_and_rebuild_marker(monkeypatch):
    import api.routes.kb_routes as routes

    monkeypatch.setattr(routes, "get_upload_task", lambda *_args: _async_value(None))
    with pytest.raises(HTTPException) as missing:
        run_async(routes.get_upload_status("missing", ADMIN))
    assert missing.value.status_code == 404

    task = {
        "task_id": "task-1",
        "filename": routes.REBUILD_TASK_FILENAME,
        "status": "processing",
        "progress": 50,
        "chunk_count": 2,
        "error": "",
        "created_at": "now",
    }
    monkeypatch.setattr(routes, "get_upload_task", lambda *_args: _async_value(task))
    result = run_async(routes.get_upload_status("task-1", ADMIN))
    assert result["is_rebuild"] is True
    assert result["progress"] == 50


def test_delete_file_checks_owner_then_cleans_vectors_disk_and_record(monkeypatch, tmp_path):
    import api.routes.kb_routes as routes
    import rag.vector_store as vector_store

    async def missing(_file_id):
        return None

    monkeypatch.setattr(routes, "get_file_by_id", missing)
    with pytest.raises(HTTPException) as not_found:
        run_async(routes.delete_file(1, ADMIN))
    assert not_found.value.status_code == 404

    async def other_owner(_file_id):
        return {"owner_id": 10, "filename": "a.txt", "filepath": str(tmp_path / "a.txt")}

    monkeypatch.setattr(routes, "get_file_by_id", other_owner)
    with pytest.raises(HTTPException) as forbidden:
        run_async(routes.delete_file(1, ADMIN))
    assert forbidden.value.status_code == 403

    calls = []

    async def to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def delete_record(file_id, owner_id):
        calls.append(("record", file_id, owner_id))

    monkeypatch.setattr(routes, "get_file_by_id", lambda _file_id: _async_value({
        "owner_id": routes._kb_owner(ADMIN),
        "filename": "a.txt",
        "filepath": "outside.txt",
    }))
    monkeypatch.setattr(vector_store, "delete_by_file", lambda filename, owner_id: calls.append(("vectors", filename, owner_id)) or 3)
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(routes, "settings", SimpleNamespace(upload_dir=str(tmp_path), resolve_path=lambda value: Path(value)))
    monkeypatch.setattr(routes, "remove_within", lambda root, path: calls.append(("disk", root, path)) or False)
    monkeypatch.setattr(routes, "delete_file_record", delete_record)

    result = run_async(routes.delete_file(8, ADMIN))
    assert result.success is True
    assert "3 个向量块" in result.message
    assert calls[0] == ("vectors", "a.txt", routes._kb_owner(ADMIN))
    assert calls[1] == ("disk", str(tmp_path), "outside.txt")
    assert calls[2] == ("record", 8, routes._kb_owner(ADMIN))


def test_delete_file_preserves_disk_and_record_if_vector_cleanup_fails(monkeypatch):
    import api.routes.kb_routes as routes
    import rag.vector_store as vector_store

    calls = []

    async def record(_file_id):
        return {"owner_id": routes._kb_owner(ADMIN), "filename": "a.txt", "filepath": "a.txt"}

    async def to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def delete_record(*_args):
        calls.append("database")

    def fail_delete(*_args):
        calls.append("vectors")
        raise vector_store.VectorStoreError("vector storage unavailable")

    monkeypatch.setattr(routes, "get_file_by_id", record)
    monkeypatch.setattr(vector_store, "delete_by_file", fail_delete)
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(routes, "delete_file_record", delete_record)
    monkeypatch.setattr(routes, "remove_within", lambda *_args: calls.append("disk"))

    with pytest.raises(vector_store.VectorStoreError, match="storage unavailable"):
        run_async(routes.delete_file(8, ADMIN))
    assert calls == ["vectors"]


def test_clear_knowledge_base_clears_database_uploads_and_vector_cache(monkeypatch, tmp_path):
    import api.routes.kb_routes as routes
    import rag.vector_store as vector_store

    (tmp_path / "old.txt").write_text("old", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    calls = []

    async def to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def delete_files(owner_id):
        calls.append(("files", owner_id))
        return 4

    monkeypatch.setattr(vector_store, "delete_all_by_owner", lambda owner: calls.append(("vectors", owner)) or 9)
    monkeypatch.setattr(vector_store, "reset_vector_store", lambda: calls.append(("reset",)))
    monkeypatch.setattr(routes, "delete_all_file_records", delete_files)
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(routes, "settings", SimpleNamespace(upload_dir=str(tmp_path), resolve_path=lambda value: Path(value)))

    result = run_async(routes.clear_knowledge_base(ADMIN))
    assert result.success is True
    assert result.message == "已清空（9 块, 4 文件）"
    assert calls == [("vectors", routes._kb_owner(ADMIN)), ("files", routes._kb_owner(ADMIN)), ("reset",)]
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "nested").is_dir()


def test_clear_knowledge_base_preserves_files_if_vector_cleanup_fails(monkeypatch, tmp_path):
    import api.routes.kb_routes as routes
    import rag.vector_store as vector_store

    calls = []

    async def to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    def fail_delete(_owner_id):
        calls.append("vectors")
        raise vector_store.VectorStoreError("vector storage unavailable")

    async def delete_files(_owner_id):
        calls.append("database")
        return 0

    monkeypatch.setattr(vector_store, "delete_all_by_owner", fail_delete)
    monkeypatch.setattr(vector_store, "reset_vector_store", lambda: calls.append("reset"))
    monkeypatch.setattr(routes, "delete_all_file_records", delete_files)
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(
        routes,
        "settings",
        SimpleNamespace(upload_dir=str(tmp_path), resolve_path=lambda value: Path(value)),
    )
    with pytest.raises(vector_store.VectorStoreError, match="storage unavailable"):
        run_async(routes.clear_knowledge_base(ADMIN))
    assert calls == ["vectors"]


def test_rebuild_job_success_and_enqueue_failure(monkeypatch):
    import api.routes.kb_routes as routes

    created = []
    updates = []

    async def create(task_id, owner_id, filename, size):
        created.append((task_id, owner_id, filename, size))

    async def update(task_id, **kwargs):
        updates.append((task_id, kwargs))

    class Queue:
        def __init__(self, fail=False):
            self.fail = fail
            self.calls = []

        def enqueue(self, *args):
            self.calls.append(args)
            if self.fail:
                raise RuntimeError("queue offline")

    monkeypatch.setattr(routes, "create_upload_task", create)
    monkeypatch.setattr(routes, "update_upload_task", update)
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: "rebuild-task-id")
    queue = Queue()
    monkeypatch.setattr(routes, "async_queue", queue)
    result = run_async(routes.rebuild_knowledge_base(ADMIN))
    assert result.success is True
    assert result.data == {"task_id": "rebuild-task"}
    assert created == [("rebuild-task", routes._kb_owner(ADMIN), routes.REBUILD_TASK_FILENAME, 0)]
    assert queue.calls[0][0] is routes.rebuild_task

    queue = Queue(fail=True)
    monkeypatch.setattr(routes, "async_queue", queue)
    with pytest.raises(HTTPException) as error:
        run_async(routes.rebuild_knowledge_base(ADMIN))
    assert error.value.status_code == 503
    assert updates[-1][0] == "rebuild-task"
    assert updates[-1][1]["status"] == "failed"
