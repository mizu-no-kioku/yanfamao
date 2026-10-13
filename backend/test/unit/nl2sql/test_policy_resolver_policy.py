"""解析器产出完整 QueryPolicy：表、字段、行范围与上限全部来自代码裁决。"""

from types import MappingProxyType

import pytest
from yuxi.nl2sql import errors, policy_resolver
from yuxi.nl2sql.policy_resolver import resolve_query_policy
from yuxi.nl2sql.principal import BusinessPrincipal
from yuxi.nl2sql.resource_policy import ResourceGrant
from yuxi.nl2sql.table_policy import MAX_ROWS_CODE_UPPER_BOUND, TABLE_UPPER_BOUNDS


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


def test_policy_declares_status_scope_columns_from_table_bounds():
    """状态列由代码上限（`TableBound.status_column`）声明，守卫据此注入。

    不硬编码 `OPERATER_STATUS`：状态列是每张表各自的上限，换表或换列时
    必须由策略跟着变，而不是让守卫继续注入一个不存在的列。
    """
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    assert dict(resolved.policy.status_scope_columns) == {
        "t_project": "OPERATER_STATUS",
        "t_project_user": "OPERATER_STATUS",
    }


def test_status_scope_columns_absent_when_table_not_granted():
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(), allowed_tables=["t_project"])
    assert dict(resolved.policy.status_scope_columns) == {"t_project": "OPERATER_STATUS"}


def test_staff_scope_columns_always_limit_member_rows_to_current_staff():
    """成员行范围由代码裁决，环境变量无权取消。

    要查看其他项目成员，必须由 RBAC/YAML 资源授权表达，而不是环境变量扩权——
    因此这里同时确认「放开开关」已不存在。
    """
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    assert dict(resolved.policy.staff_scope_columns) == {"t_project_user": "STAFF_ID"}

    with pytest.raises(TypeError):
        resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(), allow_project_member_rows=True)


def test_staff_scope_is_absent_when_member_table_not_granted():
    """成员表不在授权范围内时不应凭空产生行范围条目。"""
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(), allowed_tables=["t_project"])
    assert resolved.policy.staff_scope_columns == {}


# --- 环境变量行数上限（YUXI_BUSINESS_DB_MAX_ROWS）必须真正参与求交且只能收紧 ---


def test_default_max_rows_is_the_code_upper_bound():
    """未提供环境变量上限时，行数上限等于代码上限。"""
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory())
    assert resolved.policy.max_rows == MAX_ROWS_CODE_UPPER_BOUND


def test_env_max_rows_tightens_below_code_bound():
    """环境变量小于代码上限时必须生效——这正是它此前被完全忽略的地方。"""
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(), max_rows_env=50)
    assert resolved.policy.max_rows == 50


def test_env_max_rows_cannot_widen_above_code_bound():
    """环境变量大于代码上限时被压回代码上限，绝不扩权。"""
    resolved = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(), max_rows_env=1000)
    assert resolved.policy.max_rows == MAX_ROWS_CODE_UPPER_BOUND


def test_effective_max_rows_is_three_way_intersection(monkeypatch):
    """effective_max_rows = min(代码上限, YAML/RBAC 上限, 环境变量上限)。"""
    bound = TABLE_UPPER_BOUNDS["t_project"]
    grant = ResourceGrant(
        resource_code="AI_QA_USE",
        tables=frozenset({"t_project"}),
        columns=MappingProxyType({"t_project": bound.columns}),
        max_rows=30,
    )
    monkeypatch.setattr(policy_resolver, "resolve_grants", lambda codes: (grant,))

    # YAML 比环境变量紧 → 取 YAML
    tighter_yaml = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(), max_rows_env=200)
    assert tighter_yaml.policy.max_rows == 30
    # 环境变量比 YAML 紧 → 取环境变量
    tighter_env = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(), max_rows_env=10)
    assert tighter_env.policy.max_rows == 10
    # 环境变量试图扩权 → 仍取 YAML
    widening_env = resolve_query_policy(BusinessPrincipal(user_id=1001), FakeDirectory(), max_rows_env=999)
    assert widening_env.policy.max_rows == 30
