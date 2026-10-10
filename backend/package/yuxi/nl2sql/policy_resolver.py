"""把业务身份解析成完整的 QueryPolicy：身份 → 功能门 → 资源 → 行权限 → 策略。"""

from collections.abc import Collection
from dataclasses import dataclass

from yuxi.nl2sql import errors
from yuxi.nl2sql.directory import BusinessDirectory
from yuxi.nl2sql.principal import BusinessPrincipal
from yuxi.nl2sql.resource_policy import MENU_TO_RESOURCES, resolve_grants
from yuxi.nl2sql.table_policy import PERMANENTLY_DENIED_COLUMNS, PERMANENTLY_DENIED_TABLES, TABLE_UPPER_BOUNDS
from yuxi.permissions.sql_security import QueryPolicy

# 功能门菜单 CODE，与 resource_policy.yaml 的 menu_ids 对应；
# 单元测试 test_gate_menu_codes_match_gate_menu_ids 守卫两边一致。
_GATE_MENU_CODES = frozenset({"AI", "QA", "projectManage:projectList:ai"})


@dataclass(frozen=True)
class BusinessSubject:
    """通过身份链校验的业务主体。"""

    user_id: int
    tenant_id: int
    staff_id: int


def resolve_business_subject(principal: BusinessPrincipal, directory: BusinessDirectory) -> BusinessSubject:
    """校验用户、确定租户、要求租户内唯一有效员工。任一不成立即拒绝。"""
    if not directory.find_active_user(principal.user_id):
        raise errors.PolicyDenial(errors.IDENTITY_USER_MISSING, "业务用户不存在或已失效")

    tenants = directory.find_tenants_for_user(principal.user_id)
    if not tenants:
        raise errors.PolicyDenial(errors.IDENTITY_USER_NO_TENANT, "业务用户没有任何租户归属")

    if principal.tenant_id is None:
        if len(tenants) > 1:
            raise errors.PolicyDenial(
                errors.TENANT_AMBIGUOUS,
                f"用户归属 {len(tenants)} 个租户，必须显式指定租户，不猜测",
            )
        tenant_id = tenants[0]
    else:
        if principal.tenant_id not in tenants:
            raise errors.PolicyDenial(errors.IDENTITY_TENANT_NOT_OWNED, "指定的租户与用户归属不符")
        tenant_id = principal.tenant_id

    staff_ids = directory.find_active_staff_ids(principal.user_id, tenant_id)
    if not staff_ids:
        raise errors.PolicyDenial(errors.IDENTITY_STAFF_MISSING, "该租户下没有有效员工记录")
    if len(staff_ids) > 1:
        raise errors.PolicyDenial(
            errors.IDENTITY_STAFF_AMBIGUOUS,
            f"该租户下有 {len(staff_ids)} 个有效员工，关联歧义",
        )

    return BusinessSubject(user_id=principal.user_id, tenant_id=tenant_id, staff_id=staff_ids[0])


def resolve_function_gate(subject: BusinessSubject, directory: BusinessDirectory) -> tuple[int, ...]:
    """校验角色基数与功能门。

    角色数量必须对 tb_user_enterprise_role_tenant 的原始关系行计数：
    对角色—菜单 JOIN 计数会把一个角色放大成上百个角色。
    """
    relations = directory.find_role_relation_ids(subject.user_id, subject.tenant_id)
    if not relations:
        raise errors.PolicyDenial(errors.ROLE_MISSING, "该租户下没有有效角色关系")
    if len(relations) > 1:
        raise errors.PolicyDenial(
            errors.ROLE_CARDINALITY_INVALID,
            f"该租户下有 {len(relations)} 条角色关系，违反唯一约束，按数据异常拒绝",
        )

    active_roles = directory.find_active_role_ids(relations)
    if not active_roles:
        raise errors.PolicyDenial(errors.ROLE_INACTIVE, "角色关系指向的角色不存在或已失效")

    menu_codes = directory.find_menu_codes(active_roles, subject.tenant_id)
    if not (menu_codes & _GATE_MENU_CODES):
        raise errors.PolicyDenial(
            errors.FUNCTION_GATE_DENIED,
            f"未命中 AI 功能菜单（{sorted(_GATE_MENU_CODES)}），当前可见菜单 {sorted(menu_codes)}",
        )
    return active_roles


def resolve_resource_codes(
    subject: BusinessSubject, role_ids: Collection[int], directory: BusinessDirectory
) -> frozenset[str]:
    """菜单 → 资源码。未在 menu_binding 中声明的菜单不产生资源。"""
    menu_codes = directory.find_menu_codes(tuple(role_ids), subject.tenant_id)
    codes: set[str] = set()
    for menu_code in menu_codes:
        codes.update(MENU_TO_RESOURCES.get(menu_code, ()))
    return frozenset(codes)


@dataclass(frozen=True)
class ResolvedPolicy:
    subject: BusinessSubject
    resource_codes: frozenset[str]
    policy: QueryPolicy


def resolve_query_policy(principal: BusinessPrincipal, directory: BusinessDirectory) -> ResolvedPolicy:
    """完整解析：身份 → 功能门 → 资源 → 行权限 → QueryPolicy。"""
    subject = resolve_business_subject(principal, directory)
    role_ids = resolve_function_gate(subject, directory)
    resource_codes = resolve_resource_codes(subject, role_ids, directory)

    grants = resolve_grants(resource_codes)
    allowed_tables: set[str] = set()
    table_columns: dict[str, frozenset[str]] = {}
    max_rows = 200
    for grant in grants:
        allowed_tables |= grant.tables
        for table, columns in grant.columns.items():
            table_columns[table] = table_columns.get(table, frozenset()) | columns
        max_rows = min(max_rows, grant.max_rows)

    if not allowed_tables:
        raise errors.PolicyDenial(errors.FUNCTION_GATE_DENIED, "当前角色没有可用的数据资源")

    project_ids = directory.find_authorized_project_ids(subject.tenant_id, subject.staff_id)
    if not project_ids:
        raise errors.PolicyDenial(errors.NO_PROJECT_ACCESS, "当前员工没有可访问的业务项目")

    project_scope_columns = {
        table: TABLE_UPPER_BOUNDS[table].project_column
        for table in allowed_tables
        if TABLE_UPPER_BOUNDS[table].project_column
    }

    policy = QueryPolicy(
        tenant_id=subject.tenant_id,
        staff_id=subject.staff_id,
        allowed_tables=frozenset(allowed_tables),
        table_columns=table_columns,
        allowed_project_ids=tuple(project_ids),
        project_scope_columns=project_scope_columns,
        global_tables=frozenset(),
        denied_tables=PERMANENTLY_DENIED_TABLES,
        denied_columns=PERMANENTLY_DENIED_COLUMNS,
        max_rows=max_rows,
    )
    return ResolvedPolicy(subject=subject, resource_codes=resource_codes, policy=policy)
