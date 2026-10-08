"""
切块参数「是否真的有效」的测试
================================

这些用例的存在理由，是一次**真实的事故**：

界面上「切块设置」卡片里，"块大小"在结构感知模式下被**禁用**了，说明文字还写着
"块大小与重叠仅在定长兜底时生效"。而实测（同一个文件 08_面试问答准备.md）：

    结构感知 + 块大小 1000 → 123 块
    结构感知 + 块大小 300  → 204 块        ← 明显生效，却被禁用 + 说成无效

同时"自定义分隔符"没禁用、也没任何提示，而它对结构良好的文档**几乎无效**。

两个错误的方向相反：一个把有效的说成无效，一个把几乎无效的摆在那里不说。
**只有实测能同时抓住这两种错**，所以这些用例的核心不是"函数返回对不对"，
而是"改这个参数，切块结果会不会变"。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from core.chunking import ChunkingConfig, default_chunking_config


def _structured_doc(sections: int = 12) -> str:
    """
    造一份**各段内容互不相同**的结构化文档。

    为什么必须"互不相同"：切块后有一步去重（``deduplicate_chunks``），内容
    相同的块会被合并。第一版这里用的是"同一段文字重复 N 遍"，结果无论怎么调
    参数都只有 3 块 —— 测试反而证明了去重有效，而不是参数有效。
    """
    lines: list[str] = ["# 面试准备材料", ""]
    for i in range(sections):
        # 每节都要**明显长于**块大小（默认 1000），否则"结构块超长被二次切分"
        # 这条路径根本不会触发，块大小看起来就是无效的 —— 第一版就这么写错了。
        body = f"这是第 {i} 节的内容，讲了主题{i}相关的具体细节，需要展开说明。" * (20 + i % 11)
        lines += [
            f"## 第 {i} 节 · 主题{i}",
            "",
            body,
            "",
            f"### 第 {i} 节的小结",
            "",
            f"本节的结论是第 {i} 条结论。",
            "",
        ]
    return "\n".join(lines)


@pytest.fixture
def kb_dir():
    """在**知识库目录内**造文件（用 pytest 的 tmp_path 会越界，被包含性校验拦下）。"""
    import itertools

    base = settings.resolve_path(settings.upload_dir)
    counter = itertools.count(1)

    def _make(text: str | None = None, name: str = "doc.md") -> Path:
        target = base / f"chunk_preview_test_{next(counter)}_{name}"
        target.write_text(text if text is not None else _structured_doc(), encoding="utf-8")
        return target

    return _make


def _preview(path: Path, candidate: ChunkingConfig, current: ChunkingConfig | None = None) -> dict:
    from core.kb_tasks import preview_split

    upload_dir = str(settings.resolve_path(settings.upload_dir))
    return preview_split(str(path), upload_dir, candidate, current or default_chunking_config())


class TestParametersActuallyChangeTheOutput:
    """
    每个参数：改它，结果必须变。**这是判"真假控件"的唯一标准。**

    用例失败意味着：界面上的这个控件对用户是无效的（或者说，它的效果与预期不符）。
    """

    def test_chunk_size_changes_output_in_semantic_mode(self, kb_dir):
        """
        块大小在**结构感知**模式下也生效 —— 这是当初被搞错的那一条。

        界面曾把它禁用并声称"仅在定长兜底时生效"。实测它控制的是"结构块超过
        1.5 倍块大小时要不要二次切分"，对结构良好的文档同样有效。
        """
        path = kb_dir()
        small = _preview(path, ChunkingConfig(mode="semantic", chunk_size=200, chunk_overlap=0))
        large = _preview(path, ChunkingConfig(mode="semantic", chunk_size=4000, chunk_overlap=0))

        assert small["candidate"]["total_chunks"] > large["candidate"]["total_chunks"], (
            "结构感知模式下改块大小居然没变化 —— 那这个输入框就该禁用或删掉，"
            f"而不是既让用户填又说是无效: {small['candidate']['total_chunks']} vs "
            f"{large['candidate']['total_chunks']}"
        )
        assert not small["identical"], "两套块大小切出完全一样的结果"

    def test_mode_changes_output(self, kb_dir):
        """分段方式切换必须真的换算法。"""
        path = kb_dir()
        semantic = ChunkingConfig(mode="semantic", chunk_size=1000, chunk_overlap=200)
        fixed = ChunkingConfig(mode="fixed", chunk_size=1000, chunk_overlap=200)
        # 拿定长当"候选"、结构感知当"当前"来比 —— 默认配置本身就是结构感知，
        # 拿它当对照会把"两个模式一样"错判成通过（第一版就是这么写的）
        report = _preview(path, fixed, current=semantic)

        assert not report["identical"], "结构感知与定长切出了同样的结果，模式选择器是假的"
        assert report["current"]["total_chunks"] != report["candidate"]["total_chunks"]

    def test_separators_have_effect_in_fixed_mode(self, kb_dir):
        """定长模式下分隔符必须有效 —— 它是那一档的主要切分依据。"""
        path = kb_dir(text="第一段内容。\n===\n第二段内容。\n===\n第三段内容。" * 30)
        plain = ChunkingConfig(mode="fixed", chunk_size=200, chunk_overlap=0)
        with_sep = ChunkingConfig(
            mode="fixed", chunk_size=200, chunk_overlap=0, separators=["\n===\n", "\n"]
        )
        # 必须拿**同一个模式**下的两套配置比。第一版写成了 `_preview(path, with_sep)`
        # 而对照是"默认配置"（结构感知）—— 那样断言必然通过，因为变的根本不是
        # 分隔符而是模式。**空转的断言比没有断言更糟：它看起来覆盖了。**
        report = _preview(path, with_sep, current=plain)

        assert not report["identical"], "定长模式下改分隔符没变化，这个控件就是假的"

    def test_separators_barely_matter_in_semantic_mode(self, kb_dir):
        """
        **如实记录自定义分隔符的局限**：结构感知 + 结构良好的文档下，它几乎无效。

        不是"完全无效"（超大块的二次切分会用到它），但作为用户可填的字段，
        它的收益与预期严重不符 —— 所以界面上必须写清适用范围，并提供试切预览
        让用户自己看见（实测 123 → 122 块）。
        """
        path = kb_dir()
        without = _preview(
            path, ChunkingConfig(mode="semantic", chunk_size=1000, chunk_overlap=200)
        )
        with_sep = _preview(
            path,
            ChunkingConfig(
                mode="semantic", chunk_size=1000, chunk_overlap=200, separators=["\n### ", "\n\n"]
            ),
        )
        n1 = without["candidate"]["total_chunks"]
        n2 = with_sep["candidate"]["total_chunks"]
        # 允许有一点点差异，但**不该指望它显著改变切法**
        assert abs(n1 - n2) <= max(2, n1 // 20), (
            f"分隔符在结构感知下产生了显著差异({n1}→{n2})，那就不该按'几乎无效'来写文案了"
        )


class TestPreviewShape:
    """预览的返回结构 —— 前端照着它渲染"当前 vs 候选"的对比。"""

    def test_compares_current_against_candidate(self, kb_dir):
        path = kb_dir()
        current = ChunkingConfig(mode="semantic", chunk_size=1000, chunk_overlap=200)
        report = _preview(
            path, ChunkingConfig(mode="semantic", chunk_size=200, chunk_overlap=0), current
        )

        assert report["ok"] is True
        assert report["current"]["config"]["chunk_size"] == 1000
        assert report["candidate"]["config"]["chunk_size"] == 200
        assert report["current"]["total_chunks"] and report["candidate"]["total_chunks"]
        assert "avg_chars" in report["candidate"] and "max_chars" in report["candidate"]

    def test_sample_is_truncated_not_full_text(self, kb_dir):
        path = kb_dir()
        report = _preview(path, ChunkingConfig(mode="semantic", chunk_size=4000, chunk_overlap=0))
        sample = report["candidate"]["sample"]
        assert sample, "预览至少要给一块样例，否则用户看不出切成什么样"
        assert all(c["chars"] >= len(c["content"]) for c in sample)
        assert all(len(c["content"]) <= 401 for c in sample), "样例要截断，别把整段正文塞进响应"

    def test_does_not_touch_the_vector_store_or_database(self, kb_dir, monkeypatch):
        """预览是**只读**的：绝不能顺手写向量库（否则"看看效果"就污染了知识库）。"""
        import rag.vector_store as vs

        called = []
        monkeypatch.setattr(vs, "add_documents", lambda *a, **k: called.append("add"))
        monkeypatch.setattr(vs, "delete_by_file", lambda *a, **k: called.append("delete"))
        monkeypatch.setattr(vs, "delete_all_by_owner", lambda *a, **k: called.append("delete_all"))

        _preview(kb_dir(), ChunkingConfig(mode="semantic", chunk_size=300, chunk_overlap=0))

        assert called == [], f"预览不该写向量库，但它调用了: {called}"

    def test_unparseable_file_reports_why(self):
        """解析不了就如实说原因，而不是抛 500。"""
        from core.kb_tasks import preview_split

        upload_dir = str(settings.resolve_path(settings.upload_dir))
        missing = Path(upload_dir) / "definitely_not_here.md"
        report = preview_split(
            str(missing), upload_dir, default_chunking_config(), default_chunking_config()
        )
        assert report["ok"] is False
        assert report["reason"]


class TestPreviewEndpoint:
    """接口层：鉴权、归属、非法参数。"""

    @pytest.fixture(scope="class")
    def client(self):
        from api.main import app

        with TestClient(app) as c:
            yield c

    def test_requires_login(self, client):
        resp = client.post("/api/kb/chunking/preview", json={"file_id": 1})
        assert resp.status_code in (401, 403), "试切预览未鉴权就能调用"

    def test_requires_admin(self, client):
        """它是"改配置前先看看"的工具，归属管理侧 —— 与保存配置同一个门槛。"""
        from api.main import app
        from core.auth import get_current_user

        app.dependency_overrides[get_current_user] = lambda: {
            "owner_id": 1,
            "username": "plain",
            "role": "user",
        }
        try:
            resp = client.post("/api/kb/chunking/preview", json={"file_id": 1})
            assert resp.status_code in (401, 403), "普通用户不该能调管理侧的试切工具"
        finally:
            app.dependency_overrides.pop(get_current_user, None)
