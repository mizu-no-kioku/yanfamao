"""隔离测试库上的端到端解析：行范围、跨租户、上限。"""

import pytest

from yuxi.nl2sql import errors
from yuxi.nl2sql.mysql_directory import MySqlBusinessDirectory
from yuxi.nl2sql.policy_resolver import resolve_query_policy
from yuxi.nl2sql.principal import BusinessPrincipal
from yuxi.nl2sql.test_database import require_test_database


@pytest.fixture
def directory():
    from yuxi.nl2sql.mysql_directory import test_database_connection_factory

    config = require_test_database()
    return MySqlBusinessDirectory(test_database_connection_factory(config))


def test_resolves_full_policy_for_single_tenant_user(directory):
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), directory)
    assert resolved.policy.tenant_id == 10
    assert resolved.policy.staff_id == 2001
    assert resolved.policy.allowed_project_ids == (5001, 5002)  # 5003 失效、5004 他租户


def test_cross_tenant_user_requires_explicit_tenant(directory):
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_query_policy(BusinessPrincipal(user_id=1004), directory)
    assert exc.value.code == errors.TENANT_AMBIGUOUS


def test_cross_tenant_user_resolves_with_explicit_tenant(directory):
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1004, tenant_id=20), directory)
    assert resolved.policy.tenant_id == 20
    assert resolved.policy.staff_id == 2005


def test_ambiguous_staff_is_denied(directory):
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_query_policy(BusinessPrincipal(user_id=1005), directory)
    assert exc.value.code == errors.IDENTITY_STAFF_AMBIGUOUS


def test_inactive_user_is_denied(directory):
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_query_policy(BusinessPrincipal(user_id=1006), directory)
    assert exc.value.code == errors.IDENTITY_USER_MISSING


def test_resolved_policy_blocks_cross_tenant_sql(directory):
    """解析器产出的策略交给既有 SQLSecurityGuard：租户与项目条件由守卫注入。"""
    from yuxi.services.business_database_security import SQLSecurityGuard

    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), directory)
    secured = SQLSecurityGuard(resolved.policy).secure("SELECT ID FROM t_project")
    normalized = " ".join(secured.final_sql.lower().split())
    assert "tenant_id = 10" in normalized
    # 项目范围必须真正落到改写后的 SQL：两个授权项目（5001/5002）以 IN 列表限制
    # t_project.ID。若 project_scope_columns 意外为空，强制层会跳过该项目谓词，
    # 本断言随即变红——这正是本条负向控制的目的。
    assert "in (5001, 5002)" in normalized, secured.final_sql


def test_model_cannot_reference_guard_internal_columns(directory):
    """守卫内部列（租户/状态）模型不得引用——即便只是写进 WHERE 也要拒绝。"""
    from yuxi.services.business_database_security import PolicyViolation, SQLSecurityGuard

    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), directory)
    for sql in (
        "SELECT ID FROM t_project WHERE TENANT_ID = 99",
        "SELECT ID FROM t_project WHERE OPERATER_STATUS > 0",
    ):
        with pytest.raises(PolicyViolation) as exc:
            SQLSecurityGuard(resolved.policy).secure(sql)
        assert exc.value.code == "COLUMN_NOT_ALLOWED"


def test_resolved_policy_rejects_permanently_denied_table(directory):
    from yuxi.services.business_database_security import PolicyViolation, SQLSecurityGuard

    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), directory)
    with pytest.raises(PolicyViolation) as exc:
        SQLSecurityGuard(resolved.policy).secure("SELECT PASSWORD FROM t_user")
    # 必须钉住具体违规码：t_user 因永久拒绝表先被拦，不能靠其他分支（如 DENIED_COLUMN）
    # 偶然变绿。首期两表的字段上限里没有永久拒绝字段，DENIED_COLUMN 路径
    # 无法用现表集端到端触发。
    assert exc.value.code == "DENIED_TABLE"
