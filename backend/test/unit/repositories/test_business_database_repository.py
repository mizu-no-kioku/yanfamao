from __future__ import annotations

import pytest

from yuxi.repositories import business_database_repository as repository_module
from yuxi.repositories.business_database_repository import BusinessDatabaseConnection, BusinessDatabaseRepository

pytestmark = pytest.mark.unit


class FakeCursor:
    """记录会话设置和查询的最小游标。"""

    def __init__(self):
        self.executions = []
        self.description = [("ID",)]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executions.append((" ".join(sql.split()), params))

    def fetchall(self):
        return [{"ID": 2}]


class FakeConnection:
    """复用同一游标以观察 repository 的完整调用顺序。"""

    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


def test_repository_sets_read_only_timeout_before_query(monkeypatch) -> None:
    """最终查询前必须先把 MySQL 会话设置为只读并设置超时。"""
    connection = FakeConnection()
    connect_calls = []

    def fake_connect(**kwargs):
        connect_calls.append(kwargs)
        return connection

    monkeypatch.setattr(repository_module.pymysql, "connect", fake_connect)
    repository = BusinessDatabaseRepository(
        BusinessDatabaseConnection(
            host="db.example.test",
            port=3306,
            user="readonly",
            password="placeholder",
            database="business",
            query_timeout_ms=2500,
        )
    )

    result = repository.execute_query("SELECT ID FROM t_project LIMIT 1")

    assert [item[0] for item in connection.cursor_instance.executions] == [
        "SET SESSION TRANSACTION READ ONLY",
        "SET SESSION MAX_EXECUTION_TIME = 2500",
        "SELECT ID FROM t_project LIMIT 1",
    ]
    assert connect_calls[0]["autocommit"] is True
    assert result == {"row_count": 1, "fields": ["ID"], "rows": [{"ID": 2}]}
    assert connection.closed is True
