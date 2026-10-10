"""测试库配置只从专用环境变量读取；缺失时跳过，绝不回退业务库。"""

import pytest

from yuxi.nl2sql import test_database


def test_missing_config_returns_none(monkeypatch):
    monkeypatch.delenv(test_database.TEST_CONFIG_ENV, raising=False)
    assert test_database.load_test_database_config() is None


def test_malformed_config_returns_none(monkeypatch):
    monkeypatch.setenv(test_database.TEST_CONFIG_ENV, "{not json")
    assert test_database.load_test_database_config() is None


def test_require_skips_instead_of_falling_back(monkeypatch):
    monkeypatch.delenv(test_database.TEST_CONFIG_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        test_database.require_test_database()


def test_config_never_reuses_business_database_env(monkeypatch):
    """业务库配置存在也不得被当作测试库。"""
    monkeypatch.setenv("YUXI_BUSINESS_DATABASE_CONFIG", '{"host":"business.invalid","port":3306,'
                                                       '"user":"u","password":"p","database":"d","bindings":{}}')
    monkeypatch.delenv(test_database.TEST_CONFIG_ENV, raising=False)
    assert test_database.load_test_database_config() is None
