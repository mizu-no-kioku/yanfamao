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

# 行数硬上限的唯一代码来源。YAML/RBAC 授权与环境变量都只能在此基础上收紧，
# 不得放宽——`effective_max_rows = min(代码上限, YAML 上限, 环境变量上限)`。
MAX_ROWS_CODE_UPPER_BOUND: int = 200

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


# 守卫内部列：守卫需要它们注入租户与有效状态条件，模型不得看见、也不得引用。
# 只排除语义列。项目列（t_project.ID、t_project_user.PROJECT_ID）与员工列
# （t_project_user.STAFF_ID）是成员表的业务内容，不在排除之列——它们是用户要问的答案是本身，
# 且守卫会 AND 自己的条件，模型对它们加条件不构成越权。
#
# 状态列尤其危险：原业务库用 9999 表示启用（与 t_user_login_log.STATE 同风格），
# 模型按常识猜 =1 会把结果清空——2026-10-12 实测把「参与了多少个项目」答成 0（实际 14）。
GUARD_INTERNAL_COLUMNS: frozenset[str] = frozenset({"TENANT_ID", "OPERATER_STATUS", "ENABLE_STATUS"})


def has_complete_row_rule(table: str) -> bool:
    """租户列、项目列、状态列齐全才可开放。"""
    bound = TABLE_UPPER_BOUNDS.get(table)
    if bound is None:
        return False
    return bool(bound.tenant_column and bound.project_column and bound.status_column)


def selectable_columns_for(table: str, columns: frozenset[str]) -> frozenset[str]:
    """在上限或授权列之上算出模型可见列：去掉守卫内部列。"""
    return frozenset(column for column in columns if column.upper() not in GUARD_INTERNAL_COLUMNS)
