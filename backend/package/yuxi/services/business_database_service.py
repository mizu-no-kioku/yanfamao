"""把 Yuxi 运行身份解析为受控业务数据库查询。"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from typing import Any

from yuxi.repositories.business_database_repository import (
    BusinessDatabaseConnection,
    BusinessDatabaseRepository,
)
from yuxi.services.business_database_security import QueryPolicy, SQLSecurityGuard
from yuxi.utils import logger

DEFAULT_ALLOWED_TABLES = frozenset({"t_project", "t_project_user"})
PROJECT_SCOPE_COLUMNS = {
    "t_project": "ID",
    "t_project_user": "PROJECT_ID",
}
PERMANENTLY_DENIED_COLUMNS = frozenset(
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
PERMANENTLY_DENIED_SUFFIXES = ("_URL", "_ANNEX_ID")
# 守卫内部列：守卫需要它们来注入有效状态条件，但模型不得看见、也不得在
# SELECT/WHERE/HAVING 中引用。原业务库用 9999 表示启用（与 t_user_login_log.STATE 同风格），
# 模型一旦看见就会按常识猜 =1，把结果清空——2026-10-12 实测两个问题都因此答 0。
INTERNAL_STATUS_COLUMNS = frozenset({"ENABLE_STATUS", "OPERATER_STATUS"})


class BusinessDatabaseConfigurationError(RuntimeError):
    """业务数据库服务配置不完整或不合法。"""


class BusinessDatabaseAuthorizationError(PermissionError):
    """当前 Yuxi 用户没有可用的业务数据库身份。"""


@dataclass(frozen=True)
class BusinessDatabaseIdentity:
    """由服务端维护的原业务系统身份和授权范围。"""

    tenant_id: int
    staff_id: int
    allowed_tables: frozenset[str]
    allowed_columns: dict[str, frozenset[str]]
    allow_all_projects: bool
    allow_project_member_rows: bool


@dataclass(frozen=True)
class BusinessDatabaseSettings:
    """一次业务数据库工具调用使用的服务端配置。"""

    connection: BusinessDatabaseConnection
    identities: dict[str, dict[str, Any]]
    max_rows: int

    @classmethod
    def from_env(cls) -> BusinessDatabaseSettings:
        """从 worker 环境读取连接参数和可信身份映射。"""
        required = {
            "host": os.getenv("YUXI_BUSINESS_DB_HOST", "").strip(),
            "user": os.getenv("YUXI_BUSINESS_DB_USER", "").strip(),
            "password": os.getenv("YUXI_BUSINESS_DB_PASSWORD", ""),
            "database": os.getenv("YUXI_BUSINESS_DB_NAME", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise BusinessDatabaseConfigurationError(
                "业务数据库未配置：" + ", ".join(f"YUXI_BUSINESS_DB_{name.upper()}" for name in missing)
            )

        port = _bounded_integer("YUXI_BUSINESS_DB_PORT", default=3306, minimum=1, maximum=65535)
        timeout_ms = _bounded_integer(
            "YUXI_BUSINESS_DB_QUERY_TIMEOUT_MS",
            default=10_000,
            minimum=100,
            maximum=60_000,
        )
        max_rows = _bounded_integer("YUXI_BUSINESS_DB_MAX_ROWS", default=200, minimum=1, maximum=1000)

        raw_identities = os.getenv("YUXI_BUSINESS_DB_IDENTITIES_JSON", "").strip()
        if not raw_identities:
            raise BusinessDatabaseConfigurationError("业务数据库未配置 YUXI_BUSINESS_DB_IDENTITIES_JSON")
        try:
            identities = json.loads(raw_identities)
        except json.JSONDecodeError as exc:
            raise BusinessDatabaseConfigurationError("YUXI_BUSINESS_DB_IDENTITIES_JSON 不是合法 JSON") from exc
        if not isinstance(identities, dict):
            raise BusinessDatabaseConfigurationError("YUXI_BUSINESS_DB_IDENTITIES_JSON 必须是 uid 到身份配置的对象")

        return cls(
            connection=BusinessDatabaseConnection(
                host=required["host"],
                port=port,
                user=required["user"],
                password=required["password"],
                database=required["database"],
                query_timeout_ms=timeout_ms,
            ),
            identities=identities,
            max_rows=max_rows,
        )

    def resolve_identity(self, uid: str) -> BusinessDatabaseIdentity:
        """把可信 Yuxi uid 解析为旧系统租户、员工和查询授权。"""
        raw = self.identities.get(uid)
        if not isinstance(raw, dict):
            raise BusinessDatabaseAuthorizationError("当前用户尚未绑定业务数据库身份")

        tenant_id = _positive_integer(raw.get("tenant_id"), "tenant_id")
        staff_id = _positive_integer(raw.get("staff_id"), "staff_id")
        configured_tables = raw.get("allowed_tables", sorted(DEFAULT_ALLOWED_TABLES))
        if not isinstance(configured_tables, list) or not configured_tables:
            raise BusinessDatabaseConfigurationError("allowed_tables 必须是非空表名列表")
        allowed_tables = frozenset(str(name).strip().lower() for name in configured_tables if str(name).strip())
        unsupported = allowed_tables - DEFAULT_ALLOWED_TABLES
        if unsupported:
            raise BusinessDatabaseConfigurationError("以下业务表尚未完成权限审计：" + ", ".join(sorted(unsupported)))

        raw_columns = raw.get("allowed_columns", {})
        if not isinstance(raw_columns, dict):
            raise BusinessDatabaseConfigurationError("allowed_columns 必须是表名到字段列表的对象")
        allowed_columns: dict[str, frozenset[str]] = {}
        for table_name, columns in raw_columns.items():
            normalized_table = str(table_name).strip().lower()
            if normalized_table not in allowed_tables:
                raise BusinessDatabaseConfigurationError(f"allowed_columns 包含未授权表 {normalized_table}")
            if not isinstance(columns, list) or not columns:
                raise BusinessDatabaseConfigurationError(f"表 {normalized_table} 的 allowed_columns 必须是非空列表")
            allowed_columns[normalized_table] = frozenset(str(column).strip().upper() for column in columns)

        allow_all_projects = raw.get("allow_all_projects", False)
        if not isinstance(allow_all_projects, bool):
            raise BusinessDatabaseConfigurationError("allow_all_projects 必须是布尔值")
        allow_project_member_rows = raw.get("allow_project_member_rows", False)
        if not isinstance(allow_project_member_rows, bool):
            raise BusinessDatabaseConfigurationError("allow_project_member_rows 必须是布尔值")
        return BusinessDatabaseIdentity(
            tenant_id=tenant_id,
            staff_id=staff_id,
            allowed_tables=allowed_tables,
            allowed_columns=allowed_columns,
            allow_all_projects=allow_all_projects,
            allow_project_member_rows=allow_project_member_rows,
        )


@dataclass(frozen=True)
class PreparedBusinessDatabaseAccess:
    """完成身份、Schema 和项目授权解析后的执行上下文。"""

    identity: BusinessDatabaseIdentity
    metadata: dict[str, list[dict[str, Any]]]
    guard: SQLSecurityGuard


class BusinessDatabaseService:
    """供 Agent 工具调用的业务数据库用例服务。"""

    def __init__(
        self,
        settings_loader: Callable[[], BusinessDatabaseSettings] = BusinessDatabaseSettings.from_env,
        repository_factory: Callable[
            [BusinessDatabaseConnection], BusinessDatabaseRepository
        ] = BusinessDatabaseRepository,
    ):
        self._settings_loader = settings_loader
        self._repository_factory = repository_factory

    def list_authorized_tables(self, uid: str) -> list[dict[str, str]]:
        """列出当前用户可供模型选择的业务表。"""
        settings, repository, access = self._prepare(uid)
        del settings, repository
        return [
            {
                "table_name": table_name,
                "comment": str(columns[0].get("TABLE_COMMENT") or ""),
            }
            for table_name, columns in sorted(access.metadata.items())
        ]

    def describe_authorized_table(self, uid: str, table_name: str) -> dict[str, Any]:
        """返回当前用户可见的单表字段清单。"""
        normalized = str(table_name or "").strip().lower()
        settings, repository, access = self._prepare(uid)
        del settings, repository
        if normalized not in access.metadata:
            raise BusinessDatabaseAuthorizationError(f"当前身份无权查看表 {normalized or '<empty>'}")
        columns = access.metadata[normalized]
        return {
            "table_name": normalized,
            "comment": str(columns[0].get("TABLE_COMMENT") or ""),
            "columns": [
                {
                    "name": str(column["COLUMN_NAME"]),
                    "type": str(column["COLUMN_TYPE"]),
                    "comment": str(column.get("COLUMN_COMMENT") or ""),
                }
                for column in columns
            ],
            "enforced_scope": "tenant + active records + authorized projects + configured staff scope",
        }

    def execute_authorized_query(self, uid: str, sql: str) -> dict[str, Any]:
        """校验、重写并执行一条当前身份可访问的只读查询。"""
        started_at = time.monotonic()
        settings, repository, access = self._prepare(uid)
        secured = access.guard.secure(sql)
        result = repository.execute_query(secured.final_sql)
        normalized_result = _json_safe(result)
        logger.info(
            "business database query completed uid={} tenant_id={} staff_id={} tables={} rows={} latency_ms={}",
            uid,
            access.identity.tenant_id,
            access.identity.staff_id,
            list(secured.tables),
            normalized_result["row_count"],
            round((time.monotonic() - started_at) * 1000, 2),
        )
        return {
            **normalized_result,
            "limit": secured.limit,
            "tables": list(secured.tables),
            "scope": "tenant + active records + authorized projects + configured staff scope",
        }

    def _prepare(
        self,
        uid: str,
    ) -> tuple[BusinessDatabaseSettings, BusinessDatabaseRepository, PreparedBusinessDatabaseAccess]:
        normalized_uid = str(uid or "").strip()
        if not normalized_uid:
            raise BusinessDatabaseAuthorizationError("无法获取当前 Yuxi 用户身份")
        settings = self._settings_loader()
        identity = settings.resolve_identity(normalized_uid)
        repository = self._repository_factory(settings.connection)
        if not repository.validate_staff_identity(identity.tenant_id, identity.staff_id):
            raise BusinessDatabaseAuthorizationError("绑定的员工不属于当前租户或员工记录已失效")

        raw_metadata = repository.load_schema(identity.allowed_tables)
        missing_tables = identity.allowed_tables - set(raw_metadata)
        if missing_tables:
            raise BusinessDatabaseConfigurationError("授权表不存在：" + ", ".join(sorted(missing_tables)))
        safe_metadata = _filter_metadata(raw_metadata, {})
        metadata = _without_internal_columns(_filter_metadata(safe_metadata, identity.allowed_columns))
        table_columns = {
            table_name: frozenset(str(column["COLUMN_NAME"]) for column in columns)
            for table_name, columns in safe_metadata.items()
        }
        selectable_columns = {
            table_name: frozenset(str(column["COLUMN_NAME"]) for column in columns)
            for table_name, columns in metadata.items()
        }
        project_scope_columns = {
            table_name: PROJECT_SCOPE_COLUMNS[table_name] for table_name in identity.allowed_tables
        }
        for table_name, project_column in project_scope_columns.items():
            normalized_columns = {column.upper() for column in table_columns[table_name]}
            if "TENANT_ID" not in normalized_columns or project_column not in normalized_columns:
                raise BusinessDatabaseConfigurationError(f"表 {table_name} 缺少租户或项目安全字段")

        staff_scope_columns = (
            {}
            if identity.allow_project_member_rows or "t_project_user" not in identity.allowed_tables
            else {"t_project_user": "STAFF_ID"}
        )

        project_ids = repository.list_project_ids(
            identity.tenant_id,
            identity.staff_id,
            allow_all_projects=identity.allow_all_projects,
        )
        guard = SQLSecurityGuard(
            QueryPolicy(
                tenant_id=identity.tenant_id,
                staff_id=identity.staff_id,
                allowed_tables=identity.allowed_tables,
                table_columns=table_columns,
                allowed_project_ids=project_ids,
                selectable_columns=selectable_columns,
                project_scope_columns=project_scope_columns,
                staff_scope_columns=staff_scope_columns,
                denied_columns=PERMANENTLY_DENIED_COLUMNS,
                max_rows=settings.max_rows,
            )
        )
        return settings, repository, PreparedBusinessDatabaseAccess(identity, metadata, guard)


def _without_internal_columns(
    metadata: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """从模型可见 schema 中移除守卫内部列。

    只在构造 selectable_columns 与对外 schema 时使用；table_columns 必须保留这些列，
    否则守卫无法注入 OPERATER_STATUS > 0。
    """
    stripped: dict[str, list[dict[str, Any]]] = {}
    for table_name, columns in metadata.items():
        visible = [column for column in columns if str(column["COLUMN_NAME"]).upper() not in INTERNAL_STATUS_COLUMNS]
        if not visible:
            raise BusinessDatabaseConfigurationError(f"表 {table_name} 没有可授权字段")
        stripped[table_name] = visible
    return stripped


def _filter_metadata(
    raw_metadata: dict[str, list[dict[str, Any]]],
    allowed_columns: dict[str, frozenset[str]],
) -> dict[str, list[dict[str, Any]]]:
    filtered: dict[str, list[dict[str, Any]]] = {}
    for table_name, columns in raw_metadata.items():
        requested = allowed_columns.get(table_name)
        visible: list[dict[str, Any]] = []
        for column in columns:
            column_name = str(column["COLUMN_NAME"]).upper()
            if column_name in PERMANENTLY_DENIED_COLUMNS or column_name.endswith(PERMANENTLY_DENIED_SUFFIXES):
                continue
            if requested is not None and column_name not in requested:
                continue
            visible.append(column)
        if not visible:
            raise BusinessDatabaseConfigurationError(f"表 {table_name} 没有可授权字段")
        filtered[table_name] = visible
    return filtered


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BusinessDatabaseConfigurationError(f"{name} 必须是正整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BusinessDatabaseConfigurationError(f"{name} 必须是正整数") from exc
    if parsed <= 0:
        raise BusinessDatabaseConfigurationError(f"{name} 必须是正整数")
    return parsed


def _bounded_integer(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise BusinessDatabaseConfigurationError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise BusinessDatabaseConfigurationError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, datetime_time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
