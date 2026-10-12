from __future__ import annotations

from decimal import Decimal

import pytest

from yuxi.repositories.business_database_repository import BusinessDatabaseConnection
from yuxi.services.business_database_security import PolicyViolation
from yuxi.services.business_database_service import (
    BusinessDatabaseAuthorizationError,
    BusinessDatabaseService,
    BusinessDatabaseSettings,
)

pytestmark = pytest.mark.unit


SCHEMA = {
    "t_project": [
        {
            "TABLE_NAME": "t_project",
            "TABLE_COMMENT": "项目表",
            "COLUMN_NAME": "ID",
            "COLUMN_TYPE": "bigint",
            "COLUMN_COMMENT": "编号",
        },
        {
            "TABLE_NAME": "t_project",
            "TABLE_COMMENT": "项目表",
            "COLUMN_NAME": "TENANT_ID",
            "COLUMN_TYPE": "bigint",
            "COLUMN_COMMENT": "租户",
        },
        {
            "TABLE_NAME": "t_project",
            "TABLE_COMMENT": "项目表",
            "COLUMN_NAME": "PROJECT_NAME",
            "COLUMN_TYPE": "varchar(255)",
            "COLUMN_COMMENT": "项目名称",
        },
        {
            "TABLE_NAME": "t_project",
            "TABLE_COMMENT": "项目表",
            "COLUMN_NAME": "GENERAL_BUDGET",
            "COLUMN_TYPE": "decimal(15,5)",
            "COLUMN_COMMENT": "预算",
        },
        {
            "TABLE_NAME": "t_project",
            "TABLE_COMMENT": "项目表",
            "COLUMN_NAME": "PROPOSAL_ANNEX_URL",
            "COLUMN_TYPE": "varchar(512)",
            "COLUMN_COMMENT": "附件",
        },
        {
            "TABLE_NAME": "t_project",
            "TABLE_COMMENT": "项目表",
            "COLUMN_NAME": "OPERATER_STATUS",
            "COLUMN_TYPE": "int",
            "COLUMN_COMMENT": "状态",
        },
        {
            "TABLE_NAME": "t_project",
            "TABLE_COMMENT": "项目表",
            "COLUMN_NAME": "ENABLE_STATUS",
            "COLUMN_TYPE": "int",
            "COLUMN_COMMENT": "启用状态",
        },
    ],
    "t_project_user": [
        {
            "TABLE_NAME": "t_project_user",
            "TABLE_COMMENT": "项目人员表",
            "COLUMN_NAME": "ID",
            "COLUMN_TYPE": "bigint",
            "COLUMN_COMMENT": "编号",
        },
        {
            "TABLE_NAME": "t_project_user",
            "TABLE_COMMENT": "项目人员表",
            "COLUMN_NAME": "TENANT_ID",
            "COLUMN_TYPE": "bigint",
            "COLUMN_COMMENT": "租户",
        },
        {
            "TABLE_NAME": "t_project_user",
            "TABLE_COMMENT": "项目人员表",
            "COLUMN_NAME": "STAFF_ID",
            "COLUMN_TYPE": "bigint",
            "COLUMN_COMMENT": "员工",
        },
        {
            "TABLE_NAME": "t_project_user",
            "TABLE_COMMENT": "项目人员表",
            "COLUMN_NAME": "PROJECT_ID",
            "COLUMN_TYPE": "bigint",
            "COLUMN_COMMENT": "项目",
        },
        {
            "TABLE_NAME": "t_project_user",
            "TABLE_COMMENT": "项目人员表",
            "COLUMN_NAME": "OPERATER_STATUS",
            "COLUMN_TYPE": "int",
            "COLUMN_COMMENT": "状态",
        },
        {
            "TABLE_NAME": "t_project_user",
            "TABLE_COMMENT": "项目人员表",
            "COLUMN_NAME": "ENABLE_STATUS",
            "COLUMN_TYPE": "int",
            "COLUMN_COMMENT": "启用状态",
        },
    ],
}


class FakeRepository:
    """记录服务调用并返回固定业务数据。"""

    def __init__(self, config: BusinessDatabaseConnection):
        self.config = config
        self.identity_valid = True
        self.project_ids = (2, 71)
        self.executed_sql: str | None = None

    def validate_staff_identity(self, tenant_id: int, staff_id: int) -> bool:
        assert (tenant_id, staff_id) == (1, 13)
        return self.identity_valid

    def load_schema(self, table_names: frozenset[str]):
        return {name: SCHEMA[name] for name in table_names}

    def list_project_ids(self, tenant_id: int, staff_id: int, *, allow_all_projects: bool):
        assert (tenant_id, staff_id, allow_all_projects) == (1, 13, False)
        return self.project_ids

    def execute_query(self, sql: str):
        self.executed_sql = sql
        return {
            "row_count": 1,
            "fields": ["ID", "PROJECT_NAME", "GENERAL_BUDGET"],
            "rows": [{"ID": 2, "PROJECT_NAME": "项目甲", "GENERAL_BUDGET": Decimal("100.00")}],
        }


def make_settings(identity: dict | None = None) -> BusinessDatabaseSettings:
    """构造不含真实凭据的测试配置。"""
    return BusinessDatabaseSettings(
        connection=BusinessDatabaseConnection(
            host="db.example.test",
            port=3306,
            user="readonly",
            password="placeholder",
            database="business",
            query_timeout_ms=1000,
        ),
        identities={
            "uid-1": identity
            or {
                "tenant_id": 1,
                "staff_id": 13,
                "allowed_tables": ["t_project", "t_project_user"],
                "allow_all_projects": False,
                "allow_project_member_rows": False,
            }
        },
        max_rows=20,
    )


def build_service(settings: BusinessDatabaseSettings | None = None):
    """返回服务和可观察的假 repository。"""
    repository = FakeRepository((settings or make_settings()).connection)
    service = BusinessDatabaseService(
        settings_loader=lambda: settings or make_settings(),
        repository_factory=lambda _config: repository,
    )
    return service, repository


def test_service_hides_attachment_columns_from_schema() -> None:
    service, _ = build_service()
    description = service.describe_authorized_table("uid-1", "t_project")
    names = {column["name"] for column in description["columns"]}
    assert "PROJECT_NAME" in names
    assert "PROPOSAL_ANNEX_URL" not in names


def test_service_executes_only_rewritten_query_and_serializes_decimal() -> None:
    service, repository = build_service()
    result = service.execute_authorized_query(
        "uid-1",
        "SELECT ID, PROJECT_NAME, GENERAL_BUDGET FROM t_project",
    )
    assert repository.executed_sql is not None
    lowered = repository.executed_sql.lower()
    assert "t_project.tenant_id = 1" in lowered
    assert "t_project.id in (2, 71)" in lowered
    assert "limit 20" in lowered
    assert result["rows"][0]["GENERAL_BUDGET"] == "100.00"


def test_service_limits_project_member_rows_to_current_staff() -> None:
    service, repository = build_service()
    service.execute_authorized_query(
        "uid-1",
        "SELECT p.ID, pu.STAFF_ID FROM t_project p JOIN t_project_user pu ON pu.PROJECT_ID = p.ID",
    )
    assert repository.executed_sql is not None
    assert "pu.staff_id = 13" in repository.executed_sql.lower()


def test_service_keeps_internal_scope_columns_out_of_visible_schema() -> None:
    settings = make_settings(
        {
            "tenant_id": 1,
            "staff_id": 13,
            "allowed_tables": ["t_project"],
            "allowed_columns": {"t_project": ["PROJECT_NAME"]},
            "allow_all_projects": False,
            "allow_project_member_rows": False,
        }
    )
    service, repository = build_service(settings)

    description = service.describe_authorized_table("uid-1", "t_project")
    service.execute_authorized_query("uid-1", "SELECT PROJECT_NAME FROM t_project")

    assert [column["name"] for column in description["columns"]] == ["PROJECT_NAME"]
    assert repository.executed_sql is not None
    assert "t_project.tenant_id = 1" in repository.executed_sql.lower()
    assert "t_project.id in (2, 71)" in repository.executed_sql.lower()


def test_service_hides_internal_status_columns_from_schema() -> None:
    """状态列是守卫内部列，模型不得看见。

    原业务库用 9999 表示启用；模型一旦看见 ENABLE_STATUS 就会按常识猜 = 1，
    把本该返回 14 个项目的查询答成 0。
    """
    service, _ = build_service()
    for table in ("t_project", "t_project_user"):
        names = {column["name"] for column in service.describe_authorized_table("uid-1", table)["columns"]}
        assert "OPERATER_STATUS" not in names
        assert "ENABLE_STATUS" not in names


def test_service_rejects_query_referencing_internal_status_column() -> None:
    """模型显式引用状态列必须被拒，且不得有任何 SQL 落到数据库。"""
    service, repository = build_service()
    with pytest.raises(PolicyViolation) as exc:
        service.execute_authorized_query("uid-1", "SELECT ID FROM t_project WHERE OPERATER_STATUS > 0")
    assert exc.value.code == "COLUMN_NOT_ALLOWED"
    assert repository.executed_sql is None


def test_service_still_injects_status_predicate_after_hiding_the_column() -> None:
    """隐藏状态列不得削弱强制：守卫仍须自行注入 OPERATER_STATUS > 0。

    这条与上面两条构成一对——隐藏可见性，但不放松执行。
    """
    service, repository = build_service()
    service.execute_authorized_query("uid-1", "SELECT ID, PROJECT_NAME FROM t_project")
    assert repository.executed_sql is not None
    assert "operater_status > 0" in repository.executed_sql.lower()


def test_service_rejects_unmapped_uid_before_database_access() -> None:
    service, _ = build_service()
    with pytest.raises(BusinessDatabaseAuthorizationError, match="尚未绑定"):
        service.list_authorized_tables("unknown")


def test_service_rejects_invalid_staff_binding() -> None:
    service, repository = build_service()
    repository.identity_valid = False
    with pytest.raises(BusinessDatabaseAuthorizationError, match="员工"):
        service.list_authorized_tables("uid-1")


def test_service_rejects_queries_when_user_has_no_projects() -> None:
    service, repository = build_service()
    repository.project_ids = ()
    with pytest.raises(PolicyViolation) as caught:
        service.execute_authorized_query("uid-1", "SELECT ID FROM t_project")
    assert caught.value.code == "NO_PROJECT_ACCESS"


def test_settings_reject_unaudited_table() -> None:
    settings = make_settings(
        {
            "tenant_id": 1,
            "staff_id": 13,
            "allowed_tables": ["t_user"],
            "allow_all_projects": False,
        }
    )
    with pytest.raises(RuntimeError, match="尚未完成权限审计"):
        settings.resolve_identity("uid-1")
