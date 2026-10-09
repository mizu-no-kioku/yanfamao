"""把模型生成的 SQL 限制为当前身份可执行的只读查询。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope


class PolicyViolation(ValueError):
    """模型生成的 SQL 超出当前用户策略时抛出。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class QueryPolicy:
    tenant_id: int
    staff_id: int
    allowed_tables: frozenset[str]
    table_columns: Mapping[str, frozenset[str]]
    allowed_project_ids: tuple[int, ...]
    selectable_columns: Mapping[str, frozenset[str]] = field(default_factory=dict)
    project_scope_columns: Mapping[str, str] = field(default_factory=dict)
    staff_scope_columns: Mapping[str, str] = field(default_factory=dict)
    global_tables: frozenset[str] = frozenset()
    denied_tables: frozenset[str] = frozenset()
    denied_columns: frozenset[str] = frozenset()
    max_rows: int = 200

    def __post_init__(self):
        if not isinstance(self.tenant_id, int) or self.tenant_id <= 0:
            raise ValueError("tenant_id must be a positive integer")
        if not isinstance(self.staff_id, int) or self.staff_id <= 0:
            raise ValueError("staff_id must be a positive integer")
        if not self.allowed_tables:
            raise ValueError("allowed_tables cannot be empty")
        if not isinstance(self.max_rows, int) or self.max_rows <= 0:
            raise ValueError("max_rows must be a positive integer")


@dataclass(frozen=True)
class SecuredQuery:
    original_sql: str
    final_sql: str
    tables: tuple[str, ...]
    limit: int


class SQLSecurityGuard:
    """按查询策略校验并重写一条只读 MySQL 查询。"""

    # sqlglot normalizes some MySQL functions to internal canonical names.
    ALLOWED_FUNCTIONS = frozenset(
        {
            "ABS",
            "AND",
            "AVG",
            "CAST",
            "CASE",
            "CEIL",
            "CEILING",
            "CHAR_LENGTH",
            "COALESCE",
            "CONCAT",
            "CONCAT_WS",
            "COUNT",
            "CURRENT_DATE",
            "CURRENT_TIMESTAMP",
            "DATE",
            "DATE_ADD",
            "DATE_DIFF",
            "DATE_FORMAT",
            "DATE_SUB",
            "DAY",
            "DAYOFMONTH",
            "FLOOR",
            "FORMAT",
            "GREATEST",
            "GT",
            "GTE",
            "IF",
            "IFNULL",
            "IN",
            "IS",
            "LEAST",
            "LENGTH",
            "LEFT",
            "LOWER",
            "LT",
            "LTE",
            "MAX",
            "MIN",
            "MONTH",
            "NOW",
            "NOT",
            "NEQ",
            "NULLIF",
            "OR",
            "ROUND",
            "REPLACE",
            "RIGHT",
            "STR_TO_DATE",
            "SUBSTRING",
            "SUM",
            "TIME_TO_STR",
            "TIMESTAMPDIFF",
            "TRIM",
            "TS_OR_DS_TO_DATE",
            "TS_OR_DS_TO_TIMESTAMP",
            "UPPER",
            "YEAR",
        }
    )

    FORBIDDEN_TEXT = (
        "into outfile",
        "into dumpfile",
        "for update",
        "lock in share mode",
        "procedure analyse",
    )

    def __init__(self, policy: QueryPolicy):
        self.policy = policy
        self._allowed_tables = {name.lower() for name in policy.allowed_tables}
        self._denied_tables = {name.lower() for name in policy.denied_tables}
        self._global_tables = {name.lower() for name in policy.global_tables}
        self._denied_columns = {name.lower() for name in policy.denied_columns}
        self._table_columns = {
            table.lower(): {column.lower() for column in columns} for table, columns in policy.table_columns.items()
        }
        self._selectable_columns = {
            table.lower(): {column.lower() for column in columns}
            for table, columns in (policy.selectable_columns or policy.table_columns).items()
        }
        if set(self._selectable_columns) != self._allowed_tables:
            raise ValueError("selectable_columns must cover every allowed table")
        for table_name, columns in self._selectable_columns.items():
            if table_name not in self._table_columns or not columns <= self._table_columns[table_name]:
                raise ValueError(f"selectable_columns for {table_name} exceed table_columns")
        self._project_scope_columns = {
            table.lower(): column.lower() for table, column in policy.project_scope_columns.items()
        }
        self._staff_scope_columns = {
            table.lower(): column.lower() for table, column in policy.staff_scope_columns.items()
        }
        for scope_name, scope_columns in (
            ("project_scope_columns", self._project_scope_columns),
            ("staff_scope_columns", self._staff_scope_columns),
        ):
            for table_name, column_name in scope_columns.items():
                if table_name not in self._table_columns or column_name not in self._table_columns[table_name]:
                    raise ValueError(f"{scope_name} contains unknown field {table_name}.{column_name}")

    def secure(self, sql: str) -> SecuredQuery:
        original_sql = (sql or "").strip()
        root = self._parse_single_query(original_sql)
        tables = self._validate(root, allow_internal_columns=False)
        self._rewrite(root)
        self._apply_limit(root)
        final_sql = root.sql(dialect="mysql")

        # The rewritten SQL is parsed and validated independently.  This catches
        # serialization surprises and keeps the database boundary fail closed.
        final_root = self._parse_single_query(final_sql)
        final_tables = self._validate(final_root, allow_internal_columns=True)
        self._verify_enforcement(final_root)
        return SecuredQuery(
            original_sql=original_sql,
            final_sql=final_sql,
            tables=tuple(sorted(final_tables or tables)),
            limit=self._effective_limit(final_root),
        )

    def _parse_single_query(self, sql: str) -> exp.Expression:
        if not sql:
            raise PolicyViolation("EMPTY_SQL", "模型没有生成 SQL")
        lowered = " ".join(sql.lower().split())
        if any(fragment in lowered for fragment in self.FORBIDDEN_TEXT):
            raise PolicyViolation("FORBIDDEN_CLAUSE", "SQL 包含禁止的读取或锁定子句")
        try:
            statements = [statement for statement in parse(sql, read="mysql") if statement]
        except ParseError as exc:
            raise PolicyViolation("PARSE_ERROR", f"SQL 无法解析：{exc}") from exc
        if len(statements) != 1:
            raise PolicyViolation("MULTI_STATEMENT", "每次只允许一条查询语句")
        root = statements[0]
        if not isinstance(root, exp.Query):
            raise PolicyViolation("READ_ONLY", "只允许 SELECT/CTE/UNION 查询")
        return root

    def _validate(self, root: exp.Expression, *, allow_internal_columns: bool) -> set[str]:
        forbidden_types = tuple(
            expression_type
            for name in (
                "Alter",
                "Command",
                "Copy",
                "Create",
                "Delete",
                "Drop",
                "Grant",
                "Insert",
                "LoadData",
                "Lock",
                "Merge",
                "Replace",
                "Revoke",
                "Set",
                "Transaction",
                "TruncateTable",
                "Update",
                "Use",
            )
            if (expression_type := getattr(exp, name, None)) is not None
        )
        if forbidden_types and any(root.find_all(*forbidden_types)):
            raise PolicyViolation("READ_ONLY", "SQL 包含非只读操作")

        variable_types = tuple(
            expression_type
            for name in ("Parameter", "PropertyEQ", "SessionParameter")
            if (expression_type := getattr(exp, name, None)) is not None
        )
        if variable_types and any(root.find_all(*variable_types)):
            raise PolicyViolation("SESSION_VARIABLE", "不允许读取或修改数据库会话变量")

        with_expression = root.args.get("with_")
        if with_expression is not None and with_expression.args.get("recursive"):
            raise PolicyViolation("RECURSIVE_CTE", "不允许递归 CTE")

        for join in root.find_all(exp.Join):
            if join.side:
                raise PolicyViolation("OUTER_JOIN_NOT_ALLOWED", "当前业务查询不支持外连接")

        self._validate_stars(root)
        self._validate_functions(root)

        physical_tables: set[str] = set()
        for scope in traverse_scope(root):
            scope_tables = self._physical_sources(scope)
            physical_tables.update(table_name for _, table_name, _ in scope_tables)
            self._validate_columns(scope, scope_tables, allow_internal_columns=allow_internal_columns)

        if not physical_tables:
            raise PolicyViolation("NO_TABLE", "查询必须访问授权业务表")
        return physical_tables

    def _physical_sources(self, scope: Scope) -> list[tuple[str, str, exp.Table]]:
        sources: list[tuple[str, str, exp.Table]] = []
        for alias, source in scope.sources.items():
            if not isinstance(source, exp.Table):
                continue
            table_name = source.name.lower()
            if source.db or source.catalog:
                raise PolicyViolation("QUALIFIED_TABLE", "不允许跨库或带 catalog 查询")
            if table_name in self._denied_tables:
                raise PolicyViolation("DENIED_TABLE", f"表 {table_name} 被永久禁止查询")
            if table_name not in self._allowed_tables:
                raise PolicyViolation("TABLE_NOT_ALLOWED", f"当前身份无权查询表 {table_name}")
            if table_name not in self._table_columns:
                raise PolicyViolation("MISSING_SCHEMA", f"表 {table_name} 缺少可信字段清单")
            if table_name not in self._global_tables and "tenant_id" not in self._table_columns[table_name]:
                raise PolicyViolation("NO_TENANT_COLUMN", f"表 {table_name} 无法实施租户隔离")
            sources.append((alias.lower(), table_name, source))
        return sources

    def _validate_stars(self, root: exp.Expression) -> None:
        for star in root.find_all(exp.Star):
            if isinstance(star.parent, exp.Count):
                continue
            raise PolicyViolation("WILDCARD_COLUMN", "禁止 SELECT *，必须明确列名")

    @staticmethod
    def _function_name(function: exp.Func) -> str:
        if isinstance(function, exp.Anonymous):
            return str(function.this).upper()
        return str(function.sql_name()).upper()

    def _validate_functions(self, root: exp.Expression) -> None:
        for function in root.find_all(exp.Func):
            function_name = self._function_name(function)
            if function_name not in self.ALLOWED_FUNCTIONS:
                raise PolicyViolation("FUNCTION_NOT_ALLOWED", f"函数 {function_name} 不在白名单中")

    def _validate_columns(
        self,
        scope: Scope,
        scope_tables: list[tuple[str, str, exp.Table]],
        *,
        allow_internal_columns: bool,
    ) -> None:
        alias_to_table = {alias: table for alias, table, _ in scope_tables}
        derived_columns = {
            alias.lower(): {name.lower() for name in source.expression.named_selects}
            for alias, source in scope.sources.items()
            if isinstance(source, Scope)
        }
        select_aliases = {
            expression.alias.lower() for expression in getattr(scope.expression, "expressions", []) if expression.alias
        }

        allowed_columns = self._table_columns if allow_internal_columns else self._selectable_columns

        for column in scope.columns:
            column_name = column.name.lower()
            qualifier = column.table.lower()
            if column_name in self._denied_columns:
                raise PolicyViolation("DENIED_COLUMN", f"字段 {column.name} 被永久禁止查询")

            if qualifier:
                if qualifier in alias_to_table:
                    table_name = alias_to_table[qualifier]
                    if column_name not in allowed_columns[table_name]:
                        raise PolicyViolation(
                            "COLUMN_NOT_ALLOWED",
                            f"字段 {table_name}.{column.name} 不在可信字段清单中",
                        )
                elif qualifier not in derived_columns:
                    raise PolicyViolation("UNKNOWN_ALIAS", f"未知表别名 {column.table}")
                elif column_name not in derived_columns[qualifier]:
                    raise PolicyViolation("COLUMN_NOT_ALLOWED", f"派生表字段 {column.name} 不在投影中")
                continue

            # 物理字段优先解析，不能通过同名投影别名或旁边的派生表隐藏越权字段。
            physical_matches = [table for table in alias_to_table.values() if column_name in self._table_columns[table]]
            if physical_matches:
                if any(column_name not in allowed_columns[table] for table in physical_matches):
                    raise PolicyViolation("COLUMN_NOT_ALLOWED", f"字段 {column.name} 不在可信字段清单中")
                continue
            if column_name in select_aliases:
                ancestor = column.parent
                while ancestor is not None and ancestor is not scope.expression:
                    if isinstance(ancestor, (exp.Order, exp.Group, exp.Having)):
                        break
                    ancestor = ancestor.parent
                if isinstance(ancestor, (exp.Order, exp.Group, exp.Having)):
                    continue
            if any(column_name in columns for columns in derived_columns.values()):
                continue
            raise PolicyViolation("COLUMN_NOT_ALLOWED", f"字段 {column.name} 不在可信字段清单中")

    def _rewrite(self, root: exp.Expression) -> None:
        for scope in traverse_scope(root):
            predicates: list[exp.Expression] = []
            for alias, table_name, _ in self._physical_sources(scope):
                columns = self._table_columns[table_name]
                if table_name not in self._global_tables:
                    predicates.append(
                        exp.column("TENANT_ID", table=alias).eq(exp.Literal.number(self.policy.tenant_id))
                    )
                if "operater_status" in columns:
                    predicates.append(
                        exp.GT(
                            this=exp.column("OPERATER_STATUS", table=alias),
                            expression=exp.Literal.number(0),
                        )
                    )
                if table_name in self._staff_scope_columns:
                    staff_column = self._staff_scope_columns[table_name]
                    predicates.append(
                        exp.column(staff_column, table=alias).eq(exp.Literal.number(self.policy.staff_id))
                    )
                if table_name in self._project_scope_columns:
                    if not self.policy.allowed_project_ids:
                        raise PolicyViolation("NO_PROJECT_ACCESS", "当前员工没有可访问项目")
                    project_column = self._project_scope_columns[table_name]
                    predicates.append(
                        exp.column(project_column, table=alias).isin(
                            *[exp.Literal.number(project_id) for project_id in self.policy.allowed_project_ids]
                        )
                    )

            if predicates:
                combined = predicates[0]
                for predicate in predicates[1:]:
                    combined = exp.and_(combined, predicate)
                scope.expression.where(combined, append=True, copy=False)

    def _apply_limit(self, root: exp.Expression) -> None:
        if root.args.get("offset") is not None:
            raise PolicyViolation("OFFSET_NOT_ALLOWED", "不允许使用 OFFSET 分页")
        limit = root.args.get("limit")
        if limit is None:
            root.limit(self.policy.max_rows, copy=False)
            return
        expression = limit.expression
        if not isinstance(expression, exp.Literal) or not expression.is_int:
            root.limit(self.policy.max_rows, copy=False)
            return
        if int(expression.this) > self.policy.max_rows:
            root.limit(self.policy.max_rows, copy=False)

    def _effective_limit(self, root: exp.Expression) -> int:
        limit = root.args.get("limit")
        if limit is None or not isinstance(limit.expression, exp.Literal):
            return self.policy.max_rows
        return int(limit.expression.this)

    def _verify_enforcement(self, root: exp.Expression) -> None:
        """确认可信过滤条件在序列化和二次解析后仍然存在。"""
        for scope in traverse_scope(root):
            where = scope.expression.args.get("where")
            where_sql = where.sql(dialect="mysql").lower() if where is not None else ""
            for alias, table_name, _ in self._physical_sources(scope):
                if table_name not in self._global_tables:
                    expected = f"{alias}.tenant_id = {self.policy.tenant_id}"
                    if expected not in where_sql:
                        raise PolicyViolation("TENANT_REWRITE_FAILED", "租户过滤条件注入失败")
                if "operater_status" in self._table_columns[table_name]:
                    expected = f"{alias}.operater_status > 0"
                    if expected not in where_sql:
                        raise PolicyViolation("STATUS_REWRITE_FAILED", "有效数据过滤条件注入失败")
                if table_name in self._staff_scope_columns:
                    column = self._staff_scope_columns[table_name]
                    expected = f"{alias}.{column} = {self.policy.staff_id}"
                    if expected not in where_sql:
                        raise PolicyViolation("STAFF_REWRITE_FAILED", "员工过滤条件注入失败")
                if table_name in self._project_scope_columns:
                    column = self._project_scope_columns[table_name]
                    expected = f"{alias}.{column} in ("
                    if expected not in where_sql:
                        raise PolicyViolation("PROJECT_REWRITE_FAILED", "项目过滤条件注入失败")
