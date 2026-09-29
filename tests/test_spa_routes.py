"""
SPA 路由兜底测试
================

**这个用例是为了防一类很隐蔽的 404。**

原来 `main.py` 里是把前端页面名**逐个列举**成装饰器的：

    @fastapi_app.get("/")
    @fastapi_app.get("/chat")
    @fastapi_app.get("/knowledge")
    @fastapi_app.get("/admin")
    @fastapi_app.get("/eval")
    async def serve_spa(): ...

于是有两个问题：
1. **前端每加一个页面都要改后端，漏一个就是 404** —— 加「用量」页
   （`/stats`）时就漏了，点进去直接 `{"detail":"Not Found"}`
2. **`/login` 从来就不在列表里** —— 它平时能用只是因为前端路由是**客户端跳转**、
   没真正请求服务器；一旦在登录页刷新或直接输网址，就是 404

改成 catch-all 之后，"加页面"不再需要动后端。这个文件守住那条不变式。
"""

from fastapi.testclient import TestClient


def _client():
    # 根模块 main.py 负责挂载前端（api/main.py 只管 API）
    import main as root

    return TestClient(root.app)


class TestSpaRoutes:
    def test_all_spa_paths_serve_index(self):
        """
        每个前端路由都要能直接打开（刷新/收藏夹/直接输网址都是这种请求）。

        以后加页面时把路径加到这里 —— 如果后端没兜住，这条会红，
        而不是等用户在浏览器里撞见 404。
        """
        c = _client()
        for path in ["/", "/chat", "/knowledge", "/eval", "/stats", "/login", "/admin"]:
            r = c.get(path)
            assert r.status_code == 200, f"{path} 返回 {r.status_code}（SPA 兜底失效）"
            assert "text/html" in r.headers.get("content-type", ""), (
                f"{path} 没返回 HTML，实际是 {r.headers.get('content-type')}"
            )

    def test_unknown_api_path_still_404(self):
        """
        catch-all **不能**把不存在的 API 路径也吞掉。

        否则接口路径写错时会拿到一个 200 的 HTML，排查半天看不出问题。
        """
        c = _client()
        r = c.get("/api/definitely-not-a-real-endpoint")
        assert r.status_code == 404, "不存在的 API 路径应该 404"

    def test_health_still_works(self):
        """catch-all 不能抢在真正的 API 路由前面"""
        c = _client()
        assert c.get("/api/health").status_code == 200
