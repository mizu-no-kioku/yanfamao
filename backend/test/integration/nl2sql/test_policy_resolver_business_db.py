"""真实业务库只读对账。必须显式提供绑定用户才运行。

负向属性：本模块绝不写入业务库。
"""

import pytest


def _business_config():
    from yuxi.services.business_database_service import load_business_database_config

    try:
        return load_business_database_config()
    except PermissionError:
        # 仅“变量缺失”可用作跳过理由；配置存在但格式错误会让 ValueError 冒泡成失败。
        return None


@pytest.fixture
def business_directory():
    config = _business_config()
    if config is None:
        pytest.skip("未配置 YUXI_BUSINESS_DATABASE_CONFIG")
    from yuxi.nl2sql.mysql_directory import business_directory_from_env

    directory = business_directory_from_env()
    assert directory is not None
    return directory


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
    """两者不得共用同一个环境变量。"""
    from yuxi.nl2sql.test_database import TEST_CONFIG_ENV

    assert TEST_CONFIG_ENV != "YUXI_BUSINESS_DATABASE_CONFIG"


def test_malformed_business_config_fails_instead_of_skipping(monkeypatch):
    """配置存在但非法时必须失败，不得被当作“未配置”而跳过。

    否则部署管理员写错一个字符，就会得到一个绿色 skipped 套件、却从未触达生产库。
    只有“变量缺失”（PermissionError）才允许降级为 None；格式错误（ValueError）必须冒泡。
    """
    from yuxi.nl2sql.mysql_directory import business_directory_from_env
    from yuxi.services.business_database_service import load_business_database_config

    monkeypatch.setenv("YUXI_BUSINESS_DATABASE_CONFIG", "{ not json")

    with pytest.raises(ValueError):
        load_business_database_config()
    with pytest.raises(ValueError):
        _business_config()
    with pytest.raises(ValueError):
        business_directory_from_env()

    monkeypatch.delenv("YUXI_BUSINESS_DATABASE_CONFIG", raising=False)

    with pytest.raises(PermissionError):
        load_business_database_config()
    assert _business_config() is None
    assert business_directory_from_env() is None


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
