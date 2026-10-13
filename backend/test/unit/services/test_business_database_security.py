from __future__ import annotations

import pytest

from yuxi.services.business_database_security import PolicyViolation, QueryPolicy, SQLSecurityGuard

pytestmark = pytest.mark.unit


def make_guard(project_ids: tuple[int, ...] = (2, 71), max_rows: int = 50) -> SQLSecurityGuard:
    """构造只开放两张项目表的测试策略。"""
    return SQLSecurityGuard(
        QueryPolicy(
            tenant_id=1,
            staff_id=13,
            allowed_tables=frozenset({"t_project", "t_project_user"}),
            table_columns={
                "t_project": frozenset({"ID", "TENANT_ID", "PROJECT_NAME", "GENERAL_BUDGET", "OPERATER_STATUS"}),
                "t_project_user": frozenset({"ID", "TENANT_ID", "STAFF_ID", "PROJECT_ID", "OPERATER_STATUS"}),
            },
            allowed_project_ids=project_ids,
            project_scope_columns={"t_project": "ID", "t_project_user": "PROJECT_ID"},
            staff_scope_columns={"t_project_user": "STAFF_ID"},
            status_scope_columns={"t_project": "OPERATER_STATUS", "t_project_user": "OPERATER_STATUS"},
            denied_tables=frozenset({"t_user", "t_staff_wage"}),
            denied_columns=frozenset({"PASSWORD", "SALT", "ID_CARD", "PHONES"}),
            max_rows=max_rows,
        )
    )


def assert_violation(sql: str, code: str) -> None:
    """断言 SQL 在指定安全规则上失败。"""
    with pytest.raises(PolicyViolation) as caught:
        make_guard().secure(sql)
    assert caught.value.code == code


def test_guard_injects_tenant_project_status_and_limit() -> None:
    secured = make_guard().secure("SELECT p.ID, p.PROJECT_NAME, p.GENERAL_BUDGET FROM t_project p")
    lowered = secured.final_sql.lower()
    assert "p.tenant_id = 1" in lowered
    assert "p.operater_status > 0" in lowered
    assert "p.id in (2, 71)" in lowered
    assert "limit 50" in lowered


def test_guard_uses_policy_declared_status_column() -> None:
    """状态列必须取自策略声明，而不是硬编码 OPERATER_STATUS。

    代码上限里每张表各自声明状态列；硬编码会在换表或换列时静默失效——
    谓词照旧注入，但注入了错误的列名，强制层看似生效、实际不生效。
    """
    guard = SQLSecurityGuard(
        QueryPolicy(
            tenant_id=1,
            staff_id=13,
            allowed_tables=frozenset({"t_project"}),
            table_columns={"t_project": frozenset({"ID", "TENANT_ID", "PROJECT_NAME", "ENABLE_STATUS"})},
            allowed_project_ids=(2,),
            project_scope_columns={"t_project": "ID"},
            status_scope_columns={"t_project": "ENABLE_STATUS"},
            max_rows=50,
        )
    )

    secured = guard.secure("SELECT p.ID FROM t_project p")

    lowered = secured.final_sql.lower()
    assert "p.enable_status > 0" in lowered
    assert "operater_status" not in lowered


def test_guard_filters_each_joined_physical_table() -> None:
    secured = make_guard().secure(
        "SELECT p.ID, pu.STAFF_ID FROM t_project p JOIN t_project_user pu ON pu.PROJECT_ID = p.ID"
    )
    lowered = secured.final_sql.lower()
    for expected in (
        "p.tenant_id = 1",
        "pu.tenant_id = 1",
        "p.id in (2, 71)",
        "pu.project_id in (2, 71)",
        "pu.staff_id = 13",
    ):
        assert expected in lowered


def test_guard_filters_cte_base_table() -> None:
    secured = make_guard().secure("WITH x AS (SELECT ID, TENANT_ID FROM t_project) SELECT x.ID FROM x")
    lowered = secured.final_sql.lower()
    assert "t_project.tenant_id = 1" in lowered
    assert "t_project.id in (2, 71)" in lowered


def test_guard_caps_large_limit_and_preserves_smaller_limit() -> None:
    assert make_guard().secure("SELECT ID FROM t_project LIMIT 999").limit == 50
    assert make_guard().secure("SELECT ID FROM t_project LIMIT 5").limit == 5


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("DELETE FROM t_project", "READ_ONLY"),
        ("SELECT ID FROM t_project; SELECT ID FROM t_project", "MULTI_STATEMENT"),
        ("SELECT @x := ID FROM t_project", "SESSION_VARIABLE"),
        ("SELECT @@version, ID FROM t_project", "SESSION_VARIABLE"),
        ("SELECT ID FROM t_user", "DENIED_TABLE"),
        ("SELECT PASSWORD FROM t_project", "DENIED_COLUMN"),
        ("SELECT UNKNOWN_FIELD FROM t_project", "COLUMN_NOT_ALLOWED"),
        ("SELECT * FROM t_project", "WILDCARD_COLUMN"),
        ("SELECT SLEEP(10) FROM t_project", "FUNCTION_NOT_ALLOWED"),
        ("SELECT ID FROM other_db.t_project", "QUALIFIED_TABLE"),
        ("SELECT ID FROM t_project LIMIT 10 OFFSET 999999", "OFFSET_NOT_ALLOWED"),
    ],
)
def test_guard_rejects_unsafe_queries(sql: str, code: str) -> None:
    assert_violation(sql, code)


def test_guard_allows_count_star() -> None:
    assert "COUNT(*)" in make_guard().secure("SELECT COUNT(*) AS project_count FROM t_project").final_sql


def test_guard_denies_project_query_without_projects() -> None:
    with pytest.raises(PolicyViolation) as caught:
        make_guard(project_ids=()).secure("SELECT ID FROM t_project")
    assert caught.value.code == "NO_PROJECT_ACCESS"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT GENERAL_BUDGET AS GENERAL_BUDGET FROM t_project",
        "SELECT ID AS GENERAL_BUDGET FROM t_project WHERE GENERAL_BUDGET = 1",
        "SELECT GENERAL_BUDGET FROM t_project JOIN (SELECT ID FROM t_project) x ON x.ID = t_project.ID",
    ],
)
def test_guard_aliases_and_derived_sources_cannot_expose_hidden_columns(sql: str) -> None:
    """投影别名和派生表不能绕过字段授权。"""
    from dataclasses import replace

    policy = replace(
        make_guard().policy,
        selectable_columns={
            "t_project": frozenset({"ID", "PROJECT_NAME"}),
            "t_project_user": frozenset({"ID", "PROJECT_ID"}),
        },
    )
    with pytest.raises(PolicyViolation) as caught:
        SQLSecurityGuard(policy).secure(sql)
    assert caught.value.code == "COLUMN_NOT_ALLOWED"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT PROJECT_NAME AS label FROM t_project ORDER BY label",
        "SELECT COUNT(*) AS n FROM t_project HAVING n > 0",
        "SELECT x.label FROM (SELECT PROJECT_NAME AS label FROM t_project) x",
    ],
)
def test_guard_allows_authorized_projection_aliases(sql: str) -> None:
    """合法投影别名仍可用于排序、聚合和派生表。"""
    assert make_guard().secure(sql).final_sql


@pytest.mark.parametrize("side", ["LEFT", "RIGHT", "FULL"])
def test_guard_rejects_outer_join_instead_of_changing_result(side: str) -> None:
    """外连接不能被权限过滤静默改成内连接。"""
    with pytest.raises(PolicyViolation) as caught:
        make_guard().secure(f"SELECT p.ID FROM t_project p {side} JOIN t_project_user pu ON pu.PROJECT_ID = p.ID")
    assert caught.value.code == "OUTER_JOIN_NOT_ALLOWED"
