"""租户上下文的语义，以及运行时角色的语句形状。

上下文语义不碰数据库（真库上「注入挂在事务边界」的行为由
test_tenant_backfill.py 的集成用例覆盖）。
"""

from __future__ import annotations

import pytest

from yuxi.storage.postgres.manager import (
    TENANT_ROLE_GRANT_SCHEMA_STATEMENTS,
    _tenant_role_statements,
)
from yuxi.storage.postgres.tenant_context import DEFAULT_TENANT_ID, current_tenant_id, use_tenant

pytestmark = [pytest.mark.unit]


def test_current_tenant_falls_back_to_the_default_tenant():
    """没有显式上下文时回落默认租户——单租户部署与未接入认证前的所有会话靠它。

    不能改成返回 None：tenant_id 已是 NOT NULL，回落成 None 会让所有插入失败。
    """
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
