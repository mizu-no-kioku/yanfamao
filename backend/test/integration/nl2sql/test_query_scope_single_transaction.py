"""一次业务库工具调用必须落在同一条只读连接、同一个只读事务上。

用隔离测试库（可写控制账号）真实撤销角色来验证撤权边界——真实业务库只读，
无法在其中制造撤权。本模块不得触碰 r_d_test。
"""

from __future__ import annotations

import pymysql
import pytest

from yuxi.nl2sql.test_database import require_test_database
from yuxi.repositories.business_database_repository import BusinessDatabaseConnection
from yuxi.services.business_database_service import (
    BusinessDatabaseAuthorizationError,
    BusinessDatabaseService,
    BusinessDatabaseSettings,
    mysql_query_scope,
)

pytestmark = pytest.mark.integration

SEEDED_USER_ID = 1001  # 租户 10 / 员工 2001 / 角色 3001，种子里带 AI 功能菜单
SEEDED_ROLE_ID = 3001


class RecordingCursor:
    """把语句记到所属连接的记录簿上。"""

    def __init__(self, real, sink: list[str]) -> None:
        self._real = real
        self._sink = sink

    def execute(self, sql, *args, **kwargs):
        self._sink.append(sql)
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


class RecordingConnection:
    """记录这条连接上执行过的语句；语句归属同一记录簿即同一连接同一事务。"""

    def __init__(self, real) -> None:
        self._real = real
        self.statements: list[str] = []
        self.closed = False

    def cursor(self, *args, **kwargs):
        return RecordingCursor(self._real.cursor(*args, **kwargs), self.statements)

    def close(self) -> None:
        self.closed = True
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _connect_to_test_db(config):
    def connect():
        return pymysql.connect(
            host=config.host,
            port=config.port,
            user=config.app_user,
            password=config.app_password.get_secret_value(),
            database=config.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.Cursor,
            connect_timeout=5,
            read_timeout=15,
        )

    return connect


def _control_execute(config, sql: str) -> None:
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
def service_and_recorder():
    config = require_test_database()
    settings = BusinessDatabaseSettings(
        connection=BusinessDatabaseConnection(
            host=config.host,
            port=config.port,
            user=config.app_user,
            password=config.app_password.get_secret_value(),
            database=config.database,
            query_timeout_ms=10_000,
        ),
        identities={
            "probe-uid": {"business_user_id": SEEDED_USER_ID, "tenant_id": 10},
            # 已绑定但业务库中不存在：作用域会打开，解析在事务内失败。
            "bad-uid": {"business_user_id": 999999, "tenant_id": 10},
        },
        max_rows=200,
    )
    created: list[RecordingConnection] = []

    def counting_connect():
        wrapped = RecordingConnection(_connect_to_test_db(config)())
        created.append(wrapped)
        return wrapped

    service = BusinessDatabaseService(
        settings_loader=lambda: settings,
        query_scope=lambda connection: mysql_query_scope(connection, connect=counting_connect),
    )
    _set_role_status(config, 1)
    try:
        yield service, created, config
    finally:
        _set_role_status(config, 1)


def test_single_query_uses_one_connection_and_one_transaction(service_and_recorder):
    """解析、Schema 读取与最终 SQL 落在同一条连接上，且只开启一个事务。"""
    service, created, _config = service_and_recorder

    service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project")

    assert len(created) == 1, f"一次查询只应创建一条连接，实际 {len(created)} 条"
    statements = created[0].statements
    starts = [s for s in statements if "START TRANSACTION" in s.upper()]
    assert len(starts) == 1, f"只应开启一个事务，实际 {len(starts)} 个"

    # 最终业务 SQL 记在同一本记录簿上 ⇒ 与解析同连接、同事务。
    final = [s for s in statements if s.strip().upper().startswith("SELECT ID FROM T_PROJECT")]
    assert final, f"最终业务 SQL 未记录在本连接的语句序列中：{statements}"


def test_revocation_is_denied_on_the_next_call(service_and_recorder):
    """撤权后，下一次调用开启新事务即可见，返回 ROLE_INACTIVE。"""
    service, _created, config = service_and_recorder

    assert service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project")["row_count"] > 0

    _set_role_status(config, 0)

    with pytest.raises(BusinessDatabaseAuthorizationError) as exc:
        service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project")
    assert "ROLE_INACTIVE" in str(exc.value)


def test_restoring_the_role_restores_access(service_and_recorder):
    service, _created, config = service_and_recorder

    service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project")
    _set_role_status(config, 0)
    with pytest.raises(BusinessDatabaseAuthorizationError):
        service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project")

    _set_role_status(config, 1)
    assert service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project")["row_count"] > 0


def test_sql_rejection_executes_no_business_query(service_and_recorder):
    """被守卫拒绝时，最终业务查询执行次数为 0。"""
    service, created, _config = service_and_recorder

    with pytest.raises(Exception):
        service.execute_authorized_query("probe-uid", "SELECT ID FROM t_project WHERE TENANT_ID = 999")

    business = [s for s in created[0].statements if "TENANT_ID = 999" in s]
    assert business == [], f"被拒绝的 SQL 不得执行：{business}"


def test_connection_is_closed_when_sql_is_rejected(service_and_recorder):
    service, created, _config = service_and_recorder

    with pytest.raises(Exception):
        service.execute_authorized_query("probe-uid", "DELETE FROM t_project")

    assert created[0].closed, "SQL 被拒绝时也必须关闭连接"


def test_connection_is_closed_when_resolution_fails(service_and_recorder):
    """已在事务内解析失败时，连接同样必须关闭。"""
    service, created, _config = service_and_recorder

    with pytest.raises(BusinessDatabaseAuthorizationError):
        service.execute_authorized_query("bad-uid", "SELECT ID FROM t_project")

    assert created, "应已打开连接（绑定存在，失败发生在业务库解析阶段）"
    assert created[0].closed, "解析失败时也必须关闭连接"
