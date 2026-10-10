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
