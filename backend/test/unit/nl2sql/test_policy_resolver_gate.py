"""角色基数按原始关系表判定；功能门按菜单 ID 任意命中。"""

import pytest

from yuxi.nl2sql import errors
from yuxi.nl2sql.policy_resolver import (
    BusinessSubject,
    resolve_function_gate,
    resolve_resource_codes,
)


class FakeDirectory:
    def __init__(self, *, relations=(), active_roles=(), menu_codes=frozenset(), menu_ids=frozenset()):
        self.relations = tuple(relations)
        self.active_roles = tuple(active_roles)
        self.menu_codes = frozenset(menu_codes)
        self.menu_ids = frozenset(menu_ids)
        self.relation_lookups = 0

    def find_role_relation_ids(self, user_id, tenant_id):
        self.relation_lookups += 1
        return self.relations

    def find_active_role_ids(self, role_ids):
        return tuple(rid for rid in role_ids if rid in self.active_roles)

    def find_menu_codes(self, role_ids, tenant_id):
        return self.menu_codes

    def find_menu_ids(self, role_ids, tenant_id):
        return self.menu_ids


SUBJECT = BusinessSubject(user_id=1001, tenant_id=10, staff_id=2001)


def test_no_role_relation_is_denied():
    directory = FakeDirectory(relations=(), active_roles=())
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_function_gate(SUBJECT, directory)
    assert exc.value.code == errors.ROLE_MISSING


def test_role_relation_pointing_to_inactive_role_is_denied():
    directory = FakeDirectory(relations=(3004,), active_roles=(), menu_ids=frozenset({253}))
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_function_gate(SUBJECT, directory)
    assert exc.value.code == errors.ROLE_INACTIVE


def test_more_than_one_role_relation_is_a_data_integrity_denial():
    directory = FakeDirectory(relations=(3001, 3002), active_roles=(3001, 3002))
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_function_gate(SUBJECT, directory)
    assert exc.value.code == errors.ROLE_CARDINALITY_INVALID


def test_cardinality_uses_relation_rows_not_menu_rows():
    """一个角色关联多个菜单仍是一个角色——基数只看原始关系行。"""
    directory = FakeDirectory(
        relations=(3001,),
        active_roles=(3001,),
        menu_codes=frozenset({"AI", "QA"}),
        menu_ids=frozenset({253, 254, 238, 900}),
    )
    assert resolve_function_gate(SUBJECT, directory) == (3001,)
    assert directory.relation_lookups == 1


def test_no_gate_menu_is_denied():
    directory = FakeDirectory(relations=(3001,), active_roles=(3001,), menu_ids=frozenset({900}))
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_function_gate(SUBJECT, directory)
    assert exc.value.code == errors.FUNCTION_GATE_DENIED


def test_gate_code_with_non_gate_id_is_denied():
    """门禁按 ID：CODE 恰为门菜单码、但 ID 不是门 ID 的菜单必须被拒。

    只按 CODE 门禁时本用例会放行——这正是要防的回归。
    """
    directory = FakeDirectory(
        relations=(3001,),
        active_roles=(3001,),
        menu_codes=frozenset({"AI", "QA", "projectManage:projectList:ai"}),
        menu_ids=frozenset({900, 901}),
    )
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_function_gate(SUBJECT, directory)
    assert exc.value.code == errors.FUNCTION_GATE_DENIED


def test_gate_id_without_gate_code_is_admitted():
    """门禁按 ID：命中门 ID 即通过，不要求 CODE 落在门码集合内。"""
    directory = FakeDirectory(
        relations=(3001,),
        active_roles=(3001,),
        menu_codes=frozenset({"NOT_A_GATE_CODE"}),
        menu_ids=frozenset({253}),
    )
    assert resolve_function_gate(SUBJECT, directory) == (3001,)


@pytest.mark.parametrize("menu_id", [253, 254, 238])
def test_any_single_gate_menu_id_passes(menu_id):
    directory = FakeDirectory(relations=(3001,), active_roles=(3001,), menu_ids=frozenset({menu_id}))
    assert resolve_function_gate(SUBJECT, directory) == (3001,)


def test_resource_codes_come_from_menu_binding():
    directory = FakeDirectory(menu_codes=frozenset({"AI", "projectManage:projectList:ai"}))
    codes = resolve_resource_codes(SUBJECT, (3001,), directory)
    assert codes == frozenset({"AI_QA_USE", "RD_PROJECT_READ"})


def test_unknown_menu_contributes_no_resource():
    directory = FakeDirectory(menu_codes=frozenset({"AI", "SOMETHING_ELSE"}))
    assert resolve_resource_codes(SUBJECT, (3001,), directory) == frozenset({"AI_QA_USE"})


def test_gate_menu_binding_covers_declared_gate_codes():
    from yuxi.nl2sql.resource_policy import AI_GATE_MENU_CODES, AI_GATE_MENU_IDS, MENU_TO_RESOURCES

    assert set(MENU_TO_RESOURCES) == set(AI_GATE_MENU_CODES)
    assert AI_GATE_MENU_IDS == frozenset({253, 254, 238})
