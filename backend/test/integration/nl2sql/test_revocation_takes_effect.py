"""撤权必须在**下一次执行前**重新解析并拒绝，而不是沿用上次的策略。

用隔离测试库（可写控制账号）真实撤销角色后再查询——真实业务库是只读的，
无法在其中制造撤权。本模块不得触碰 r_d_test。
"""

from __future__ import annotations

import pymysql
import pytest

from yuxi.nl2sql.mysql_directory import MySqlBusinessDirectory
from yuxi.nl2sql.test_database import require_test_database
from yuxi.repositories.business_database_repository import BusinessDatabaseConnection
from yuxi.services.business_database_service import (
    BusinessDatabaseService,
    BusinessDatabaseSettings,
)

pytestmark = pytest.mark.integration

SEEDED_USER_ID = 1001  # 租户 10 / 员工 2001 / 角色 3001，种子里带 AI 功能菜单
SEEDED_ROLE_ID = 3001


def _control_execute(config, sql: str) -> None:
    """用可写控制账号改测试库状态（仅在隔离测试库上）。"""
    connection = pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.control_user,
        password=config.control_password.get_secret_value(),
        database=config.database,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
    finally:
        connection.close()


def _set_role_status(config, status: int) -> None:
    _control_execute(config, f"UPDATE t_enterprise_role SET OPERATER_STATUS = {status} WHERE ID = {SEEDED_ROLE_ID}")


@pytest.fixture
def service_and_config():
    config = require_test_database()
    connection = BusinessDatabaseConnection(
        host=config.host,
        port=config.port,
        user=config.app_user,
        password=config.app_password.get_secret_value(),
        database=config.database,
        query_timeout_ms=10_000,
    )

    def directory_factory(_connection):
        def connect():
            return pymysql.connect(
                host=config.host,
                port=config.port,
                user=config.app_user,
                password=config.app_password.get_secret_value(),
                database=config.database,
                charset="utf8mb4",
                connect_timeout=5,
                read_timeout=15,
            )

        return MySqlBusinessDirectory(connect)

    settings = BusinessDatabaseSettings(
        connection=connection,
        identities={"probe-uid": {"business_user_id": SEEDED_USER_ID, "tenant_id": 10}},
        max_rows=200,
    )
    service = BusinessDatabaseService(
        settings_loader=lambda: settings,
        directory_factory=directory_factory,
    )
    _set_role_status(config, 1)
    try:
        yield service, config
    finally:
        _set_role_status(config, 1)


def test_revocation_is_denied_on_the_next_query(service_and_config):
    """授权时能查；撤销角色后再查必须重新解析并拒绝。"""
    service, config = service_and_config

    before = service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project")
    assert before["row_count"] > 0

    _set_role_status(config, 0)

    with pytest.raises(Exception) as exc:
        service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project")
    assert "ROLE_INACTIVE" in str(exc.value)


def test_restoring_the_role_restores_access(service_and_config):
    """撤权不是单向的：角色恢复后应重新可用，证明拒绝来自实时解析而非缓存。"""
    service, config = service_and_config

    service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project")
    _set_role_status(config, 0)
    with pytest.raises(Exception):
        service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project")

    _set_role_status(config, 1)
    after = service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project")
    assert after["row_count"] > 0
