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


def test_rejects_permanently_denied_table_even_if_in_upper_bound(monkeypatch):
    """永久拒绝表即便被加进代码上限也必须拒绝。

    不能只靠「两张清单恰好互斥」这个巧合——那是个没有被表达、也没有被验证的不变量。
    """
    monkeypatch.setitem(
        table_policy.TABLE_UPPER_BOUNDS, "t_user", table_policy.TABLE_UPPER_BOUNDS["t_project"]
    )
    payload = _payload("AI_QA_USE", {"t_user": ["ID", "TENANT_ID", "PROJECT_NAME"]})
    with pytest.raises(resource_policy.ResourcePolicyError) as exc:
        resource_policy.build_policy(payload)
    assert "永久拒绝的表" in str(exc.value)


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


def test_rejects_grant_that_drops_status_column():
    """授权丢掉 OPERATER_STATUS 会让状态行规则静默消失，必须报错。"""
    payload = _payload("AI_QA_USE", {"t_project": ["ID", "TENANT_ID", "PROJECT_NAME"]})
    with pytest.raises(resource_policy.ResourcePolicyError) as exc:
        resource_policy.build_policy(payload)
    message = str(exc.value)
    assert "缺少行权限必需列" in message and "OPERATER_STATUS" in message


def test_rejects_grant_that_drops_tenant_column():
    """授权丢掉 TENANT_ID 同样必须报错，而不是交给强制层兜底。"""
    payload = _payload("AI_QA_USE", {"t_project": ["ID", "PROJECT_NAME", "OPERATER_STATUS"]})
    with pytest.raises(resource_policy.ResourcePolicyError) as exc:
        resource_policy.build_policy(payload)
    message = str(exc.value)
    assert "缺少行权限必需列" in message and "TENANT_ID" in message


def test_shipped_policy_keeps_required_row_rule_columns():
    """随包 YAML 必须仍带着租户列与状态列，否则状态行规则会消失。"""
    assert resource_policy.RESOURCE_POLICY, "随包资源策略不应为空"
    for resource_code, grant in resource_policy.RESOURCE_POLICY.items():
        for table, columns in grant.columns.items():
            bound = table_policy.TABLE_UPPER_BOUNDS[table]
            assert bound.tenant_column in columns, f"{resource_code}.{table} 缺少 {bound.tenant_column}"
            assert bound.status_column in columns, f"{resource_code}.{table} 缺少 {bound.status_column}"
