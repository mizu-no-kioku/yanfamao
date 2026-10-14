"""租户上下文的语义，以及运行时角色的语句形状。

上下文语义不碰数据库（真库上「注入挂在事务边界」的行为由
test_tenant_backfill.py 的集成用例覆盖）。
"""

from __future__ import annotations

import pytest

from yuxi.storage.postgres.manager import (
    TENANT_ROLE_GRANT_SCHEMA_STATEMENTS,
    PostgresManager,
    _tenant_role_statements,
)
from yuxi.storage.postgres.tenant_context import (
    DEFAULT_TENANT_ID,
    current_tenant_id,
    explicit_tenant_id,
    use_tenant,
)

pytestmark = [pytest.mark.unit]


class _RecordingConnection:
    """记录 `set_config` 注入调用的假连接。"""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[dict] = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        self.params.append(params or {})


def _bare_manager() -> PostgresManager:
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    return manager


def test_apply_tenant_context_skips_injection_without_an_explicit_tenant():
    """缺显式上下文时**不注入**默认租户：会话的 `app.tenant_id` 保持未设置 → RLS fail-closed。

    这是"缺上下文 ≠ 租户 1"的落点：不再靠回落把"忘了建立上下文"变成"以租户 1 运行"。
    """
    manager = _bare_manager()
    applied: list[int] = []
    manager.on_tenant_context_applied = applied.append
    connection = _RecordingConnection()

    manager._apply_tenant_context(None, None, connection)

    assert connection.statements == []
    assert applied == []


def test_apply_tenant_context_injects_the_explicit_tenant():
    manager = _bare_manager()
    applied: list[int] = []
    manager.on_tenant_context_applied = applied.append
    connection = _RecordingConnection()

    with use_tenant(9):
        manager._apply_tenant_context(None, None, connection)

    assert len(connection.statements) == 1
    assert "set_config('app.tenant_id'" in connection.statements[0]
    assert connection.params[0] == {"tenant_id": "9"}
    assert applied == [9]


def test_explicit_tenant_is_unset_by_default_and_never_falls_back():
    """缺上下文返回 `None`，**不**回落默认租户——这是运行时数据库会话的唯一租户来源。

    回落会让"忘了建立上下文"静默变成"以租户 1 运行"；`_apply_tenant_context` 据此在缺上下文时
    **跳过注入**，RLS 因此按"无租户"处理（读 0 行、写被拒）。
    """
    assert explicit_tenant_id() is None
    with use_tenant(7):
        assert explicit_tenant_id() == 7
    assert explicit_tenant_id() is None


def test_current_tenant_falls_back_to_the_default_tenant():
    """`current_tenant_id()` 只服务 ORM **列默认值**（建表 / SQLite 单测 / 迁移）。

    它不是运行时数据库会话的租户来源（那个用 `explicit_tenant_id()`）。不能改成返回 None：
    `tenant_id` 已是 NOT NULL，回落成 None 会让这些没有会话上下文的插入直接失败。
    """
    assert current_tenant_id() == DEFAULT_TENANT_ID
    with use_tenant(7):
        assert current_tenant_id() == 7
    assert current_tenant_id() == DEFAULT_TENANT_ID


def test_use_tenant_scopes_and_restores_the_context():
    with use_tenant(7):
        assert current_tenant_id() == 7
        with use_tenant(9):
            assert current_tenant_id() == 9
        assert current_tenant_id() == 7

    assert current_tenant_id() == DEFAULT_TENANT_ID


def test_role_statements_create_a_non_bypassing_login_role():
    """superuser 与 BYPASSRLS 角色永远绕过 RLS，连 FORCE 也压不住；这两条属性是保证。"""
    statements = "\n".join((*_tenant_role_statements(), *TENANT_ROLE_GRANT_SCHEMA_STATEMENTS))

    assert "CREATE ROLE yuxi_app LOGIN" in statements
    assert "NOSUPERUSER" in statements
    assert "NOBYPASSRLS" in statements


def test_role_grants_cover_every_table_not_only_ownership_roots():
    """应用要查 messages / conversations 等派生表；只授归属根会全站 permission denied。"""
    statements = "\n".join(TENANT_ROLE_GRANT_SCHEMA_STATEMENTS)

    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO yuxi_app" in statements
    # 迁移之后新建的表也要能访问，否则下一次加表就 permission denied。
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public" in statements
