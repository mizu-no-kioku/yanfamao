"""解析器产出完整 QueryPolicy：表、字段、行范围与上限全部来自代码裁决。"""

import pytest

from yuxi.nl2sql import errors
from yuxi.nl2sql.policy_resolver import resolve_query_policy
from yuxi.nl2sql.principal import BusinessPrincipal


class FakeDirectory:
    def __init__(self, *, projects=(5001, 5002)):
        self.projects = tuple(projects)

    def find_active_user(self, user_id):
        return True

    def find_tenants_for_user(self, user_id):
        return (10,)

    def find_active_staff_ids(self, user_id, tenant_id):
        return (2001,)

    def find_role_relation_ids(self, user_id, tenant_id):
        return (3001,)

    def find_active_role_ids(self, role_ids):
        return (3001,)

    def find_menu_codes(self, role_ids, tenant_id):
        return frozenset({"AI"})

    def find_menu_ids(self, role_ids, tenant_id):
        return frozenset({253})

    def find_authorized_project_ids(self, tenant_id, staff_id):
        return self.projects


def test_policy_has_tenant_and_staff_from_subject():
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    assert resolved.policy.tenant_id == 10
    assert resolved.policy.staff_id == 2001


def test_policy_tables_come_from_resource_grants():
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    assert resolved.policy.allowed_tables == frozenset({"t_project", "t_project_user"})


def test_policy_project_scope_columns_match_table_bounds():
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    assert dict(resolved.policy.project_scope_columns) == {"t_project": "ID", "t_project_user": "PROJECT_ID"}


def test_policy_carries_authorized_projects():
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(projects=(5001, 5002)))
    assert resolved.policy.allowed_project_ids == (5001, 5002)


def test_no_authorized_project_is_denied():
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(projects=()))
    assert exc.value.code == errors.NO_PROJECT_ACCESS


def test_policy_carries_permanent_denials():
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    assert "t_user" in resolved.policy.denied_tables
    assert "PASSWORD" in resolved.policy.denied_columns


def test_policy_row_cap_never_exceeds_200():
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    assert resolved.policy.max_rows <= 200


def test_policy_always_sets_selectable_columns_explicitly():
    """必须显式给 selectable_columns。

    守卫的实现是 `selectable_columns or table_columns`：留空会回退到 table_columns，
    状态列随之重新暴露给模型——这正是 2026-10-12 那个「答 0」缺陷的成因。
    """
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    assert resolved.policy.selectable_columns
    assert set(resolved.policy.selectable_columns) == set(resolved.policy.allowed_tables)


def test_guard_internal_columns_are_not_selectable_but_remain_in_table_columns():
    """守卫内部列（租户/状态）对模型不可见，但必须留在 table_columns 供守卫注入条件。"""
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    for table in resolved.policy.allowed_tables:
        assert "TENANT_ID" not in resolved.policy.selectable_columns[table]
        assert "OPERATER_STATUS" not in resolved.policy.selectable_columns[table]
        assert "TENANT_ID" in resolved.policy.table_columns[table]
        assert "OPERATER_STATUS" in resolved.policy.table_columns[table]


def test_business_columns_stay_selectable():
    """项目列与员工列是成员表的业务内容，不能一并排除——否则成员查询作废。"""
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    assert "PROJECT_NAME" in resolved.policy.selectable_columns["t_project"]
    assert "PROJECT_ID" in resolved.policy.selectable_columns["t_project_user"]
    assert "STAFF_ID" in resolved.policy.selectable_columns["t_project_user"]


def test_env_allowed_columns_narrows_selectable_only():
    """环境变量层只收紧模型可见列；table_columns 必须保持完整，否则守卫无法注入。"""
    resolved = resolve_query_policy(
        BusinessPrincipal(user_id=1001),
        FakeDirectory(),
        allowed_columns={"t_project": ["ID", "PROJECT_NAME"]},
    )
    assert resolved.policy.selectable_columns["t_project"] == frozenset({"ID", "PROJECT_NAME"})
    assert "GENERAL_BUDGET" in resolved.policy.table_columns["t_project"]


def test_env_allowed_columns_cannot_widen_beyond_grant():
    """环境变量试图放开未授权列时被剔除，不报错也不放行。"""
    resolved = resolve_query_policy(
        BusinessPrincipal(user_id=1001),
        FakeDirectory(),
        allowed_columns={"t_project": ["ID", "PASSWORD", "SALARY"]},
    )
    assert resolved.policy.selectable_columns["t_project"] == frozenset({"ID"})


def test_env_allowed_tables_narrows_and_cannot_widen():
    resolved = resolve_query_policy(
        BusinessPrincipal(user_id=1001), FakeDirectory(), allowed_tables=["t_project", "t_something_else"]
    )
    assert resolved.policy.allowed_tables == frozenset({"t_project"})
    assert resolved.policy.staff_scope_columns == {}


def test_env_narrowing_to_nothing_is_denied():
    with pytest.raises(errors.PolicyDenial):
        resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(), allowed_tables=["t_unknown"])


def test_env_narrowing_that_empties_a_table_is_denied():
    """收紧到只剩守卫内部列时该表不可用，按拒绝处理而不是留一张空可见列的表。"""
    with pytest.raises(errors.PolicyDenial):
        resolve_query_policy(
            BusinessPrincipal(user_id=1001),
            FakeDirectory(),
            allowed_columns={"t_project": ["TENANT_ID", "OPERATER_STATUS"]},
        )


def test_staff_scope_columns_default_to_current_staff():
    """默认把成员行限定为本人；显式放开时才给空。"""
    default = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    assert dict(default.policy.staff_scope_columns) == {"t_project_user": "STAFF_ID"}

    opened = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(), allow_project_member_rows=True)
    assert opened.policy.staff_scope_columns == {}
