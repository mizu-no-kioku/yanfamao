"""表/列上限由代码拥有；缺行权限规则的表不可开放。"""

from yuxi.nl2sql import table_policy


def test_first_phase_exposes_only_project_tables():
    assert set(table_policy.TABLE_UPPER_BOUNDS) == {"t_project", "t_project_user"}


def test_project_table_bound_is_complete():
    bound = table_policy.TABLE_UPPER_BOUNDS["t_project"]
    assert bound.tenant_column == "TENANT_ID"
    assert bound.project_column == "ID"
    assert bound.status_predicate == "OPERATER_STATUS > 0"
    assert "GENERAL_BUDGET" in bound.columns
    assert "PASSWORD" not in bound.columns


def test_member_table_bound_is_complete():
    bound = table_policy.TABLE_UPPER_BOUNDS["t_project_user"]
    assert bound.tenant_column == "TENANT_ID"
    assert bound.project_column == "PROJECT_ID"
    assert bound.columns == frozenset({"ID", "TENANT_ID", "PROJECT_ID", "STAFF_ID", "OPERATER_STATUS"})


def test_unknown_table_has_no_bound():
    assert table_policy.TABLE_UPPER_BOUNDS.get("t_staff_wage") is None


def test_incomplete_row_rule_is_not_openable():
    assert table_policy.has_complete_row_rule("t_project") is True
    assert table_policy.has_complete_row_rule("t_unknown") is False


def test_table_with_partial_row_rule_is_not_openable(monkeypatch):
    """有表项但缺行权限规则的表必须拒绝——这是「缺任一规则即拒绝」的真正分支。"""
    from dataclasses import replace

    partial = replace(table_policy.TABLE_UPPER_BOUNDS["t_project"], project_column=None)
    monkeypatch.setitem(table_policy.TABLE_UPPER_BOUNDS, "t_partial", partial)
    assert table_policy.has_complete_row_rule("t_partial") is False


def test_permanent_denials_include_sensitive_identity_columns():
    assert {"ACCOUNT", "PASSWORD", "SALT", "ID_CARD", "PHONES"} <= table_policy.PERMANENTLY_DENIED_COLUMNS
    assert {"system_user", "t_user", "t_staff_wage"} <= table_policy.PERMANENTLY_DENIED_TABLES
