"""
检索质量改进测试 —— 中文分词 + PDF 页码回溯
============================================

覆盖本次两处检索质量短板的修复，**全部离线**（不联网、不调 DashScope /
真实向量库；PDF 用 PyMuPDF 在内存里现造）：

1. **中文分词从"逐字 unigram"改为按词切分（jieba）**
   - jieba 可用：按词切分，「人工智能」是**一个词**而不是四个字（核心收益）；
   - jieba 不可用：降级到 unigram+bigram，且**不崩、有日志**；
   - 两条路径都要能跑，且英文/数字仍按整词提取、统一小写。
2. **PDF 页码一路带到检索结果**
   - ``load_pdf_pages`` / ``load_document_detailed`` 保留逐页文本；
   - 按页切分后每个 chunk 带正确页码（物理页码 1-based）；
   - ``retrieve()`` 组装出的 documents 新增 ``page``，且**旧字段一个不少**。
"""

import pytest

# ============================================================
# 工具
# ============================================================

#: 每个中文 token 都必须是"词"（长度≥2）的样例词。
#: 这些都是 jieba 词典里的**已知整词**（「机器学习」是少数默认被切成"机器/学习"
#: 的领域词，测试里用 ``suggest_freq`` 显式声明为词 —— 也是 jieba 的惯用法）。
_MULTI_CHAR_TERMS = ["人工智能", "知识库", "机器学习"]


#: reportlab 内置的中文 CID 字体（无需系统字体文件，随包提供度量）。
#: 之所以不用 PyMuPDF 造 PDF：实测 PyMuPDF 写出的 CJK **子集**字体，
#: pypdf 的 ToUnicode 解析取不回汉字（得到乱码），而生产代码正是用 pypdf。
_CJK_FONT = "STSong-Light"


def _can_make_cjk_pdf() -> bool:
    """探测 reportlab 是否能生成"pypdf 能取回中文"的 PDF（不同环境兜底）。"""
    try:
        import tempfile
        from pathlib import Path

        from pypdf import PdfReader
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas

        pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))
        tmp = Path(tempfile.mkdtemp()) / "probe.pdf"
        c = canvas.Canvas(str(tmp))
        c.setFont(_CJK_FONT, 14)
        c.drawString(72, 700, "中文")
        c.showPage()
        c.save()
        text = PdfReader(str(tmp)).pages[0].extract_text() or ""
        return "中文" in text
    except Exception:
        return False


def _make_pdf(pages: list[str]) -> bytes:
    """
    在内存里造一个多页 PDF（每页一行给定文字），返回字节。

    用 reportlab + 内置 CID 中文字体（``STSong-Light``）：汉字能被 **pypdf**
    正确提取 —— 这正是被测试的生产读取路径（见 ``rag/document_loader.load_pdf_pages``）。
    """
    pytest.importorskip("reportlab")
    import tempfile
    from pathlib import Path

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    # 注册 CID 字体（重复注册是幂等的）
    pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))

    tmp_dir = Path(tempfile.mkdtemp(prefix="pdfgen_"))
    path = tmp_dir / "generated.pdf"
    c = canvas.Canvas(str(path))
    for text in pages:
        c.setFont(_CJK_FONT, 14)
        c.drawString(72, 700, text)
        c.showPage()
    c.save()
    return path.read_bytes()


def _write_pdf(tmp_path, name: str, pages: list[str]) -> str:
    p = tmp_path / name
    p.write_bytes(_make_pdf(pages))
    return str(p)


@pytest.fixture(autouse=True)
def _require_cjk_pdf_font():
    """
    造带中文的多页 PDF 需要 reportlab 的中文 CID 字体，且要求能被 pypdf 取回。

    不满足环境时相关用例 **skip**（本机实测可用，跳过逻辑只是防御不同环境）。
    """
    if not _can_make_cjk_pdf():
        pytest.skip("当前环境无法用 reportlab 生成 pypdf 可读的中文 PDF")


def _patch_retriever(monkeypatch, raw_results, rerank_items):
    """把向量检索与 rerank 换成本地实现（不联网）。"""
    import rag.retriever as retriever

    monkeypatch.setattr(retriever, "search_by_owner", lambda *a, **k: raw_results)
    monkeypatch.setattr(retriever, "rerank_with_dashscope", lambda *a, **k: rerank_items)
    return retriever


# ============================================================
# 1. 中文分词：jieba 可用 / 不可用两条路径
# ============================================================


class TestTokenizerJiebaPath:
    """jieba 可用时的分词行为。"""

    def test_multi_char_term_stays_one_token(self):
        """核心收益：多字词在 jieba 路径下**不被拆成单字**。"""
        import rag.bm25_search as bm

        if not bm._JIEBA_AVAILABLE:
            pytest.skip("本机未安装 jieba，无法断言分词路径")

        # 显式声明领域词（jieba 惯用法）；不影响被测代码，只是让本例的断言确定。
        bm._jieba.suggest_freq("机器学习", True)
        try:
            for term in _MULTI_CHAR_TERMS:
                tokens = bm._tokenize(term)
                # 整词出现在 token 里（未被逐字拆开）
                assert term in tokens, f"{term} 在 jieba 路径下应保持为整词，实际={tokens}"
                # 且等于分词器对该串的直接输出（不是靠别的路径拼出来的）
                assert tokens == bm._jieba.lcut(term)
        finally:
            bm._jieba.suggest_freq("机器学习", False)

    def test_not_split_into_single_chars(self):
        """反面断言：不再出现"每个字各成一个 token"的逐字行为。"""
        import rag.bm25_search as bm

        if not bm._JIEBA_AVAILABLE:
            pytest.skip("本机未安装 jieba")

        term = "人工智能"
        tokens = bm._tokenize(term)
        single_chars = [t for t in tokens if len(t) == 1]
        # 逐字 unigram 会给出 人/工/智/能 四个单字；分词后不应如此
        assert single_chars != list(term)
        assert len([t for t in tokens if len(t) >= 2]) >= 1

    def test_latin_and_digits_still_work(self):
        import rag.bm25_search as bm

        tokens = bm._tokenize("Python 3.12 与 LangChain")
        assert "python" in tokens  # 统一小写
        assert "3" in tokens and "12" in tokens
        # 中文按词（LangChain 是英文，留在 alnum 路径）
        assert "langchain" in tokens

    def test_mixed_text_keeps_chinese_words(self):
        import rag.bm25_search as bm

        if not bm._JIEBA_AVAILABLE:
            pytest.skip("本机未安装 jieba")

        tokens = bm._tokenize("我用人工智能做向量检索")
        assert "人工智能" in tokens
        assert "向量" in tokens or "向量检索" in tokens

    def test_empty_text(self):
        import rag.bm25_search as bm

        assert bm._tokenize("") == []


class TestTokenizerFallbackPath:
    """jieba 不可用时的降级行为（用 monkeypatch 模拟缺失）。"""

    @pytest.fixture
    def no_jieba(self, monkeypatch):
        """把模块级标志与 jieba 对象都改掉，模拟"没装 jieba"。"""
        import rag.bm25_search as bm

        monkeypatch.setattr(bm, "_JIEBA_AVAILABLE", False)
        monkeypatch.setattr(bm, "_jieba", None)
        return bm

    def test_fallback_does_not_crash(self, no_jieba):
        tokens = no_jieba._tokenize("人工智能和机器学习")
        assert tokens, "降级路径也必须产出 token"
        assert all(isinstance(t, str) and t for t in tokens)

    def test_fallback_uses_unigram_plus_bigram(self, no_jieba):
        """降级 = 单字(unigram) + 相邻两字(bigram)。"""
        tokens = no_jieba._tokenize("人工智能")
        # 四个单字
        assert set("人工智能") <= set(tokens)
        # 三个 bigram
        assert "人工" in tokens
        assert "工智" in tokens
        assert "智能" in tokens

    def test_fallback_keeps_some_word_order_info(self, no_jieba):
        """
        降级方案比纯 unigram 多保留词序：字相同、顺序不同的串 token 集合不同。
        （纯 unigram 下两者完全一样，无法区分。）
        """
        a = no_jieba._tokenize("人工智能")
        b = no_jieba._tokenize("智能人工")
        # 单字集合相同（unigram 无法区分）……
        assert set("人工智能") <= set(a) and set("人工智能") <= set(b)
        # ……但 bigram 让两者变得可区分
        assert set(a) != set(b)

    def test_fallback_handles_single_char(self, no_jieba):
        """单字没有邻居：只能给 unigram，不能报错。"""
        assert no_jieba._tokenize("人") == ["人"]

    def test_fallback_latin_still_works(self, no_jieba):
        tokens = no_jieba._tokenize("AI 与 NLP")
        assert "ai" in tokens and "nlp" in tokens

    def test_both_paths_agree_on_english(self, no_jieba):
        """中英混排时，英文/数字的提取在两条路径下一致（中文切法不同）。"""
        import rag.bm25_search as bm

        text = "深度学习 DeepLearning 2024"
        fallback = set(no_jieba._tokenize(text))
        assert {"deeplearning", "2024"} <= fallback
        if bm._JIEBA_AVAILABLE:
            assert {"deeplearning", "2024"} <= set(bm._tokenize(text))

    def test_bm25_index_build_works_without_jieba(self, no_jieba, monkeypatch):
        """降级时 BM25 索引构建 + 查询整条链路仍可用（用桩替换向量库）。"""
        import rag.bm25_search as bm

        # 语料要有多篇、且有区分度：单篇语料下 BM25 的 IDF 近乎零、甚至给负分，
        # 而 bm25_search 只保留 >0 的结果，会误判成"没检索到"。
        corpus = [
            "人工智能与机器学习是当前的技术热点",
            "今天午饭吃了牛肉面味道很不错",
            "向量检索可以提升私有知识库的召回质量",
            "周末打算去公园跑步锻炼身体",
        ]

        class _FakeCollection:
            def get(self, **kwargs):
                return {
                    "ids": [str(i) for i in range(len(corpus))],
                    "documents": list(corpus),
                    "metadatas": [{} for _ in corpus],
                }

        class _FakeClient:
            @staticmethod
            def get_collection(name):
                return _FakeCollection()

        monkeypatch.setattr(bm, "_get_chroma_client", lambda: _FakeClient())

        bm._bm25_manager.invalidate()
        try:
            bm25, documents = bm._bm25_manager.build_index(1)
            assert bm25 is not None and len(documents) == len(corpus)
            results = bm.bm25_search("人工智能", 1, top_k=3)
            assert results, "降级路径也要能检索出结果"
            assert results[0]["doc_id"] == "0"
        finally:
            bm._bm25_manager.invalidate()


# ============================================================
# 2. PDF 页码
# ============================================================


class TestPdfPageNumbers:
    def test_load_pdf_pages_returns_one_entry_per_page(self, tmp_path):
        from rag.document_loader import load_pdf_pages

        path = _write_pdf(tmp_path, "two.pdf", ["第一页内容", "第二页内容"])
        pages = load_pdf_pages(path)
        assert len(pages) == 2
        assert "第一页内容" in pages[0]
        assert "第二页内容" in pages[1]

    def test_load_pdf_still_returns_joined_string(self, tmp_path):
        """向后兼容：load_pdf 仍是"拼接后的字符串"。"""
        from rag.document_loader import load_pdf

        path = _write_pdf(tmp_path, "join.pdf", ["甲页", "乙页"])
        content = load_pdf(path)
        assert isinstance(content, str)
        assert "甲页" in content and "乙页" in content

    def test_detailed_outcome_carries_pages_and_content(self, tmp_path):
        from rag.document_loader import load_document_detailed

        path = _write_pdf(tmp_path, "det.pdf", ["第一页内容", "第二页内容"])
        outcome = load_document_detailed(path)

        assert outcome.status == "ok"
        assert len(outcome.pages) == 2
        # content 仍是全文（旧字段行为不变）
        assert "第一页内容" in outcome.content
        assert "第二页内容" in outcome.content

    def test_non_pdf_has_no_pages(self, tmp_path):
        """其它格式没有页码：pages 为空（不会因此崩）。"""
        from rag.document_loader import load_document_detailed

        p = tmp_path / "a.txt"
        p.write_text("纯文本内容", encoding="utf-8")
        outcome = load_document_detailed(str(p))
        assert outcome.status == "ok"
        assert outcome.pages == []

    def test_failed_pdf_has_no_pages(self, tmp_path):
        from rag.document_loader import load_document_detailed

        p = tmp_path / "broken.pdf"
        p.write_bytes(b"%PDF-1.4 not really a pdf")
        outcome = load_document_detailed(str(p))
        assert outcome.status == "failed"
        assert outcome.pages == []


class TestPageAwareSplitting:
    """按页切分：每个 chunk 的页码必须与它所在页对应。"""

    def _outcome_with_pages(self, pages: list[str]):
        from rag.document_loader import LoadOutcome

        return LoadOutcome("f.pdf", "f.pdf", content="\n\n".join(pages), status="ok", pages=pages)

    def test_chunks_get_correct_page_numbers(self):
        from core.kb_tasks import _split_loaded_document

        pages = [
            "第一页的内容讲的是项目背景与目标设定等等等",
            "第二页的内容讲的是具体实现方案与细节等等等",
            "第三页的内容讲的是测试结论与后续计划等等等",
        ]
        chunks = _split_loaded_document(self._outcome_with_pages(pages), use_semantic_splitter=True)

        assert chunks, "应产出 chunk"
        for ch in chunks:
            assert ch["page"] is not None
            # 每个 chunk 的正文只能来自它自己那一页
            assert ch["content"].strip() in pages[ch["page"] - 1]

        # 页码集合覆盖三页
        assert {ch["page"] for ch in chunks} == {1, 2, 3}

    def test_chunks_never_cross_pages(self):
        """按页切分 ⇒ chunk 不跨页（跨页语义单元被页边界拆开，是刻意的取舍）。"""
        from core.kb_tasks import _split_loaded_document

        pages = ["甲" * 40, "乙" * 40]
        chunks = _split_loaded_document(self._outcome_with_pages(pages), use_semantic_splitter=True)
        for ch in chunks:
            assert "甲" not in ch["content"] or "乙" not in ch["content"]

    def test_blank_page_keeps_index_alignment(self):
        """空白页被跳过，但不影响其它页的页码仍与其物理页对齐。"""
        from core.kb_tasks import _split_loaded_document

        pages = [
            "第一页有效内容足够长可以成块了这是一段正文",
            "",
            "第三页有效内容足够长可以成块了这是一段正文",
        ]
        chunks = _split_loaded_document(self._outcome_with_pages(pages), use_semantic_splitter=True)
        page_nums = {ch["page"] for ch in chunks}
        assert 1 in page_nums and 3 in page_nums
        assert 2 not in page_nums  # 空白页不产出 chunk
        for ch in chunks:
            assert ch["content"].strip() in pages[ch["page"] - 1]

    def test_no_pages_falls_back_to_none(self):
        """没有逐页信息（非 PDF）时页码为 None，行为与旧版一致。"""
        from core.kb_tasks import _split_loaded_document
        from rag.document_loader import LoadOutcome

        outcome = LoadOutcome("a.txt", "a.txt", content="一段足够长的纯文本内容" * 3, status="ok")
        chunks = _split_loaded_document(outcome, use_semantic_splitter=True)
        assert chunks
        assert all(ch["page"] is None for ch in chunks)

    def test_non_pdf_uses_batch_splitter_entry(self, monkeypatch):
        """
        非 PDF 路径必须走 ``process_documents_batch``（与旧实现同一条路）。

        这是刻意的：既有的重建回归测试都是对 ``process_documents_batch`` 打桩的，
        保持这条路径能避免它们失效，也让"没有页码"的默认行为与旧版逐字一致。
        """
        import core.kb_tasks as kb
        import rag.text_splitter as ts
        from rag.document_loader import LoadOutcome

        calls = []

        def _fake_batch(docs, **kwargs):
            calls.append(docs)
            return [
                {"filepath": d["filepath"], "filename": d["filename"], "chunks": ["切出来的一个块"]}
                for d in docs
            ]

        monkeypatch.setattr(ts, "process_documents_batch", _fake_batch)
        outcome = LoadOutcome("a.txt", "a.txt", content="随便什么正文", status="ok")

        chunks = kb._split_loaded_document(outcome, use_semantic_splitter=True)

        assert calls, "应调用 process_documents_batch"
        assert chunks == [{"content": "切出来的一个块", "page": None}]

    def test_pdf_path_does_not_use_batch_entry(self, monkeypatch):
        """PDF 有页码时必须逐页切：不能走批量入口（否则页码无从对应）。"""
        import core.kb_tasks as kb
        import rag.text_splitter as ts
        from rag.document_loader import LoadOutcome

        def _boom(*a, **k):
            raise AssertionError("PDF 路径不应调用 process_documents_batch")

        monkeypatch.setattr(ts, "process_documents_batch", _boom)
        pages = ["第一页的内容足够长可以成块了这是一段正文文字"]
        outcome = LoadOutcome("f.pdf", "f.pdf", content=pages[0], status="ok", pages=pages)

        chunks = kb._split_loaded_document(outcome, use_semantic_splitter=True)
        assert chunks and all(ch["page"] == 1 for ch in chunks)

    def test_metadata_omits_none_page_and_keeps_old_keys(self):
        """metadata 构造：旧键齐全；page 为 None 时不写入（Chroma 不接受 None）。"""
        from core.kb_tasks import _chunk_metadatas

        chunks = [
            {"content": "a", "page": 2},
            {"content": "b", "page": None},
        ]
        metas = _chunk_metadatas(chunks, owner_id=7, filename="f.pdf", filepath="/x/f.pdf")

        assert metas[0]["page"] == 2
        assert "page" not in metas[1]
        for i, meta in enumerate(metas):
            assert meta["owner_id"] == 7
            assert meta["source"] == "f.pdf"
            assert meta["chunk_idx"] == i
            assert meta["filepath"] == "/x/f.pdf"


# ============================================================
# 3. 页码一路带到 retrieve() 的结果（端到端、离线）
# ============================================================


class TestPageReachesRetrieveResult:
    def test_retrieve_documents_carry_page_and_keep_old_fields(self, monkeypatch):
        raw = [
            {
                "content": "第一页的片段",
                "metadata": {
                    "source": "doc.pdf",
                    "chunk_idx": 0,
                    "filepath": "/data/doc.pdf",
                    "page": 1,
                },
                "score": 0.9,
            },
            {
                "content": "第二页的片段",
                "metadata": {"source": "doc.pdf", "chunk_idx": 1, "page": 2},
                "score": 0.8,
            },
        ]
        rerank = [
            {"index": 0, "score": 0.95, "text": "第一页的片段"},
            {"index": 1, "score": 0.5, "text": "第二页的片段"},
        ]
        retriever = _patch_retriever(monkeypatch, raw, rerank)
        result = retriever._retrieve_impl(
            "q", 1, top_k_search=10, top_k_rerank=5, use_hybrid=False, bm25_weight=0.3
        )

        docs = result["documents"]
        assert len(docs) == 2
        # 新增键：页码
        assert docs[0]["page"] == 1
        assert docs[1]["page"] == 2
        # 旧字段一个不少（向后兼容）
        for d in docs:
            assert {"content", "score", "source", "chunk_idx", "filepath"} <= set(d)
        assert docs[0]["chunk_idx"] == 0  # 0 是合法值，不能被当 falsy 丢掉
        assert docs[0]["filepath"] == "/data/doc.pdf"

    def test_missing_page_is_none_not_crash(self, monkeypatch):
        """旧数据（入库时还没写 page）→ page 为 None，不崩。"""
        raw = [{"content": "旧片段", "metadata": {"source": "old.txt"}, "score": 0.8}]
        retriever = _patch_retriever(
            monkeypatch, raw, [{"index": 0, "score": 0.7, "text": "旧片段"}]
        )
        result = retriever._retrieve_impl(
            "q", 1, top_k_search=10, top_k_rerank=5, use_hybrid=False, bm25_weight=0.3
        )
        doc = result["documents"][0]
        assert doc["page"] is None
        assert doc["chunk_idx"] is None


# ============================================================
# 4. PDF → 切分 → metadata 的整条离线链路
# ============================================================


class TestPdfToMetadataPipeline:
    def test_pdf_pages_flow_into_chunk_metadata(self, tmp_path):
        """造一个多页 PDF，走 加载→按页切分→metadata 构造，断言页码对应正确。"""
        from core.kb_tasks import _chunk_metadatas, _split_loaded_document
        from rag.document_loader import load_document_detailed

        page_texts = [
            "第一页内容：介绍项目背景与总体目标，以及要解决的问题范围",
            "第二页内容：详细的技术实现方案说明，包括分块与检索策略",
        ]
        path = _write_pdf(tmp_path, "pipeline.pdf", page_texts)

        outcome = load_document_detailed(path)
        assert outcome.status == "ok"
        assert len(outcome.pages) == 2

        chunks = _split_loaded_document(outcome, use_semantic_splitter=True)
        metas = _chunk_metadatas(chunks, owner_id=1, filename="pipeline.pdf", filepath=path)

        assert len(metas) == len(chunks)
        assert {m.get("page") for m in metas} == {1, 2}
        # 页码既在 metadata 里，也与 chunk 正文所在页一致
        for ch, meta in zip(chunks, metas, strict=True):
            assert meta["page"] == ch["page"]
            assert ch["content"].strip() in outcome.pages[meta["page"] - 1]

    def test_long_page_is_split_but_all_chunks_share_that_page(self):
        """同一页内容很长被切成多块时，这些块的页码都应是同一页。"""
        from core.kb_tasks import _split_loaded_document
        from rag.document_loader import LoadOutcome

        long_page = "这一页的内容很长。" * 300  # 远超 chunk_size，必被切多块
        outcome = LoadOutcome(
            "big.pdf", "big.pdf", content=long_page, status="ok", pages=[long_page]
        )
        chunks = _split_loaded_document(outcome, use_semantic_splitter=True)

        assert len(chunks) > 1
        assert all(ch["page"] == 1 for ch in chunks)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
