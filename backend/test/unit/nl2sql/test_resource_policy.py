"""资源 YAML 只能收紧：超出代码上限的表或字段必须被拒绝。"""

import pytest

from yuxi.nl2sql import resource_policy
from yuxi.nl2sql import table_policy


def test_menu_binding_covers_the_three_gate_menus():
    assert resource_policy.AI_GATE_MENU_IDS == frozenset({253, 254, 238})
    assert resource_policy.MENU_TO_RESOURCES["AI"] == ("AI_QA_USE",)
    assert resource_policy.MENU_TO_RESOURCES["QA"] == ("AI_QA_USE",)
    assert resource_policy.MENU_TO_RESOURCES["projectManage:projectList:ai"] == ("RD_PROJECT_READ",)


def test_project_grant_stays_within_upper_bound():
    grant = resource_policy.RESOURCE_POLICY["RD_PROJECT_READ"]
    for table, columns in grant.columns.items():
        bound = table_policy.TABLE_UPPER_BOUNDS[table]
        assert columns <= bound.columns, f"{table} 的授权列超出代码上限"
    assert grant.max_rows <= 200


def test_undeclared_resource_code_yields_no_grant():
    assert resource_policy.resolve_grants(("NOPE_NOT_DECLARED",)) == ()


VALID_MENU_IDS = {"AI": 253, "QA": 254, "projectManage:projectList:ai": 238}


def _payload(resource_code, tables, *, max_rows=200, menu_ids=VALID_MENU_IDS):
    """构造载荷。

    menu_ids 必须带上：build_policy 先校验菜单 ID，缺了会让越界用例
    因为无关原因失败，负向案例就失去意义。
    """
    return {
        "version": 1,
        "menu_ids": dict(menu_ids),
        "menu_binding": {"AI": [resource_code]},
        "resources": {resource_code: {"tables": tables, "max_rows": max_rows}},
    }


def test_rejects_table_outside_upper_bound():
    """YAML 引用上限之外的表时必须报错，而不是放行。"""
    payload = _payload("AI_QA_USE", {"t_staff_wage": ["ID"]})
    with pytest.raises(resource_policy.ResourcePolicyError) as exc:
        resource_policy.build_policy(payload)
    assert "不可开放的表" in str(exc.value) and "t_staff_wage" in str(exc.value)


def test_rejects_columns_outside_upper_bound():
    payload = _payload("AI_QA_USE", {"t_project": ["ID", "PROJECT_NAME", "SALARY"]})
    with pytest.raises(resource_policy.ResourcePolicyError) as exc:
        resource_policy.build_policy(payload)
    assert "超出代码上限" in str(exc.value) and "SALARY" in str(exc.value)


def test_rejects_permanently_denied_columns_even_if_in_upper_bound():
    payload = _payload("AI_QA_USE", {"t_project": ["ID", "PASSWORD"]})
    with pytest.raises(resource_policy.ResourcePolicyError) as exc:
        resource_policy.build_policy(payload)
    assert "永久拒绝字段" in str(exc.value) and "PASSWORD" in str(exc.value)


def test_rejects_menu_ids_that_drift_from_the_agreed_gate():
    payload = _payload("AI_QA_USE", {"t_project": ["ID"]}, menu_ids={"AI": 253})
    with pytest.raises(resource_policy.ResourcePolicyError) as exc:
        resource_policy.build_policy(payload)
    assert "功能门菜单 ID" in str(exc.value)
