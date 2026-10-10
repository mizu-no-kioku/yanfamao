"""把业务身份解析成完整的 QueryPolicy。本任务只实现身份链部分。"""

from dataclasses import dataclass

from yuxi.nl2sql import errors
from yuxi.nl2sql.directory import BusinessDirectory
from yuxi.nl2sql.principal import BusinessPrincipal


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
        raise errors.PolicyDenial(errors.IDENTITY_USER_MISSING, "业务用户没有任何租户归属")

    if principal.tenant_id is None:
        if len(tenants) > 1:
            raise errors.PolicyDenial(
                errors.TENANT_AMBIGUOUS,
                f"用户归属 {len(tenants)} 个租户，必须显式指定租户，不猜测",
            )
        tenant_id = tenants[0]
    else:
        if principal.tenant_id not in tenants:
            raise errors.PolicyDenial(errors.IDENTITY_USER_MISSING, "指定的租户与用户归属不符")
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
