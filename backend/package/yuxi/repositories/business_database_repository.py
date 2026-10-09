"""原业务 MySQL 的只读访问边界。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pymysql
from pymysql.cursors import DictCursor


@dataclass(frozen=True)
class BusinessDatabaseConnection:
    """业务数据库连接参数。"""

    host: str
    port: int
    user: str
    password: str
    database: str
    query_timeout_ms: int


class BusinessDatabaseRepository:
    """使用只读会话读取原业务 MySQL。"""

    def __init__(self, config: BusinessDatabaseConnection):
        self.config = config

    def validate_staff_identity(self, tenant_id: int, staff_id: int) -> bool:
        """确认员工属于租户且记录仍有效。"""
        rows = self._internal_select(
            """
            SELECT ID
            FROM t_staff
            WHERE TENANT_ID = %s
              AND ID = %s
              AND OPERATER_STATUS > 0
            LIMIT 1
            """,
            (tenant_id, staff_id),
        )
        return bool(rows)

    def load_schema(self, table_names: frozenset[str]) -> dict[str, list[dict[str, Any]]]:
        """读取授权基础表的字段和注释。"""
        names = sorted(table_names)
        if not names:
            return {}
        placeholders = ", ".join(["%s"] * len(names))
        rows = self._internal_select(
            f"""
            SELECT
                c.TABLE_NAME,
                t.TABLE_COMMENT,
                c.COLUMN_NAME,
                c.COLUMN_TYPE,
                c.COLUMN_COMMENT,
                c.ORDINAL_POSITION
            FROM information_schema.columns AS c
            JOIN information_schema.tables AS t
              ON t.TABLE_SCHEMA = c.TABLE_SCHEMA
             AND t.TABLE_NAME = c.TABLE_NAME
            WHERE c.TABLE_SCHEMA = DATABASE()
              AND t.TABLE_TYPE = 'BASE TABLE'
              AND LOWER(c.TABLE_NAME) IN ({placeholders})
            ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
            """,
            tuple(names),
        )
        metadata: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            metadata.setdefault(str(row["TABLE_NAME"]).lower(), []).append(row)
        return metadata

    def list_project_ids(self, tenant_id: int, staff_id: int, *, allow_all_projects: bool) -> tuple[int, ...]:
        """返回当前身份可以访问的有效项目编号。"""
        if allow_all_projects:
            rows = self._internal_select(
                """
                SELECT DISTINCT p.ID
                FROM t_project AS p
                WHERE p.TENANT_ID = %s
                  AND p.OPERATER_STATUS > 0
                ORDER BY p.ID
                """,
                (tenant_id,),
            )
        else:
            rows = self._internal_select(
                """
                SELECT DISTINCT p.ID
                FROM t_project_user AS pu
                JOIN t_project AS p
                  ON p.TENANT_ID = pu.TENANT_ID
                 AND p.ID = pu.PROJECT_ID
                WHERE pu.TENANT_ID = %s
                  AND pu.STAFF_ID = %s
                  AND pu.OPERATER_STATUS > 0
                  AND p.OPERATER_STATUS > 0
                ORDER BY p.ID
                """,
                (tenant_id, staff_id),
            )
        return tuple(int(row["ID"]) for row in rows)

    def execute_query(self, sql: str) -> dict[str, Any]:
        """执行已经通过安全校验和重写的查询。"""
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(sql)
            rows = list(cursor.fetchall())
            fields = [item[0] for item in (cursor.description or [])]
        return {"row_count": len(rows), "fields": fields, "rows": rows}

    def _internal_select(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("内部业务数据库方法只允许 SELECT")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())

    @contextmanager
    def _connection(self) -> Iterator[pymysql.Connection]:
        timeout_seconds = max(1, (self.config.query_timeout_ms + 999) // 1000)
        connection = pymysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            database=self.config.database,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=DictCursor,
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
            write_timeout=timeout_seconds,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {self.config.query_timeout_ms}")
            yield connection
        finally:
            connection.close()
