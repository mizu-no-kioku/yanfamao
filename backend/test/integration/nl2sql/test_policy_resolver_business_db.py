"""真实业务库只读对账。必须显式提供绑定用户才运行。

负向属性：本模块绝不写入业务库。
"""

import pytest


@pytest.fixture
def business_directory():
    import os

    if os.environ.get("YUXI_NL2SQL_BUSINESS_TEST") != "1":
        pytest.skip("Set YUXI_NL2SQL_BUSINESS_TEST=1 for read-only business verification")
    from yuxi.nl2sql.mysql_directory import business_directory_from_env

    return business_directory_from_env()


def test_business_account_cannot_write(business_directory):
    """生产账号只有 SELECT；这里证明它确实写不了。"""
    import pymysql

    from yuxi.nl2sql.mysql_directory import business_connection_factory

    connection = business_connection_factory()()
    try:
        with connection.cursor() as cursor:
            with pytest.raises(pymysql.err.OperationalError):
                cursor.execute("CREATE TEMPORARY TABLE nl2sql_probe_write (ID INT)")
    finally:
        connection.close()


def test_test_database_config_is_independent_from_business_config():
    """两者不得共用同一个环境变量，也不得回退到业务库配置。"""
    from yuxi.nl2sql.test_database import TEST_CONFIG_ENV

    assert TEST_CONFIG_ENV == "YUXI_NL2SQL_TEST_DATABASE_CONFIG"
    assert not TEST_CONFIG_ENV.startswith("YUXI_BUSINESS_DB_")


def test_malformed_business_config_fails_instead_of_skipping(monkeypatch):
    from yuxi.nl2sql.mysql_directory import business_directory_from_env
    from yuxi.services.business_database_service import BusinessDatabaseConfigurationError

    for name in ("HOST", "USER", "PASSWORD", "NAME"):
        monkeypatch.setenv("YUXI_BUSINESS_DB_" + name, "test")
    monkeypatch.setenv("YUXI_BUSINESS_DB_PORT", "invalid")
    with pytest.raises(BusinessDatabaseConfigurationError):
        business_directory_from_env()
    monkeypatch.delenv("YUXI_BUSINESS_DB_HOST")
    with pytest.raises(BusinessDatabaseConfigurationError):
        business_directory_from_env()


def test_trial_identity_chain_resolves_on_business_db(business_directory):
    """绑定试用主体：租户 25 / 员工 96，角色与菜单必须能解析出来。"""
    from yuxi.nl2sql.policy_resolver import BusinessSubject, resolve_function_gate

    subject = BusinessSubject(user_id=17, tenant_id=25, staff_id=96)
    roles = resolve_function_gate(subject, business_directory)
    assert len(roles) == 1


def test_trial_identity_project_scope_matches_independent_query(business_directory):
    """授权项目数必须与独立成员关系查询一致（2026-10-10 实测为 14）。"""
    from yuxi.nl2sql.policy_resolver import resolve_query_policy
    from yuxi.nl2sql.principal import BusinessPrincipal

    resolved = resolve_query_policy(BusinessPrincipal(user_id=17, tenant_id=25), business_directory)
    assert resolved.policy.staff_id == 96
    assert len(resolved.policy.allowed_project_ids) == 14
