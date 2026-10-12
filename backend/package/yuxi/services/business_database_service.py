"""把 Yuxi 运行身份解析为受控业务数据库查询。"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as datetime_time
from decimal import Decimal
from typing import Any

import pymysql
from yuxi.nl2sql import errors as nl2sql_errors
from yuxi.nl2sql.directory import BusinessDirectory
from yuxi.nl2sql.mysql_directory import directory_on_connection
from yuxi.nl2sql.policy_resolver import ResolvedPolicy, resolve_query_policy
from yuxi.nl2sql.principal import BusinessPrincipal
from yuxi.repositories.business_database_repository import (
    BusinessDatabaseConnection,
    BusinessDatabaseRepository,
)
from yuxi.services.business_database_security import SQLSecurityGuard
from yuxi.utils import logger

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


class BusinessDatabaseConfigurationError(RuntimeError):
    """业务数据库服务配置不完整或不合法。"""


class BusinessDatabaseAuthorizationError(PermissionError):
    """当前 Yuxi 用户没有可用的业务数据库身份。"""


@dataclass(frozen=True)
class BusinessDatabaseBinding:
    """Yuxi 用户到业务库身份的绑定。

    只声明「这个 Yuxi 用户是谁」以及可选的环境变量收紧项。
    租户可以省略（由业务库推导），**员工编号不在此声明**——由解析器按当前租户动态推导，
    这样员工与租户的对应关系变化时不需要改配置。
    """

    business_user_id: int
    tenant_id: int | None
    allowed_tables: frozenset[str] | None
    allowed_columns: dict[str, frozenset[str]]
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

    def resolve_binding(self, uid: str) -> BusinessDatabaseBinding:
        """把可信 Yuxi uid 解析为业务库身份绑定与可选收紧项。

        绑定只给 `business_user_id`（租户可选），**员工编号不再静态声明**——
        它由解析器按当前租户从业务库推导，员工归属变化时无需改配置。
        `allowed_tables` / `allowed_columns` 是环境变量层，语义上只能收紧。
        """
        raw = self.identities.get(uid)
        if not isinstance(raw, dict):
            raise BusinessDatabaseAuthorizationError("当前用户尚未绑定业务数据库身份")

        business_user_id = _positive_integer(raw.get("business_user_id"), "business_user_id")
        raw_tenant = raw.get("tenant_id")
        tenant_id = None if raw_tenant in (None, "") else _positive_integer(raw_tenant, "tenant_id")

        allowed_tables: frozenset[str] | None = None
        if "allowed_tables" in raw:
            configured = raw["allowed_tables"]
            if not isinstance(configured, list) or not configured:
                raise BusinessDatabaseConfigurationError("allowed_tables 必须是非空表名列表")
            names = frozenset(str(name).strip().lower() for name in configured if str(name).strip())
            allowed_tables = names or None

        raw_columns = raw.get("allowed_columns", {})
        if not isinstance(raw_columns, dict):
            raise BusinessDatabaseConfigurationError("allowed_columns 必须是表名到字段列表的对象")
        allowed_columns: dict[str, frozenset[str]] = {}
        for table_name, columns in raw_columns.items():
            normalized_table = str(table_name).strip().lower()
            if not isinstance(columns, list) or not columns:
                raise BusinessDatabaseConfigurationError(f"表 {normalized_table} 的 allowed_columns 必须是非空列表")
            allowed_columns[normalized_table] = frozenset(str(column).strip().upper() for column in columns)

        allow_project_member_rows = raw.get("allow_project_member_rows", False)
        if not isinstance(allow_project_member_rows, bool):
            raise BusinessDatabaseConfigurationError("allow_project_member_rows 必须是布尔值")

        return BusinessDatabaseBinding(
            business_user_id=business_user_id,
            tenant_id=tenant_id,
            allowed_tables=allowed_tables,
            allowed_columns=allowed_columns,
            allow_project_member_rows=allow_project_member_rows,
        )


@dataclass(frozen=True)
class BusinessQueryScope:
    """一次业务库工具调用共用的只读连接与解析目录。

    解析与最终 SQL 必须落在这同一条连接、同一个事务上，才能保证策略所依据的
    权限快照与查询所依据的数据快照是同一份。
    """

    connection: pymysql.connections.Connection
    directory: BusinessDirectory


@dataclass(frozen=True)
class PreparedBusinessDatabaseAccess:
    """完成身份、Schema 和项目授权解析后的执行上下文。"""

    resolved: ResolvedPolicy
    metadata: dict[str, list[dict[str, Any]]]
    guard: SQLSecurityGuard
    repository: BusinessDatabaseRepository
    connection: pymysql.connections.Connection


class BusinessDatabaseService:
    """供 Agent 工具调用的业务数据库用例服务。"""

    def __init__(
        self,
        settings_loader: Callable[[], BusinessDatabaseSettings] = BusinessDatabaseSettings.from_env,
        repository_factory: Callable[
            [BusinessDatabaseConnection], BusinessDatabaseRepository
        ] = BusinessDatabaseRepository,
        query_scope: Callable[[BusinessDatabaseConnection], AbstractContextManager[BusinessQueryScope]] | None = None,
    ):
        self._settings_loader = settings_loader
        self._repository_factory = repository_factory
        # 延迟解析：模块级上下文管理器定义在类之后，直接做默认参数会在类定义时求值失败。
        self._query_scope = query_scope or mysql_query_scope

    def list_authorized_tables(self, uid: str) -> list[dict[str, str]]:
        """列出当前用户可供模型选择的业务表。"""
        with self._prepared(uid) as access:
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
        with self._prepared(uid) as access:
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
        """校验、重写并执行一条当前身份可访问的只读查询。

        解析、Schema 读取、SQL 校验与最终执行共用**同一条只读连接和同一个事务**：
        策略与它约束的数据来自同一快照。每次调用都新建连接与事务，不缓存策略，
        因此下一次调用必然看到已提交的撤权。
        """
        started_at = time.monotonic()
        with self._prepared(uid) as access:
            secured = access.guard.secure(sql)
            result = access.repository.execute_query(secured.final_sql, connection=access.connection)
            normalized_result = _json_safe(result)
            logger.info(
                "business database query completed uid={} tenant_id={} staff_id={} tables={} rows={} latency_ms={}",
                uid,
                access.resolved.subject.tenant_id,
                access.resolved.subject.staff_id,
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

    @contextmanager
    def _prepared(self, uid: str) -> Iterator[PreparedBusinessDatabaseAccess]:
        """在一次只读连接与事务内完成解析、Schema 读取与策略构造。

        `list` / `describe` / `query` 都走这里，因此三者都会重新读取当前权限。
        """
        normalized_uid = str(uid or "").strip()
        if not normalized_uid:
            raise BusinessDatabaseAuthorizationError("无法获取当前 Yuxi 用户身份")
        settings = self._settings_loader()
        binding = settings.resolve_binding(normalized_uid)
        repository = self._repository_factory(settings.connection)

        with self._query_scope(settings.connection) as scope:
            resolved = self._resolve_policy(binding, scope.directory)
            policy = resolved.policy

            raw_metadata = repository.load_schema(policy.allowed_tables, connection=scope.connection)
            missing_tables = policy.allowed_tables - set(raw_metadata)
            if missing_tables:
                raise BusinessDatabaseConfigurationError("授权表不存在：" + ", ".join(sorted(missing_tables)))
            metadata = _visible_metadata(raw_metadata, policy.selectable_columns)

            guard = SQLSecurityGuard(policy)
            yield PreparedBusinessDatabaseAccess(resolved, metadata, guard, repository, scope.connection)

    def _resolve_policy(self, binding: BusinessDatabaseBinding, directory: BusinessDirectory) -> ResolvedPolicy:
        """每次调用都重新解析身份、功能门、资源与项目范围。

        不缓存结果：撤权必须在下一次调用开始的新事务里可见，缓存会让旧授权继续放行。
        """
        try:
            return resolve_query_policy(
                BusinessPrincipal(user_id=binding.business_user_id, tenant_id=binding.tenant_id),
                directory,
                allowed_tables=binding.allowed_tables,
                allowed_columns=binding.allowed_columns,
                allow_project_member_rows=binding.allow_project_member_rows,
            )
        except nl2sql_errors.PolicyDenial as exc:
            raise BusinessDatabaseAuthorizationError(f"{exc.code}: {exc.message}") from exc


def _visible_metadata(
    raw_metadata: dict[str, list[dict[str, Any]]],
    selectable_columns: Mapping[str, frozenset[str]],
) -> dict[str, list[dict[str, Any]]]:
    """按策略的 selectable_columns 裁剪模型可见 schema。

    模型可见列的唯一来源是策略，不在服务层重新推导——否则容易出现
    「守卫按策略校验、schema 按另一套展示」这种两套口径不一致的缺陷。
    守卫内部列（租户/状态）因此不会出现在模型可见结构里。
    """
    visible: dict[str, list[dict[str, Any]]] = {}
    for table_name, columns in raw_metadata.items():
        allowed = selectable_columns.get(table_name.lower())
        if not allowed:
            continue
        kept: list[dict[str, Any]] = []
        for column in columns:
            column_name = str(column["COLUMN_NAME"]).upper()
            if column_name in PERMANENTLY_DENIED_COLUMNS or column_name.endswith(PERMANENTLY_DENIED_SUFFIXES):
                continue
            if column_name not in allowed:
                continue
            kept.append(column)
        if not kept:
            raise BusinessDatabaseConfigurationError(f"表 {table_name} 没有可授权字段")
        visible[table_name] = kept
    return visible


def _mysql_connection_factory(connection: BusinessDatabaseConnection) -> Callable[[], pymysql.connections.Connection]:
    """解析器用的只读连接工厂。连接的真实生命周期由 single_transaction_directory 管理。"""
    timeout_seconds = max(1, (connection.query_timeout_ms + 999) // 1000)

    def connect() -> pymysql.connections.Connection:
        return pymysql.connect(
            host=connection.host,
            port=connection.port,
            user=connection.user,
            password=connection.password,
            database=connection.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.Cursor,
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
            write_timeout=timeout_seconds,
        )

    return connect


@contextmanager
def mysql_query_scope(
    connection: BusinessDatabaseConnection,
    *,
    connect: Callable[[], pymysql.connections.Connection] | None = None,
) -> Iterator[BusinessQueryScope]:
    """默认作用域：一条只读连接 + 一个只读事务，供解析与最终 SQL 共用。

    连接由本函数创建并负责关闭；目录绑定在它上面，不会自行关闭。
    撤权生效边界：下一次工具调用会开启新的连接与事务，因此已提交的撤权必然可见。

    `connect` 可替换连接来源（例如测试中计数，或将来的连接池）。
    """
    active = (connect or _mysql_connection_factory(connection))()
    try:
        with active.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {connection.query_timeout_ms}")
            cursor.execute("START TRANSACTION READ ONLY")
        yield BusinessQueryScope(connection=active, directory=directory_on_connection(active))
    finally:
        active.close()


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
