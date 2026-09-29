"""
限流测试
========

**这些用例是为了防"限流配了但没生效"复发。**

原状况：`api/main.py` 创建了 `Limiter`、注册了 `RateLimitExceeded` 处理器，
**却没有 `add_middleware(SlowAPIMiddleware)`**。而 slowapi 的 `default_limits`
是**由中间件执行**的 —— 所以那套限流完全没在跑，而 README 里却写着
"slowapi 限流"。更糟的是测试里**连一个 429 用例都没有**，所以没人发现。

所以这里两层都要守：
1. **结构性**：中间件必须在 app 的中间件栈里（直接盯住那次回归）
2. **功能性**：真的打满会返回 429（证明它不只是"挂上了"）
"""

import pytest
from fastapi.testclient import TestClient
from slowapi.middleware import SlowAPIMiddleware


@pytest.fixture(scope="module")
def app():
    from api.main import app as application

    return application


class TestRateLimitWiring:
    def test_middleware_is_registered(self, app):
        """
        结构断言：SlowAPIMiddleware 必须在栈里。

        这条直接盯住原来的 bug —— 光有 `app.state.limiter` 是不够的，
        `default_limits` 只有中间件会执行。
        """
        names = [m.cls.__name__ for m in app.user_middleware]
        assert "SlowAPIMiddleware" in names, (
            f"限流中间件没挂上，default_limits 不会生效。当前中间件栈: {names}"
        )

    def test_limiter_state_is_set(self, app):
        """中间件依赖 app.state.limiter，两者必须同时存在"""
        assert getattr(app.state, "limiter", None) is not None


class TestRateLimitEffective:
    def test_exceeding_limit_returns_429(self, monkeypatch):
        """
        功能断言：真的打满会 429。

        用一份**新建的 app**（限流阈值改成 3/min），避免污染共享的 app 实例
        和它的计数器。不用 `with TestClient(...)`，以免重复跑 lifespan。

        注意：`api/main.py` 里的 `app_settings` 是 `create_app` 内部的局部导入名，
        指向的仍是 `config.settings.settings` 这个单例 —— 所以直接改那个对象即可。
        """
        from api import main as main_mod
        from config.settings import settings

        monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
        limited_app = main_mod.create_app()
        client = TestClient(limited_app)

        codes = [client.get("/api/health").status_code for _ in range(6)]

        assert 429 in codes, f"打满 6 次都没触发限流，实际状态码: {codes}"
        # 前面的请求应该是通的，说明不是"全都 429"这种假阳性
        assert codes[0] == 200, f"第一次请求就被限了，阈值不对: {codes}"
