"""代码拥有的表与列上限。

资源 YAML 只能在本模块声明的范围内取子集；缺任一行权限规则（租户列、
项目列、状态规则）的表一律不可开放。改这里等于改权限边界，必须走决策记录。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TableBound:
    """单表上限。columns 含过滤所需的 ID/TENANT_ID 等列，强制层需要它们。"""

    columns: frozenset[str]
    tenant_column: str | None
    project_column: str | None
    status_column: str | None


TABLE_UPPER_BOUNDS: dict[str, TableBound] = {
    "t_project": TableBound(
        columns=frozenset(
            {"ID", "TENANT_ID", "PROJECT_NAME", "PROJECT_MANAGER_NAMES", "GENERAL_BUDGET", "OPERATER_STATUS"}
        ),
        tenant_column="TENANT_ID",
        project_column="ID",
        status_column="OPERATER_STATUS",
    ),
    "t_project_user": TableBound(
        columns=frozenset({"ID", "TENANT_ID", "PROJECT_ID", "STAFF_ID", "OPERATER_STATUS"}),
        tenant_column="TENANT_ID",
        project_column="PROJECT_ID",
        status_column="OPERATER_STATUS",
    ),
}

PERMANENTLY_DENIED_TABLES: frozenset[str] = frozenset(
    {"system_user", "t_user", "t_staff_wage", "t_staff_wage_benefit_config"}
)

PERMANENTLY_DENIED_COLUMNS: frozenset[str] = frozenset(
    {
        "ACCOUNT",
        "PASSWORD",
        "SALT",
        "ID_CARD",
        "PHONES",
        "EDUCATION_CERTIFICATE_ANNEX_URL",
        "DEGREE_CERTIFICATE_ANNEX_URL",
        "LABOR_CONTRACT_ANNEX_URL",
    }
)


def has_complete_row_rule(table: str) -> bool:
    """租户列、项目列、状态列齐全才可开放。"""
    bound = TABLE_UPPER_BOUNDS.get(table)
    if bound is None:
        return False
    return bool(bound.tenant_column and bound.project_column and bound.status_column)
