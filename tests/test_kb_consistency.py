"""Consistency checks distinguish missing data from unavailable storage."""

import asyncio

import pytest


def _patch_consistency_inputs(monkeypatch, files, stats):
    import core.kb_tasks as tasks

    async def get_files(owner_id):
        assert owner_id == 7
        return files

    monkeypatch.setattr(tasks, "get_files_by_owner", get_files)
    monkeypatch.setattr(tasks, "run_async_from_thread", asyncio.run)
    monkeypatch.setattr("rag.vector_store.get_collection_stats", lambda owner_id: stats)
    return tasks


def test_consistency_reports_orphans_and_missing_vectors(monkeypatch):
    tasks = _patch_consistency_inputs(
        monkeypatch,
        [
            {"filename": "tracked.md"},
            {"filename": "missing-vectors.md"},
            {"filename": ""},
        ],
        {"total_chunks": 4, "files": ["tracked.md", "orphan.md"], "error": None},
    )

    report = tasks.check_kb_consistency(7)
    assert report == {
        "owner_id": 7,
        "consistent": False,
        "error": None,
        "db_file_count": 2,
        "vector_chunk_count": 4,
        "vector_source_count": 2,
        "orphan_sources": ["orphan.md"],
        "sources_without_chunks": ["missing-vectors.md"],
    }


def test_consistent_records_produce_clean_report(monkeypatch):
    tasks = _patch_consistency_inputs(
        monkeypatch,
        [{"filename": "a.md"}, {"filename": "b.md"}],
        {"total_chunks": 5, "files": ["a.md", "b.md"], "error": None},
    )
    report = tasks.check_kb_consistency(7)
    assert report["consistent"] is True
    assert report["orphan_sources"] == []
    assert report["sources_without_chunks"] == []
    assert report["error"] is None


def test_repair_removes_only_orphans_and_preserves_database_only_files(monkeypatch):
    import rag.vector_store as vector_store

    tasks = _patch_consistency_inputs(
        monkeypatch,
        [{"filename": "tracked.md"}, {"filename": "missing-vectors.md"}],
        {"total_chunks": 3, "files": ["tracked.md", "orphan.md"], "error": None},
    )
    deleted = []
    monkeypatch.setattr(
        vector_store,
        "delete_by_file",
        lambda source, owner_id: deleted.append((source, owner_id)) or 2,
    )

    result = tasks.repair_kb_consistency(7)
    assert deleted == [("orphan.md", 7)]
    assert result["removed_sources"] == {"orphan.md": 2}
    assert result["removed_chunks"] == 2
    assert result["before"]["sources_without_chunks"] == ["missing-vectors.md"]


def test_storage_failure_is_not_reported_as_missing_vectors_or_repaired(monkeypatch):
    import rag.vector_store as vector_store

    tasks = _patch_consistency_inputs(
        monkeypatch,
        [{"filename": "tracked.md"}],
        {
            "total_chunks": 0,
            "files": [],
            "error": "获取向量库统计失败: storage offline",
        },
    )
    deleted = []
    monkeypatch.setattr(
        vector_store,
        "delete_by_file",
        lambda *args: deleted.append(args),
    )

    report = tasks.check_kb_consistency(7)
    assert report["consistent"] is False
    assert report["error"] == "获取向量库统计失败: storage offline"
    with pytest.raises(RuntimeError, match="未执行修复"):
        tasks.repair_kb_consistency(7)
    assert deleted == []
