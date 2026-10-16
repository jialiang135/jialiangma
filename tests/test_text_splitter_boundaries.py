"""Boundary and fallback paths for document cleaning and text splitting."""


def test_cleaning_and_structural_split_helpers_cover_empty_and_mixed_inputs():
    from rag.text_splitter import (
        clean_text,
        deduplicate_chunks,
        filter_short_chunks,
        split_by_headings,
        split_by_numbered_sections,
        split_by_paragraphs,
    )

    assert clean_text("") == ""
    assert clean_text(" a\x00\x01  b\n\n\n\n c ") == "a b\n\n\n c"
    assert filter_short_chunks([" x ", "long enough"], min_length=4) == ["long enough"]
    assert deduplicate_chunks([" same ", "same", "different"]) == [" same ", "different"]

    headings = split_by_headings("preamble\n# First\nbody\n## Second\nmore")
    assert headings == ["preamble", "# First\nbody", "## Second\nmore"]
    numbered = split_by_numbered_sections("intro\n第一章 起因\n正文\n2. 结果\n结论")
    assert numbered == ["intro", "第一章 起因\n正文", "2. 结果\n结论"]
    assert split_by_paragraphs("one\r\n\r\ntwo") == ["one", "two"]
    assert split_by_paragraphs(" \n\n ") == [" \n\n "]


def test_semantic_splitter_selects_heading_numbered_paragraph_and_fixed_paths():
    from rag.text_splitter import SemanticTextSplitter

    splitter = SemanticTextSplitter(chunk_size=20, chunk_overlap=0, min_chunk_length=1)
    assert splitter.split_text(" \n ") == []
    assert splitter.split_text("# A\nalpha\n# B\nbeta") == ["# A\nalpha", "# B\nbeta"]
    assert splitter.split_text("第一章\nalpha\n第二章\nbeta") == ["第一章\nalpha", "第二章\nbeta"]
    assert splitter.split_text("first paragraph\n\nsecond paragraph") == [
        "first paragraph",
        "second paragraph",
    ]
    text = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"
    fixed = splitter.split_text(text)
    assert len(fixed) > 1
    assert "".join(fixed) == text


def test_oversized_refinement_splits_nested_paragraphs_and_single_blocks(monkeypatch):
    import rag.text_splitter as module

    calls = []

    class FakeSplitter:
        def split_text(self, text):
            calls.append(text)
            return [text[:10], text[10:]]

    monkeypatch.setattr(module, "create_text_splitter", lambda **_kwargs: FakeSplitter())
    splitter = module.SemanticTextSplitter(chunk_size=10, chunk_overlap=0, min_chunk_length=1)
    nested = "1234567890123456\n\nsmall"
    single = "abcdefghijklmnop"

    result = splitter._refine_oversized(["tiny", nested, single])
    assert calls == ["1234567890123456", single]
    assert result == ["tiny", "1234567890", "123456", "small", "abcdefghij", "klmnop"]


def test_document_processing_rejects_empty_content_and_batch_skips_empty_documents():
    from rag.text_splitter import process_document, process_documents_batch

    assert process_document("\x00\x01 \n ", use_semantic_splitter=True) == []
    assert process_documents_batch(
        [
            {"content": "\x00", "filepath": "empty", "filename": "empty"},
            {
                "content": "a sufficiently long paragraph for indexing",
                "filepath": "ok",
                "filename": "ok",
            },
        ],
        chunk_size=100,
        chunk_overlap=0,
    ) == [
        {
            "filepath": "ok",
            "filename": "ok",
            "chunks": ["a sufficiently long paragraph for indexing"],
        }
    ]
