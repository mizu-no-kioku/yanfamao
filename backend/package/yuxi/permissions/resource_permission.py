"""统一解析 Agent、Skill 与知识库的共享权限。"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ResourcePermission(StrEnum):
    """资源权限等级，数值顺序用于判断权限是否足够。"""

    NONE = "none"
    READ = "read"
    MANAGE = "manage"


class ResourcePermissionDenied(PermissionError):
    """当前用户的资源权限不足。"""


class ShareableResource(Protocol):
    """声明可通过共享配置进行权限解析的资源字段。"""

    created_by: str | None
    share_config: dict | None


@dataclass(frozen=True)
class ResourcePermissionPolicy:
    """声明资源类型允许的角色上限，不包含共享范围匹配逻辑。"""

    role_ceiling: dict[str, ResourcePermission]


RESOURCE_PERMISSION_ORDER = {
    ResourcePermission.NONE: 0,
    ResourcePermission.READ: 1,
    ResourcePermission.MANAGE: 2,
}

DEFAULT_SCOPE = {"access_levels": ["global"], "department_ids": [], "user_uids": []}
# 规范化顺序：global 在前，判定与输出都稳定
SCOPE_ACCESS_LEVELS = ("global", "department", "user")
KNOWLEDGE_BASE_PERMISSION_POLICY = ResourcePermissionPolicy(
    role_ceiling={
        "user": ResourcePermission.READ,
        "admin": ResourcePermission.MANAGE,
        "superadmin": ResourcePermission.MANAGE,
    }
)
AGENT_PERMISSION_POLICY = ResourcePermissionPolicy(
    role_ceiling={
        "user": ResourcePermission.MANAGE,
        "admin": ResourcePermission.MANAGE,
        "superadmin": ResourcePermission.MANAGE,
    }
)
SKILL_PERMISSION_POLICY = AGENT_PERMISSION_POLICY


def _normalize_access_levels(scope: Mapping[str, Any], *, strict: bool) -> list[str]:
    """读取范围档位；旧形状（单个 access_level）归一为单元素数组。

    这是 `access_level` 键在本仓**唯一**的读取入口（另一处是迁移 SQL）。两个键都没有时
    显式失败——旧实现的 `or "global"` 会把空范围悄悄放成全局可见，且让"字段缺失"与
    "显式全局"无法区分。

    `strict` 表示写入路径（现有语义：调用方是保存入口）：此时**不接受**旧键，否则浏览器
    缓存里的老 bundle 只要提交一次就会把范围按缺失字段落成默认值。读路径（strict=False）
    继续接受旧键，存量行靠它。
    """

    if "access_levels" in scope:
        raw = scope.get("access_levels") or []
    elif "access_level" in scope:
        if strict:
            raise ValueError("共享范围必须显式使用 access_levels")
        raw = [scope.get("access_level")]
    else:
        raise ValueError("共享范围必须声明 access_levels")
    if isinstance(raw, str) or not isinstance(raw, (list, tuple, set, frozenset)):
        raise ValueError("access_levels 必须是数组")
    levels = {str(value) for value in raw if str(value).strip()}
    unknown = levels - set(SCOPE_ACCESS_LEVELS)
    if unknown:
        raise ValueError(f"无效的资源权限范围：{sorted(unknown)}")
    if not levels:
        raise ValueError("access_levels 不能为空")
    if "global" in levels and len(levels) > 1:
        raise ValueError("global 范围不能与其他范围并存")
    return [level for level in SCOPE_ACCESS_LEVELS if level in levels]


def _normalize_scope(scope: dict | None, *, strict: bool) -> dict | None:
    """规范化共享范围并校验其访问级别与成员列表。"""

    if scope is None:
        return None
    if not isinstance(scope, dict):
        raise ValueError("权限范围必须是对象")

    access_levels = _normalize_access_levels(scope, strict=strict)
    if access_levels == ["global"]:
        # 写入路径拒绝"global + 名单"这种自相矛盾形状；读路径保持宽容（存量行靠它）
        if strict and (scope.get("department_ids") or scope.get("user_uids")):
            raise ValueError("全局范围不能同时携带部门或用户名单")
        return DEFAULT_SCOPE.copy()

    department_ids = sorted({int(value) for value in scope.get("department_ids") or []})
    user_uids = sorted({str(value).strip() for value in scope.get("user_uids") or [] if str(value).strip()})
    if "department" in access_levels:
        if not department_ids:
            raise ValueError("部门权限至少需要选择一个部门")
    elif department_ids:
        raise ValueError("未声明部门范围时不能带 department_ids")
    if "user" in access_levels and not user_uids:
        raise ValueError("指定用户权限至少需要选择一个用户")
    if "user" not in access_levels and user_uids:
        raise ValueError("未声明指定用户范围时不能带 user_uids")
    return {"access_levels": access_levels, "department_ids": department_ids, "user_uids": user_uids}


def _validate_manage_scope(read_scope: dict | None, manage_scope: dict | None) -> None:
    """确保管理范围不会超出读取范围；逐个主体判断，不做跨档推导。

    两侧范围都可以缺省：`read_scope is None` 是既有的「只配管理范围」形状（此时读取权限由
    管理范围派生，见 `resolve_resource_permission`），不能被当成越界。
    """

    if not read_scope or not manage_scope:
        return
    if not scope_contains(read_scope, manage_scope):
        raise ValueError("管理范围必须包含在读取范围内")


def normalize_permission_config(
    share_config: dict | None,
    *,
    allowed_access_levels: Collection[str] | None = None,
    unauthorized_access_level_message: str = "当前用户无权使用该资源共享范围",
    strict: bool = False,
) -> dict:
    """规范化并校验 v2 共享配置。"""

    config = share_config if isinstance(share_config, dict) else {}
    if config.get("version") == 2:
        read_scope = _normalize_scope(config.get("read_scope"), strict=strict)
        manage_scope = _normalize_scope(config.get("manage_scope"), strict=strict)
        try:
            _validate_manage_scope(read_scope, manage_scope)
        except ValueError:
            if strict:
                raise
            # 读取历史配置时保持原值；保存时由 strict 校验拒绝越界配置。
        normalized = {
            "version": 2,
            "read_scope": read_scope,
            "manage_scope": manage_scope,
        }
        if allowed_access_levels is not None:
            allowed = set(allowed_access_levels)
            for scope in (normalized["read_scope"], normalized["manage_scope"]):
                # 并集里每一档都必须被允许：出现任一档即可放宽，等于绕过授予上限。
                if scope and not set(scope["access_levels"]).issubset(allowed):
                    raise ValueError(unauthorized_access_level_message)
        return normalized
    raise ValueError("资源共享配置必须使用 version 2")


def scope_matches(user: Any, scope: dict | None) -> bool:
    """判断用户是否命中一个共享范围；范围必须是归一后的形状。

    只读 `access_levels`，因此未经 `normalize_permission_config` 的旧形状一律不命中——
    这是有意为之的 fail-closed：漏了一处归一，结果是"看不到"，而不是"所有人都能看到"。
    """

    if not scope:
        return False
    levels = scope.get("access_levels") or []
    if "global" in levels:
        return True
    if "department" in levels:
        department_id = _value(user, "department_id")
        try:
            if department_id is not None and int(department_id) in set(scope.get("department_ids") or []):
                return True
        except (TypeError, ValueError):
            pass
    if "user" in levels:
        return str(_value(user, "uid", "") or "") in {str(uid) for uid in scope.get("user_uids") or []}
    return False


def scope_contains(container: dict | None, target: dict | None) -> bool:
    """判断 container 是否完整覆盖 target。

    逐个主体判断：target 的每个部门 id 必须出现在 container.department_ids 里，每个 uid 必须
    出现在 container.user_uids 里；container 含 global 时覆盖一切，target 含 global 时除了
    container 也是 global 之外都不覆盖。**不做**「某用户属于某部门」的推导——那要查用户表，
    会把范围判定与用户目录耦合，且保守方向是 fail-closed。
    """

    if not target:
        return True
    if not container:
        return False
    container_levels = set(container.get("access_levels") or [])
    target_levels = set(target.get("access_levels") or [])
    if "global" in container_levels:
        return True
    if "global" in target_levels:
        return False
    container_departments = {int(value) for value in container.get("department_ids") or []}
    container_uids = {str(value) for value in container.get("user_uids") or []}
    return {int(value) for value in target.get("department_ids") or []}.issubset(container_departments) and {
        str(value) for value in target.get("user_uids") or []
    }.issubset(container_uids)


def _value(source: Any, key: str, default: Any = None) -> Any:
    """从字典或对象读取属性，统一权限解析的输入访问方式。"""

    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _minimum_permission(left: ResourcePermission, right: ResourcePermission) -> ResourcePermission:
    """按权限等级顺序返回两者中更低的权限。"""

    return left if RESOURCE_PERMISSION_ORDER[left] <= RESOURCE_PERMISSION_ORDER[right] else right


def resolve_resource_permission(
    user: Any,
    resource: ShareableResource,
    policy: ResourcePermissionPolicy,
) -> ResourcePermission:
    """解析资源所有权、共享范围和角色上限后的有效权限。"""

    if _value(user, "role") == "superadmin":
        return ResourcePermission.MANAGE

    raw_share_config = _value(resource, "share_config")
    config = normalize_permission_config(
        raw_share_config,
    )
    if str(_value(resource, "created_by", "") or "") == str(_value(user, "uid", "") or ""):
        granted = ResourcePermission.MANAGE
    elif scope_matches(user, config["manage_scope"]) and (
        config["read_scope"] is None or scope_matches(user, config["read_scope"])
    ):
        granted = ResourcePermission.MANAGE
    elif scope_matches(user, config["read_scope"]):
        granted = ResourcePermission.READ
    else:
        granted = ResourcePermission.NONE

    ceiling = policy.role_ceiling.get(_value(user, "role"), ResourcePermission.NONE)
    return _minimum_permission(granted, ceiling)


def require_resource_permission(
    actual: ResourcePermission,
    required: ResourcePermission,
) -> None:
    """在权限不足时显式失败。"""

    if RESOURCE_PERMISSION_ORDER[actual] < RESOURCE_PERMISSION_ORDER[required]:
        raise ResourcePermissionDenied(f"需要 {required.value} 权限，当前为 {actual.value}")


def resolve_knowledge_base_permission(user: Any, resource: ShareableResource) -> ResourcePermission:
    """解析知识库权限。

    个人知识库采用 fail-closed 判定：只有创建者与超管拿到 MANAGE，其余人仅能被 read_scope
    的 user 级定向分享放宽到 READ。这里刻意不走通用 scope 判定——通用路径会照 share_config
    的字面值放行，一旦个人库的 read_scope 被误设成 global 或 department，陌生人就会拿到
    READ，个人库的可见性边界会被它自己的 share_config 悄悄撑破；因此除 user 级定向分享外
    一律 NONE。超管的放行与通用路径同形，且同样早于个人库判定，否则超管也会被夹成 NONE
    而看不到别人的个人库。共享库交回通用判定，创建者也会受当前角色上限约束。
    """

    if _value(user, "role") == "superadmin":
        return ResourcePermission.MANAGE

    if _value(resource, "scope") == "personal":
        if str(_value(resource, "created_by", "") or "") == str(_value(user, "uid", "") or ""):
            return ResourcePermission.MANAGE
        read_scope = normalize_permission_config(_value(resource, "share_config"))["read_scope"]
        if read_scope and read_scope["access_levels"] == ["user"] and scope_matches(user, read_scope):
            return ResourcePermission.READ
        return ResourcePermission.NONE

    return resolve_resource_permission(
        user,
        resource,
        KNOWLEDGE_BASE_PERMISSION_POLICY,
    )


def require_knowledge_base_permission(
    user: Any,
    resource: ShareableResource,
    required: ResourcePermission,
) -> ResourcePermission:
    """校验用户是否具备知识库所需权限，并返回实际权限。"""

    actual = resolve_knowledge_base_permission(user, resource)
    require_resource_permission(actual, required)
    return actual


def resolve_agent_permission(user: Any, resource: ShareableResource) -> ResourcePermission:
    """解析 Agent 权限。"""

    return resolve_resource_permission(
        user,
        resource,
        AGENT_PERMISSION_POLICY,
    )


def resolve_skill_permission(user: Any, resource: ShareableResource) -> ResourcePermission:
    """解析 Skill 权限。"""

    if _value(resource, "source_scope") == "personal":
        if str(_value(resource, "created_by", "") or "") == str(_value(user, "uid", "") or ""):
            return ResourcePermission.MANAGE
        return ResourcePermission.NONE

    return resolve_resource_permission(
        user,
        resource,
        SKILL_PERMISSION_POLICY,
    )
