"""
鉴权要求端点测试
================

`GET /api/auth/requirements` 存在的意义是**让注册页不必再抄一份校验规则**：
前端照着它渲染实时校验清单、并据此决定要不要显示"未开放注册"。

所以这里除了测端点本身，还要守住一条**一致性**：
端点上报的"密码最小长度"必须等于真正生效的那条规则
（`core.auth.validate_password_strength` 用的 `settings.password_min_length`）。
一旦有人只改了其中一处，这些用例会红。
"""

import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from core.auth import validate_password_strength
from core.schemas import PASSWORD_MIN_LENGTH


@pytest.fixture(scope="module")
def client():
    from api.main import app

    with TestClient(app) as c:
        yield c


class TestAuthRequirements:
    def test_endpoint_is_public(self, client):
        """登录页要用它，所以**必须**不需要鉴权"""
        resp = client.get("/api/auth/requirements")
        assert resp.status_code == 200

    def test_reports_effective_password_rule(self, client):
        """
        上报的最小长度要等于真正生效的规则。

        这条是核心：如果只改 settings.password_min_length 而忘了别的地方，
        或者反过来，这里就会失败。
        """
        data = client.get("/api/auth/requirements").json()
        assert data["password_min_length"] == settings.password_min_length

        # 构造一个刚好达标的密码，确认处理器认可它
        ok_pwd = "a" * (settings.password_min_length - 1) + "1"
        assert validate_password_strength(ok_pwd) is None

        # 再短一位就该被拒，且**拒绝理由要与上报的最小长度一致**
        too_short = "a" * (settings.password_min_length - 2) + "1"
        error = validate_password_strength(too_short)
        assert error is not None
        assert str(settings.password_min_length) in error

    def test_reports_registration_switch(self, client):
        data = client.get("/api/auth/requirements").json()
        assert data["allow_registration"] == settings.allow_registration

    def test_reports_char_class_rules(self, client):
        """数字/字母两条要求：报 True 就和处理器实际行为对得上"""
        data = client.get("/api/auth/requirements").json()

        if data["password_require_digit"]:
            assert validate_password_strength("a" * PASSWORD_MIN_LENGTH) is not None
        if data["password_require_letter"]:
            assert validate_password_strength("1" * PASSWORD_MIN_LENGTH) is not None

    def test_length_constants_are_single_sourced(self):
        """
        schema 的密码下限必须等于配置里的下限。

        原先是 schema 写死 6、处理器要求 8 —— 6~7 位的密码能过 schema
        却被处理器打回。这条用例守住它们不再分家。
        """
        assert settings.password_min_length == PASSWORD_MIN_LENGTH
