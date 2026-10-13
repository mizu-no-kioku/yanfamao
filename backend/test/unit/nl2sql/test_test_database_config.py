"""测试库配置只从专用环境变量读取；缺失时跳过，绝不回退业务库。"""

import pytest
from yuxi.nl2sql import test_database


def test_missing_config_returns_none(monkeypatch):
    monkeypatch.delenv(test_database.TEST_CONFIG_ENV, raising=False)
    assert test_database.load_test_database_config() is None


def test_malformed_config_fails(monkeypatch):
    monkeypatch.setenv(test_database.TEST_CONFIG_ENV, "{not json")
    with pytest.raises(ValueError, match="Invalid isolated"):
        test_database.load_test_database_config()
    with pytest.raises(ValueError):
        test_database.require_test_database()


def test_require_skips_instead_of_falling_back(monkeypatch):
    monkeypatch.delenv(test_database.TEST_CONFIG_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        test_database.require_test_database()


def test_config_never_reuses_business_database_env(monkeypatch):
    """业务库配置存在也不得被当作测试库。

    这里用**当前**的业务库配置变量名，而不是已废弃的 `YUXI_BUSINESS_DATABASE_CONFIG`：
    用废弃名会让这条用例在业务库改名后变成永真断言。
    """
    monkeypatch.setenv("YUXI_BUSINESS_DB_HOST", "business.invalid")
    monkeypatch.setenv("YUXI_BUSINESS_DB_USER", "u")
    monkeypatch.setenv("YUXI_BUSINESS_DB_PASSWORD", "p")
    monkeypatch.setenv("YUXI_BUSINESS_DB_NAME", "d")
    monkeypatch.setenv("YUXI_BUSINESS_DB_IDENTITIES_JSON", '{"uid":{"business_user_id":1}}')
    monkeypatch.delenv(test_database.TEST_CONFIG_ENV, raising=False)
    assert test_database.load_test_database_config() is None
