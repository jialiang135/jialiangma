"""
静默失效回归测试
================

覆盖四类"出错了但用户与日志都看不出"的问题：

1. 重建向量索引时全部解析失败却被报成成功（任务状态语义）；
2. OCR 失败原因不可分辨（引擎缺失 / 没识别出文字 / 文件本来就空）；
3. ZIP 解压无上限（压缩炸弹）；
4. 链路追踪未配导出端时 span 被静默丢弃。

全部离线：不调用 LLM / Embedding / 真实 OCR 引擎。
"""

import zipfile

import pytest

# ============================================================
# 工具
# ============================================================


def _outcome(
    filepath: str,
    filename: str,
    status: str,
    reason: str = "ok",
    detail: str = "",
    content: str = "",
):
    from rag.document_loader import LoadOutcome

    return LoadOutcome(
        filepath, filename, content=content, status=status, reason=reason, detail=detail
    )


def _fake_batch(docs, **kwargs):
    """分块桩：每个文档产出一个块（内容即 chunk）。"""
    return [
        {"filepath": d["filepath"], "filename": d["filename"], "chunks": [d["content"]]}
        for d in docs
    ]


def _tesseract_available() -> bool:
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


# ============================================================
# 1. 重建任务状态语义（纯函数）
# ============================================================


class TestRebuildStatusSummary:
    @staticmethod
    def _report(**kw):
        base = {
            "total_chunks": 0,
            "total_files": 0,
            "succeeded_files": 0,
            "failed_files": [],
            "empty_files": [],
        }
        base.update(kw)
        return base

    def test_no_files_is_done(self):
        """无文件 → done（空知识库是合法状态，不是失败）。"""
        from core.kb_tasks import _summarize_rebuild

        status, error = _summarize_rebuild(self._report())
        assert status == "done"
        assert error == ""

    def test_all_success_is_done(self):
        from core.kb_tasks import _summarize_rebuild

        status, error = _summarize_rebuild(
            self._report(total_files=3, succeeded_files=3, total_chunks=42)
        )
        assert status == "done"
        assert error == ""

    def test_partial_when_some_files_failed(self):
        """部分失败 → 不能是干净的 done；error 里要看得出是哪个文件、为什么。"""
        from core.kb_tasks import _summarize_rebuild

        status, error = _summarize_rebuild(
            self._report(
                total_files=3,
                succeeded_files=2,
                total_chunks=10,
                failed_files=[
                    {"filename": "a.png", "reason": "ocr_engine_missing", "detail": "缺少 OCR 引擎"}
                ],
            )
        )
        assert status == "partial"
        assert "a.png" in error
        assert "缺少 OCR 引擎" in error

    def test_all_failed_is_failed(self):
        from core.kb_tasks import _summarize_rebuild

        status, error = _summarize_rebuild(
            self._report(
                total_files=2,
                succeeded_files=0,
                total_chunks=0,
                failed_files=[
                    {
                        "filename": "a.png",
                        "reason": "ocr_engine_missing",
                        "detail": "缺少 OCR 引擎",
                    },
                    {"filename": "b.pdf", "reason": "parse_error", "detail": "boom"},
                ],
            )
        )
        assert status == "failed"
        assert "a.png" in error
        assert "b.pdf" in error
        assert "boom" in error

    def test_all_empty_with_files_is_failed(self):
        """有文件但一块都没入库（全部为空）→ failed，而不是"完成 0 块"。"""
        from core.kb_tasks import _summarize_rebuild

        status, error = _summarize_rebuild(
            self._report(
                total_files=1,
                succeeded_files=0,
                total_chunks=0,
                empty_files=[
                    {"filename": "scan.pdf", "reason": "empty_content", "detail": "无文字层"}
                ],
            )
        )
        assert status == "failed"
        assert "scan.pdf" in error

    def test_empty_and_failed_mixed_all_zero_chunks_is_failed(self):
        from core.kb_tasks import _summarize_rebuild

        status, _ = _summarize_rebuild(
            self._report(
                total_files=2,
                succeeded_files=0,
                total_chunks=0,
                failed_files=[{"filename": "a.png", "reason": "ocr_engine_missing", "detail": ""}],
                empty_files=[{"filename": "b.txt", "reason": "empty_content", "detail": ""}],
            )
        )
        assert status == "failed"


# ============================================================
# 1b. 重建循环：逐文件分类（离线，全部桩替换）
# ============================================================


class TestRebuildReportLoop:
    def _patch_common(self, monkeypatch, files, outcomes, add_documents=None):
        import pathlib

        import core.kb_tasks as kb
        import rag.document_loader as dl
        import rag.text_splitter as ts
        import rag.vector_store as vs

        # 让记录里的文件**真实存在**：重建现在有预检（文件找不到就中止、不动旧
        # 索引，见 TestRebuildPreflight）。这些用例要验的是"分类逻辑"，所以先
        # 过预检。注意：它们**不能**再靠"路径不存在也照常重建"这个旧行为。
        for record in files:
            pathlib.Path(record["filepath"]).touch()

        monkeypatch.setattr(kb, "get_files_by_owner", lambda owner_id: files)
        monkeypatch.setattr(kb, "update_file_chunk_count", lambda *a, **k: None)
        monkeypatch.setattr(kb, "run_async_from_thread", lambda coro, *a, **k: coro)
        monkeypatch.setattr(dl, "load_document_detailed", lambda fp, upload_dir="": outcomes[fp])
        monkeypatch.setattr(ts, "process_documents_batch", _fake_batch)
        monkeypatch.setattr(vs, "delete_all_by_owner", lambda owner_id: None)
        monkeypatch.setattr(vs, "reset_vector_store", lambda: None)
        monkeypatch.setattr(vs, "add_documents", add_documents or (lambda chunks, metas: None))

    def test_classifies_ok_failed_empty(self, monkeypatch, tmp_path):
        import core.kb_tasks as kb

        ok = str(tmp_path / "ok.txt")
        img = str(tmp_path / "img.png")
        scan = str(tmp_path / "scan.pdf")
        files = [
            {"id": 1, "filepath": ok, "filename": "ok.txt"},
            {"id": 2, "filepath": img, "filename": "img.png"},
            {"id": 3, "filepath": scan, "filename": "scan.pdf"},
        ]
        outcomes = {
            ok: _outcome(ok, "ok.txt", "ok", content="hello world"),
            img: _outcome(
                img, "img.png", "failed", reason="ocr_engine_missing", detail="缺少 OCR 引擎"
            ),
            scan: _outcome(
                scan, "scan.pdf", "empty", reason="empty_content", detail="扫描件无文字层"
            ),
        }
        added = []
        self._patch_common(
            monkeypatch, files, outcomes, add_documents=lambda c, m: added.append((c, m))
        )

        report = kb._rebuild_knowledge_base_report(1)

        assert report["total_files"] == 3
        assert report["succeeded_files"] == 1
        assert report["total_chunks"] == 1
        assert [f["filename"] for f in report["failed_files"]] == ["img.png"]
        assert [f["filename"] for f in report["empty_files"]] == ["scan.pdf"]
        assert len(added) == 1  # 只有成功的那个文件进了向量库

        status, error = kb._summarize_rebuild(report)
        assert status == "partial"
        assert "img.png" in error
        assert "scan.pdf" in error

    def test_all_failed_yields_failed_status(self, monkeypatch, tmp_path):
        import core.kb_tasks as kb

        a = str(tmp_path / "a.png")
        b = str(tmp_path / "b.pdf")
        files = [
            {"id": 1, "filepath": a, "filename": "a.png"},
            {"id": 2, "filepath": b, "filename": "b.pdf"},
        ]
        outcomes = {
            a: _outcome(a, "a.png", "failed", reason="ocr_engine_missing", detail="缺少 OCR 引擎"),
            b: _outcome(b, "b.pdf", "failed", reason="parse_error", detail="损坏"),
        }
        self._patch_common(monkeypatch, files, outcomes)

        report = kb._rebuild_knowledge_base_report(1)
        assert report["total_chunks"] == 0
        status, error = kb._summarize_rebuild(report)
        assert status == "failed"
        assert "a.png" in error and "b.pdf" in error

    def test_embedding_failure_is_not_silent(self, monkeypatch, tmp_path):
        """向量化抛错必须被记为 failed（embed_error），而不是静默算成功。"""
        import core.kb_tasks as kb

        ok = str(tmp_path / "ok.txt")
        files = [{"id": 1, "filepath": ok, "filename": "ok.txt"}]
        outcomes = {ok: _outcome(ok, "ok.txt", "ok", content="hello")}

        def boom(chunks, metas):
            raise RuntimeError("DashScope 挂了")

        self._patch_common(monkeypatch, files, outcomes, add_documents=boom)

        report = kb._rebuild_knowledge_base_report(1)
        assert report["total_chunks"] == 0
        assert report["failed_files"][0]["reason"] == "embed_error"
        status, _ = kb._summarize_rebuild(report)
        assert status == "failed"


# ============================================================
# 1c. rebuild_task 写回的状态（端到端，桩替换入口）
# ============================================================


class TestRebuildTaskWritesStatus:
    def _run(self, monkeypatch, report):
        import core.kb_tasks as kb

        recorded = []
        monkeypatch.setattr(kb, "update_upload_task", lambda task_id, **f: recorded.append(f))
        monkeypatch.setattr(kb, "run_async_from_thread", lambda coro, *a, **k: coro)
        monkeypatch.setattr(kb, "_rebuild_knowledge_base_report", lambda owner_id: report)
        kb.rebuild_task("task-1", 1)
        return recorded[-1]

    def test_partial_not_written_as_done(self, monkeypatch):
        final = self._run(
            monkeypatch,
            {
                "total_chunks": 5,
                "total_files": 2,
                "succeeded_files": 1,
                "failed_files": [
                    {"filename": "a.png", "reason": "ocr_engine_missing", "detail": "缺少 OCR 引擎"}
                ],
                "empty_files": [],
            },
        )
        assert final["status"] == "partial"
        assert final["chunk_count"] == 5
        assert "a.png" in final["error"]

    def test_all_failed_written_as_failed(self, monkeypatch):
        final = self._run(
            monkeypatch,
            {
                "total_chunks": 0,
                "total_files": 1,
                "succeeded_files": 0,
                "failed_files": [
                    {"filename": "a.png", "reason": "ocr_engine_missing", "detail": "缺少 OCR 引擎"}
                ],
                "empty_files": [],
            },
        )
        assert final["status"] == "failed"
        assert "a.png" in final["error"]

    def test_all_ok_written_as_done(self, monkeypatch):
        final = self._run(
            monkeypatch,
            {
                "total_chunks": 12,
                "total_files": 2,
                "succeeded_files": 2,
                "failed_files": [],
                "empty_files": [],
            },
        )
        assert final["status"] == "done"
        assert final["error"] == ""
        assert final["chunk_count"] == 12


# ============================================================
# 2. OCR / 加载失败原因可分辨
# ============================================================


class TestLoadFailureReasons:
    def test_missing_ocr_engine_is_reported(self, tmp_path):
        """本机没装 Tesseract —— 真实构造"引擎缺失"这条路径。"""
        pytest.importorskip("PIL")
        pytest.importorskip("pytesseract")
        if _tesseract_available():
            pytest.skip("本机装了 Tesseract，无法构造'引擎缺失'路径")

        from PIL import Image

        from rag.document_loader import load_document_detailed

        p = tmp_path / "img.png"
        Image.new("RGB", (16, 16), "white").save(p)

        outcome = load_document_detailed(str(p))
        assert outcome.status == "failed"
        assert outcome.reason == "ocr_engine_missing"
        assert "OCR" in outcome.detail

    def test_ocr_ran_but_no_text_is_empty_not_failed(self, tmp_path, monkeypatch):
        """引擎"在"但没识别出文字 → empty / ocr_no_text（与引擎缺失区分开）。"""
        pytest.importorskip("PIL")
        pytesseract = pytest.importorskip("pytesseract")

        from PIL import Image

        from rag.document_loader import load_document_detailed

        p = tmp_path / "img.png"
        Image.new("RGB", (16, 16), "white").save(p)
        monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "   ")

        outcome = load_document_detailed(str(p))
        assert outcome.status == "empty"
        assert outcome.reason == "ocr_no_text"
        assert "未识别出文字" in outcome.detail

    def test_empty_text_file_is_empty_content(self, tmp_path):
        from rag.document_loader import load_document_detailed

        p = tmp_path / "empty.txt"
        p.write_text("", encoding="utf-8")

        outcome = load_document_detailed(str(p))
        assert outcome.status == "empty"
        assert outcome.reason == "empty_content"
        assert "空" in outcome.detail

    def test_missing_file_is_failed(self, tmp_path):
        from rag.document_loader import load_document_detailed

        outcome = load_document_detailed(str(tmp_path / "nope.pdf"))
        assert outcome.status == "failed"
        assert outcome.reason == "file_missing"

    def test_unsupported_format_is_failed(self, tmp_path):
        from rag.document_loader import load_document_detailed

        p = tmp_path / "x.xyz"
        p.write_text("hi", encoding="utf-8")

        outcome = load_document_detailed(str(p))
        assert outcome.status == "failed"
        assert outcome.reason == "unsupported_format"

    def test_unsupported_format_still_raises_valueerror(self):
        """历史行为：load_single_document 对不支持格式抛 ValueError。"""
        from rag.document_loader import load_single_document

        with pytest.raises(ValueError, match="不支持的文件格式"):
            load_single_document("/tmp/definitely.xyz")

    def test_batch_loader_keeps_only_ok_documents(self, tmp_path):
        """load_documents_from_paths 对外行为不变：只返回成功且有内容的。"""
        from rag.document_loader import load_documents_from_paths

        good = tmp_path / "good.txt"
        good.write_text("有内容", encoding="utf-8")
        empty = tmp_path / "empty.txt"
        empty.write_text("", encoding="utf-8")

        docs = load_documents_from_paths([str(good), str(empty), str(tmp_path / "gone.txt")])
        assert len(docs) == 1
        assert docs[0]["filename"] == "good.txt"


# ============================================================
# 3. ZIP 解压上限（压缩炸弹）
# ============================================================


class TestZipLimits:
    @staticmethod
    def _zip_with(path, entries, compress=zipfile.ZIP_DEFLATED):
        with zipfile.ZipFile(path, "w", compress) as zf:
            for name, data in entries:
                zf.writestr(name, data)

    def test_zip_bomb_rejected_by_decompressed_size(self, tmp_path, monkeypatch):
        """声明解压后 25MB（压缩后极小）、上限 20MB → 拒绝。"""
        from config.settings import settings
        from rag.document_loader import ZipLimitExceededError, load_zip

        monkeypatch.setattr(settings, "max_upload_size_mb", 1)  # 1MB × 20 = 20MB 上限
        p = tmp_path / "bomb.zip"
        # 全是 0 的内容压缩后只有几十 KB，但声明大小 25MB —— 典型压缩炸弹特征
        self._zip_with(p, [("big.txt", b"\0" * (25 * 1024 * 1024))])

        with pytest.raises(ZipLimitExceededError):
            load_zip(str(p), str(tmp_path))

    def test_zip_bomb_reaches_task_status_as_failure(self, tmp_path, monkeypatch):
        from config.settings import settings
        from rag.document_loader import load_document_detailed

        monkeypatch.setattr(settings, "max_upload_size_mb", 1)
        p = tmp_path / "bomb.zip"
        self._zip_with(p, [("big.txt", b"\0" * (25 * 1024 * 1024))])

        outcome = load_document_detailed(str(p))
        assert outcome.status == "failed"
        assert outcome.reason == "zip_limit_exceeded"

    def test_too_many_members_rejected(self, tmp_path, monkeypatch):
        import rag.document_loader as dl

        monkeypatch.setattr(dl, "ZIP_MAX_MEMBERS", 3)
        p = tmp_path / "many.zip"
        self._zip_with(p, [(f"f{i}.txt", b"x") for i in range(5)])

        with pytest.raises(dl.ZipLimitExceededError):
            dl.load_zip(str(p), str(tmp_path))

    def test_normal_zip_is_accepted(self, tmp_path):
        """正常的小压缩包不能被误杀。"""
        from rag.document_loader import load_zip

        p = tmp_path / "ok.zip"
        self._zip_with(p, [("a.txt", "你好世界".encode())])

        texts = load_zip(str(p), str(tmp_path))
        assert texts
        assert "你好世界" in texts[0]

    def test_limits_derive_from_upload_setting(self, monkeypatch):
        from config.settings import settings
        from rag.document_loader import ZIP_MAX_EXPANSION_FACTOR, _zip_limits

        monkeypatch.setattr(settings, "max_upload_size_mb", 10)
        max_total, max_members = _zip_limits()
        assert max_total == 10 * 1024 * 1024 * ZIP_MAX_EXPANSION_FACTOR
        assert max_members > 0


# ============================================================
# 4. telemetry：未配导出端时的默认行为
# ============================================================


class TestTelemetryExportPolicy:
    def test_endpoint_selects_otlp(self):
        from core.telemetry import _resolve_export_mode

        assert _resolve_export_mode("http://collector:4318", "") == "otlp"

    def test_default_is_console_never_silent(self):
        """未配导出端时**默认降级为控制台导出**，不允许静默丢弃 span。"""
        from core.telemetry import _resolve_export_mode

        assert _resolve_export_mode("", "") == "console"  # 未设置
        assert _resolve_export_mode("", "true") == "console"
        assert _resolve_export_mode("", "1") == "console"

    def test_only_explicit_disable_yields_none(self):
        from core.telemetry import _resolve_export_mode

        for value in ("0", "false", "FALSE", "no", "off"):
            assert _resolve_export_mode("", value) == "none"

    def test_setup_defaults_to_console_mode(self, monkeypatch):
        """强制重新初始化后，默认模式的 span 有落点（不是 none）。"""
        import core.telemetry as t

        if not t._OTEL_AVAILABLE:
            pytest.skip("本机未安装 OpenTelemetry SDK")

        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_CONSOLE_EXPORT", raising=False)
        monkeypatch.setattr(t, "_tracer", None)  # 绕过幂等短路，重新走一遍决策

        assert t.setup_telemetry() is True
        assert t._export_mode == "console"

    def test_configured_endpoint_never_silently_drops(self, monkeypatch):
        """配了 OTLP 端点、但 exporter 不可用时也必须降级（不能变成 none）。"""
        import core.telemetry as t

        if not t._OTEL_AVAILABLE:
            pytest.skip("本机未安装 OpenTelemetry SDK")

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
        monkeypatch.setattr(t, "_tracer", None)

        assert t.setup_telemetry() is True
        assert t._export_mode != "none"


# ============================================================
# 2. 重建预检：文件找不到时，不许先删旧索引
# ============================================================


class TestRebuildPreflight:
    """
    重建是"先删后建"，而删除不可逆 —— 所以必须在删之前确认文件都读得到。

    实测事故：14 条文件记录里 9 条的 ``filepath`` 指向**另一个机器上的绝对路径**
    （数据库是从开发机整体搬过来的），而重建不预检：旧索引被清掉后只建回 5 个
    文件，289 块剩 164 块。任务状态只是个 "partial"，不细看根本发现不了。

    这个用例盯住"不许先删"，它比"重建成功"重要得多。
    """

    def _setup(self, monkeypatch, tmp_path, *, exists: bool):
        """替换重建函数的外部依赖，返回 (kb_tasks, 被要求删除的 owner 列表)。"""
        import asyncio

        import core.kb_tasks as kt
        import rag.vector_store as vs

        path = tmp_path / "kb.md"
        if exists:
            path.write_text("内容", encoding="utf-8")

        async def _fake_get_files(_owner_id):
            return [{"id": 1, "filename": "kb.md", "filepath": str(path)}]

        monkeypatch.setattr(kt, "get_files_by_owner", _fake_get_files)
        monkeypatch.setattr(kt, "run_async_from_thread", asyncio.run)

        deleted: list[int] = []
        monkeypatch.setattr(vs, "delete_all_by_owner", lambda oid: deleted.append(oid) or 0)
        monkeypatch.setattr(vs, "reset_vector_store", lambda: None)
        return kt, deleted

    def test_missing_file_aborts_and_leaves_index_alone(self, monkeypatch, tmp_path):
        kt, deleted = self._setup(monkeypatch, tmp_path, exists=False)

        report = kt._rebuild_knowledge_base_report(1)

        assert report.get("aborted") is True
        assert report["total_chunks"] == 0
        assert deleted == [], "源文件找不到时绝不能先删旧索引 —— 删除不可逆"

        status, error = kt._summarize_rebuild(report)
        assert status == "failed"
        assert "已中止" in error and "未改动" in error, error
        assert "kb.md" in error, f"失败原因里必须点名是哪个文件: {error}"

    def test_all_files_present_still_rebuilds(self, monkeypatch, tmp_path):
        """反向保护：别把预检做成一票否决 —— 文件都在时必须照常删除并重建。"""
        import types

        import core.kb_tasks as kt
        import rag.document_loader as dl
        import rag.vector_store as vs

        kt2, deleted = self._setup(monkeypatch, tmp_path, exists=True)

        monkeypatch.setattr(
            dl,
            "load_document_detailed",
            lambda *a, **k: types.SimpleNamespace(status="ok"),
        )
        monkeypatch.setattr(
            kt2, "_split_loaded_document", lambda outcome, semantic: [{"content": "内容"}]
        )
        monkeypatch.setattr(kt2, "_chunk_metadatas", lambda chunks, *a: [{"owner_id": 1}])
        monkeypatch.setattr(vs, "add_documents", lambda chunks, metas: ["id"])

        async def _noop(*_a, **_k):
            return None

        monkeypatch.setattr(kt2, "update_file_chunk_count", _noop)

        report = kt2._rebuild_knowledge_base_report(1)

        assert not report.get("aborted"), "文件都在却中止了，预检写得太严"
        assert deleted == [1], "文件都在时必须照常清旧索引，否则会留下重复内容"
        assert report["succeeded_files"] == 1
