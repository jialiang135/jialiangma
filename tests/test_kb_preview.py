"""
知识库预览（入库切片 / 原文件内容）的测试
==========================================

两个只读端点：`GET /api/kb/files/{id}/chunks` 与 `.../content`。

这里最要紧的**不是**"能不能显示"，而是**包含性校验**：

这个项目被"路径不可信"咬过两次 —— 上传接口原来直接用客户端传的文件名拼路径，
`../../../../config/settings.py` 能写到知识库目录之外；库里的 `filepath` 又
出现过**另一台机器**的绝对路径（数据库整体搬过来），让重建把知识库打成残缺。
预览接口如果按文件名取文件、或者不校验库里的路径，就是把这些洞重新开一遍。

所以用例里有一条是"指向目录外文件时：必须 400，而且**绝不能读到那个文件**"
—— 后者靠往那个文件里写标记字符串、再断言响应体里没有它。
"""

import pytest
from fastapi.testclient import TestClient

from config.settings import settings


@pytest.fixture(scope="module")
def client():
    from api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def as_user():
    """把鉴权换成指定用户；用例结束自动摘掉覆盖。"""
    from api.main import app
    from core.auth import get_current_user

    def _login(owner_id: int = 1, role: str = "admin"):
        app.dependency_overrides[get_current_user] = lambda: {
            "owner_id": owner_id,
            "username": "tester",
            "role": role,
        }

    yield _login
    app.dependency_overrides.pop(get_current_user, None)


def _patch_file_record(monkeypatch, record: dict | None):
    """把 kb_routes 里按 id 取文件记录换掉（真实实现会打数据库）。"""
    import api.routes.kb_routes as kb

    async def _fake_get_file_by_id(_file_id):
        return record

    monkeypatch.setattr(kb, "get_file_by_id", _fake_get_file_by_id)


@pytest.fixture
def kb_file(tmp_path_factory):
    """
    在**知识库目录内**造一个真实文件。

    注意不能用 pytest 的 `tmp_path` —— 它在 `upload_dir` 之外，会被包含性校验
    正确拦下（第一版测试就是这么写错的，反而证明了校验是有效的）。
    """
    import itertools

    from config.settings import settings

    base = settings.resolve_path(settings.upload_dir)
    counter = itertools.count(1)

    def _make(name: str = "note.md", text: str = "内容"):
        target = base / f"preview_test_{next(counter)}_{name}"
        target.write_text(text, encoding="utf-8")
        return target

    return _make


def _record(filepath: str, *, owner_id: int = 1, filename: str = "a.md") -> dict:
    return {"id": 7, "owner_id": owner_id, "filename": filename, "filepath": filepath}


class TestPreviewRequiresLogin:
    """两个端点都必须要求登录 —— 它们是知识库内容的直接视图。"""

    @pytest.mark.parametrize(
        "path",
        ["/api/kb/files/1/chunks", "/api/kb/files/1/content"],
    )
    def test_unauthenticated_is_rejected(self, client, path):
        resp = client.get(path)
        assert resp.status_code in (401, 403), f"{path} 未鉴权就能访问: {resp.status_code}"


class TestPreviewOwnership:
    def test_unknown_file_is_404(self, client, as_user, monkeypatch):
        as_user(owner_id=1)
        _patch_file_record(monkeypatch, None)
        assert client.get("/api/kb/files/999/chunks").status_code == 404

    def test_file_of_another_owner_is_404(self, client, as_user, monkeypatch):
        """拿共享知识库的文件 id 去查别的 owner 的记录 → 不能泄漏。"""
        as_user(owner_id=1)
        _patch_file_record(monkeypatch, _record("/app/assets/upload_docs/a.md", owner_id=999))
        assert client.get("/api/kb/files/7/chunks").status_code == 404
        assert client.get("/api/kb/files/7/content").status_code == 404


class TestPreviewPathContainment:
    """
    库里的 `filepath` 一律不可信：可能来自别的机器，也可能是历史遗留的穿越路径。
    """

    def test_path_outside_upload_dir_is_refused_and_not_read(
        self, client, as_user, monkeypatch, tmp_path
    ):
        secret = tmp_path / "secret.txt"
        secret.write_text("MUST-NOT-LEAK-42", encoding="utf-8")

        as_user(owner_id=1)
        _patch_file_record(monkeypatch, _record(str(secret)))

        for path in ("/api/kb/files/7/chunks", "/api/kb/files/7/content"):
            resp = client.get(path)
            assert resp.status_code == 400, f"{path} 应拒绝越界路径，实际 {resp.status_code}"
            assert "MUST-NOT-LEAK-42" not in resp.text, f"{path} 泄漏了目录外的文件内容！"

    def test_windows_style_path_from_another_machine_is_refused(self, client, as_user, monkeypatch):
        """
        真实发生过的那种记录：另一台机器上的绝对路径（数据库整体搬过来的）。
        在 Linux 上它会被当相对路径解析而越界 —— 必须 400，而不是 500。
        """
        as_user(owner_id=1)
        _patch_file_record(
            monkeypatch,
            _record(r"E:\zuoye\jialiangma\personal_agent\assets\upload_docs\01_自我介绍.md"),
        )
        resp = client.get("/api/kb/files/7/chunks")
        assert resp.status_code == 400
        assert "无法预览" in resp.text


class TestChunksEndpoint:
    def test_returns_sorted_chunks_with_expected_shape(self, client, as_user, monkeypatch, kb_file):
        import rag.vector_store as vs

        inside = kb_file(text="x")

        as_user(owner_id=1)
        _patch_file_record(monkeypatch, _record(str(inside)))

        captured: dict = {}

        def fake_list_chunks(source, owner_id, limit, offset):
            captured.update(source=source, owner_id=owner_id, limit=limit, offset=offset)
            return {
                "total": 2,
                "chunks": [
                    {"chunk_idx": 0, "page": 1, "content": "第一块", "chars": 3},
                    {"chunk_idx": 1, "page": None, "content": "第二块", "chars": 3},
                ],
            }

        monkeypatch.setattr(vs, "list_chunks", fake_list_chunks)

        resp = client.get("/api/kb/files/7/chunks")
        assert resp.status_code == 200
        data = resp.json()["data"]

        assert data["total"] == 2
        assert [c["chunk_idx"] for c in data["chunks"]] == [0, 1]
        assert data["chunks"][1]["page"] is None  # 非 PDF 没有页码，不能是缺字段
        # 归属必须由 kb_access 裁决后传给向量库，而不是请求方自己的 owner_id
        assert captured["owner_id"] == settings.shared_kb_owner_id

    def test_another_owner_never_reaches_the_vector_store(self, client, as_user, monkeypatch):
        """越权请求应该在查向量库之前就被拦掉（别让无效请求打到 Chroma）。"""
        import rag.vector_store as vs

        called = []
        monkeypatch.setattr(vs, "list_chunks", lambda *a, **k: called.append(1) or {})
        as_user(owner_id=1)
        _patch_file_record(monkeypatch, _record("/app/assets/upload_docs/a.md", owner_id=999))

        assert client.get("/api/kb/files/7/chunks").status_code == 404
        assert called == [], "越权请求不该打到向量库"


class TestContentEndpoint:
    def test_raw_returns_the_original_bytes(self, client, as_user, monkeypatch, kb_file):
        real = kb_file(text="原文件正文")

        as_user(owner_id=1)
        _patch_file_record(monkeypatch, _record(str(real), filename="note.md"))

        resp = client.get("/api/kb/files/7/content?mode=raw")
        assert resp.status_code == 200
        assert "原文件正文" in resp.text

    def test_text_mode_returns_parsed_text_and_pages(self, client, as_user, monkeypatch, kb_file):
        import rag.document_loader as dl

        real = kb_file()

        as_user(owner_id=1)
        _patch_file_record(monkeypatch, _record(str(real), filename="doc.pdf"))

        class _Outcome:
            import typing

            status: typing.ClassVar[str] = "ok"
            reason: typing.ClassVar[str] = "ok"
            detail: typing.ClassVar[str] = "解析正常"
            content: typing.ClassVar[str] = "解析出来的正文"
            pages: typing.ClassVar[list] = ["第一页", "第二页"]

        monkeypatch.setattr(dl, "load_document_detailed", lambda *_a, **_k: _Outcome())

        resp = client.get("/api/kb/files/7/content?mode=text")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["text"] == "解析出来的正文"
        assert data["chars"] == len("解析出来的正文")
        assert data["pages"] == ["第一页", "第二页"], "PDF 的逐页文本要透出来，切片页码才有出处"

    def test_missing_source_file_is_409_not_500(self, client, as_user, monkeypatch):
        """记录在、文件被删了 —— 如实说清楚，而不是 500。"""
        from config.settings import settings

        gone = settings.resolve_path(settings.upload_dir) / "definitely_gone.md"

        as_user(owner_id=1)
        _patch_file_record(monkeypatch, _record(str(gone)))

        resp = client.get("/api/kb/files/7/content")
        assert resp.status_code == 409


class TestSupportedFormatsEndpoint:
    """
    上传支持的格式清单必须由后端提供、前端取用。

    前端原先手抄了一份 `accept`，抄漏了 .doc/.xls/.csv/.jpeg/.bmp/.tiff ——
    页面文案说"支持 Excel"，选择器里却没有 .xls；切到"所有文件"又能传上去。
    两边说法不一致，用户只能猜。现在只有一份清单。
    """

    def test_no_login_required_is_ok_but_content_is_generic(self, client):
        """它不含敏感信息（只是扩展名），不强制登录；但要能正常返回。"""
        resp = client.get("/api/kb/formats")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["extensions"], "格式清单不能为空"

    def test_matches_the_backend_whitelist_exactly(self, client):
        """清单必须**等于**真正的上传白名单 —— 多一个会误导，少一个会让人以为不支持。"""
        from api.routes.kb_routes import ALLOWED_UPLOAD_EXTENSIONS

        assert set(client.get("/api/kb/formats").json()["data"]["extensions"]) == set(
            ALLOWED_UPLOAD_EXTENSIONS
        )

    def test_covers_formats_the_frontend_used_to_miss(self, client):
        """这几类正是当初抄漏的 —— 用断言钉住。"""
        exts = set(client.get("/api/kb/formats").json()["data"]["extensions"])
        for missing in (".doc", ".xls", ".csv", ".jpeg", ".bmp", ".tiff"):
            assert missing in exts, f"{missing} 又漏了，前端选择器会过滤掉它"
