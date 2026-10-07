"""将知识库领域权限校验适配为 FastAPI 依赖。"""

from fastapi import Depends, HTTPException

from server.utils.auth_middleware import get_required_user
from yuxi.knowledge.read_models import KnowledgeBaseDetail
from yuxi.knowledge.runtime import knowledge_base
from yuxi.permissions import (
    ResourcePermission,
    ResourcePermissionDenied,
    require_knowledge_base_permission,
)
from yuxi.storage.postgres.models_business import User


async def ensure_knowledge_base_permission(
    kb_id: str,
    current_user: User,
    required: ResourcePermission,
) -> KnowledgeBaseDetail:
    """加载知识库并校验当前用户的有效资源权限。"""

    db_info = await knowledge_base.get_database_info(kb_id)
    if not db_info:
        raise HTTPException(status_code=404, detail=f"知识库 {kb_id} 不存在")

    try:
        require_knowledge_base_permission(current_user, db_info, required)
    except ResourcePermissionDenied as error:
        raise HTTPException(status_code=403, detail="无权操作该知识库") from error
    return db_info


async def require_knowledge_base_read(
    kb_id: str,
    current_user: User = Depends(get_required_user),
) -> User:
    """校验当前用户对指定知识库的读取权限。

    底座是「任意已登录且绑定部门的用户」而不是管理员：授权完全由 resolve_knowledge_base_permission
    按知识库的 scope 判定。如果这里退回 get_admin_user，普通用户的个人知识库与共享知识库都会不可用。
    """

    await ensure_knowledge_base_permission(kb_id, current_user, ResourcePermission.READ)
    return current_user


async def require_knowledge_base_manage(
    kb_id: str,
    current_user: User = Depends(get_required_user),
) -> User:
    """校验当前用户对指定知识库的管理权限。

    底座同样放宽到「任意已登录且绑定部门的用户」：写权限不是角色特权，而是由知识库的
    manage_scope（个人库则只有创建者与超管）在 resolve_knowledge_base_permission 里决定。
    退回 get_admin_user 会让普通用户无法管理自己的个人知识库。
    """

    await ensure_knowledge_base_permission(kb_id, current_user, ResourcePermission.MANAGE)
    return current_user
