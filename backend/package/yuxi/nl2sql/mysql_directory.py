"""BusinessDirectory 的 MySQL 实现。

每次查询各自开一条新连接、用完即关；一次 resolve_query_policy 的多步读取
来自不同时刻，不存在共享快照。解析内部的单事务一致性需 Plan 2 替换本接口，
本层不得声称以只读事务运行。

关键：角色基数走 tb_user_enterprise_role_tenant 的原始行，
不 JOIN 角色—菜单表——否则一个多菜单角色会被放大成上百行。
"""

from collections.abc import Callable, Sequence

import pymysql

from yuxi.nl2sql.test_database import TestDatabaseConfig

ConnectionFactory = Callable[[], "pymysql.connections.Connection"]


def test_database_connection_factory(config: TestDatabaseConfig) -> ConnectionFactory:
    """隔离测试库的只读应用账号连接。"""

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


class MySqlBusinessDirectory:
    def __init__(self, connect: ConnectionFactory):
        self._connect = connect

    def _open(self):
        return self._connect()

    def find_active_user(self, user_id: int) -> bool:
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM t_user WHERE ID = %s AND OPERATER_STATUS > 0 LIMIT 1",
                    (user_id,),
                )
                return cursor.fetchone() is not None
        finally:
            connection.close()

    def find_tenants_for_user(self, user_id: int) -> tuple[int, ...]:
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT TENANT_ID FROM tb_user_enterprise_role_tenant "
                    "WHERE USER_ID = %s ORDER BY TENANT_ID",
                    (user_id,),
                )
                return tuple(row[0] for row in cursor.fetchall())
        finally:
            connection.close()

    def find_active_staff_ids(self, user_id: int, tenant_id: int) -> tuple[int, ...]:
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT ID FROM t_staff WHERE BELONG_USER_ID = %s AND TENANT_ID = %s "
                    "AND OPERATER_STATUS > 0 ORDER BY ID",
                    (user_id, tenant_id),
                )
                return tuple(row[0] for row in cursor.fetchall())
        finally:
            connection.close()

    def find_role_relation_ids(self, user_id: int, tenant_id: int) -> tuple[int, ...]:
        """原始关系行。不得 JOIN 角色—菜单表：那会按菜单数放大行数。"""
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT ENTERPRISE_ROLE_ID FROM tb_user_enterprise_role_tenant "
                    "WHERE TENANT_ID = %s AND USER_ID = %s",
                    (tenant_id, user_id),
                )
                return tuple(row[0] for row in cursor.fetchall())
        finally:
            connection.close()

    def find_active_role_ids(self, role_ids: Sequence[int]) -> tuple[int, ...]:
        if not role_ids:
            return ()
        placeholders = ", ".join(["%s"] * len(role_ids))
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT ID FROM t_enterprise_role WHERE ID IN ({placeholders}) "
                    "AND OPERATER_STATUS > 0 ORDER BY ID",
                    tuple(role_ids),
                )
                return tuple(row[0] for row in cursor.fetchall())
        finally:
            connection.close()

    def find_menu_codes(self, role_ids: Sequence[int], tenant_id: int) -> frozenset[str]:
        if not role_ids:
            return frozenset()
        placeholders = ", ".join(["%s"] * len(role_ids))
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT m.CODE FROM tb_enterprise_role_menu_tenant rm "
                    "JOIN t_enterprise_menu m ON m.ID = rm.ENTERPRISE_MENU_ID "
                    f"WHERE rm.TENANT_ID = %s AND rm.ENTERPRISE_ROLE_ID IN ({placeholders}) "
                    "AND m.OPERATER_STATUS > 0",
                    (tenant_id, *role_ids),
                )
                return frozenset(row[0] for row in cursor.fetchall() if row[0])
        finally:
            connection.close()

    def find_menu_ids(self, role_ids: Sequence[int], tenant_id: int) -> frozenset[int]:
        """与 find_menu_codes 同一 JOIN 与租户范围，取菜单 ID 供功能门判 ID。"""
        if not role_ids:
            return frozenset()
        placeholders = ", ".join(["%s"] * len(role_ids))
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT m.ID FROM tb_enterprise_role_menu_tenant rm "
                    "JOIN t_enterprise_menu m ON m.ID = rm.ENTERPRISE_MENU_ID "
                    f"WHERE rm.TENANT_ID = %s AND rm.ENTERPRISE_ROLE_ID IN ({placeholders}) "
                    "AND m.OPERATER_STATUS > 0",
                    (tenant_id, *role_ids),
                )
                return frozenset(row[0] for row in cursor.fetchall() if row[0] is not None)
        finally:
            connection.close()

    def find_authorized_project_ids(self, tenant_id: int, staff_id: int) -> tuple[int, ...]:
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT p.ID FROM t_project_user pu "
                    "JOIN t_project p ON p.ID = pu.PROJECT_ID AND p.TENANT_ID = pu.TENANT_ID "
                    "WHERE pu.TENANT_ID = %s AND pu.STAFF_ID = %s "
                    "AND pu.OPERATER_STATUS > 0 AND p.OPERATER_STATUS > 0 ORDER BY p.ID",
                    (tenant_id, staff_id),
                )
                return tuple(row[0] for row in cursor.fetchall())
        finally:
            connection.close()


def business_connection_factory() -> ConnectionFactory:
    """真实业务库连接。账号只有 SELECT，本模块只发查询，永不写入。"""
    from yuxi.services.business_database_service import BusinessDatabaseSettings

    config = BusinessDatabaseSettings.from_env().connection

    def connect():
        return pymysql.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.Cursor,
            connect_timeout=5,
            read_timeout=15,
        )

    return connect


def business_directory_from_env() -> MySqlBusinessDirectory:
    """Build a read-only directory using the existing deployment configuration."""
    return MySqlBusinessDirectory(business_connection_factory())
