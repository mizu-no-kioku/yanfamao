from types import SimpleNamespace

import pytest

from yuxi.permissions import (
    ResourcePermission,
    ResourcePermissionDenied,
    require_knowledge_base_permission,
    resolve_agent_permission,
    resolve_knowledge_base_permission,
    resolve_skill_permission,
)


def _user(uid="user-1", role="user", department_id=1):
    return SimpleNamespace(uid=uid, role=role, department_id=department_id)


def _resource(created_by="owner", share_config=None):
    return SimpleNamespace(created_by=created_by, share_config=share_config)


def test_knowledge_base_global_read_and_department_manage():
    config = {
        "version": 2,
        "read_scope": {"access_level": "global"},
        "manage_scope": {"access_level": "department", "department_ids": [1]},
    }
    resource = _resource(share_config=config)

    assert resolve_knowledge_base_permission(_user(department_id=1), resource) == ResourcePermission.READ
    managing_admin = _user(uid="admin-1", role="admin", department_id=1)
    readonly_admin = _user(uid="other", role="admin", department_id=2)
    assert resolve_knowledge_base_permission(managing_admin, resource) == ResourcePermission.MANAGE
    assert resolve_knowledge_base_permission(readonly_admin, resource) == ResourcePermission.READ


def test_invalid_v2_scope_does_not_expand_read_access_when_reading():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "department", "department_ids": [1]},
            "manage_scope": {"access_level": "global"},
        }
    )

    assert resolve_knowledge_base_permission(_user(role="admin", department_id=2), resource) == ResourcePermission.NONE


def test_strict_config_rejects_manage_scope_outside_read_scope():
    from yuxi.permissions import normalize_permission_config

    with pytest.raises(ValueError, match="管理范围"):
        normalize_permission_config(
            {
                "version": 2,
                "read_scope": {"access_levels": ["department"], "department_ids": [1]},
                "manage_scope": {"access_levels": ["global"]},
            },
            strict=True,
        )


def test_strict_config_rejects_user_manage_scope_under_department_read_scope():
    from yuxi.permissions import normalize_permission_config

    with pytest.raises(ValueError, match="管理范围"):
        normalize_permission_config(
            {
                "version": 2,
                "read_scope": {"access_levels": ["department"], "department_ids": [1]},
                "manage_scope": {"access_levels": ["user"], "user_uids": ["user-1"]},
            },
            strict=True,
        )


def test_global_agent_scope_preserves_admin_management():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": {"access_level": "global"},
        }
    )

    assert resolve_agent_permission(_user(role="admin"), resource) == ResourcePermission.MANAGE


def test_user_agent_and_skill_scope_preserves_user_management():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "user", "user_uids": ["user-1"]},
            "manage_scope": {"access_level": "user", "user_uids": ["user-1"]},
        }
    )

    assert resolve_agent_permission(_user(), resource) == ResourcePermission.MANAGE
    assert resolve_skill_permission(_user(), resource) == ResourcePermission.MANAGE


def test_knowledge_base_owner_and_superadmin_can_manage():
    resource = _resource(created_by="owner", share_config={"version": 2})

    assert resolve_knowledge_base_permission(_user(uid="owner"), resource) == ResourcePermission.MANAGE
    assert resolve_knowledge_base_permission(_user(uid="owner", role="admin"), resource) == ResourcePermission.MANAGE
    assert resolve_knowledge_base_permission(_user(role="superadmin"), resource) == ResourcePermission.MANAGE


def test_global_knowledge_base_share_remains_manage_for_admin():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": {"access_level": "global"},
        }
    )

    assert resolve_knowledge_base_permission(_user(role="admin"), resource) == ResourcePermission.MANAGE
    assert resolve_knowledge_base_permission(_user(role="user"), resource) == ResourcePermission.READ


def test_legacy_permission_config_is_rejected_at_runtime():
    from yuxi.permissions import normalize_permission_config

    with pytest.raises(ValueError, match="version 2"):
        normalize_permission_config({"access_level": "department", "department_ids": [1]})


def test_agent_and_skill_use_shared_resolver_with_resource_policy():
    resource = _resource(share_config={"version": 2, "manage_scope": {"access_level": "user", "user_uids": ["user-2"]}})

    assert resolve_agent_permission(_user(uid="user-2"), resource) == ResourcePermission.MANAGE
    assert resolve_skill_permission(_user(uid="user-2"), resource) == ResourcePermission.MANAGE


def test_personal_skill_permission_is_limited_to_owner():
    resource = SimpleNamespace(source_scope="personal", created_by="user-1", share_config=None)

    assert resolve_skill_permission(_user(uid="user-1"), resource) == ResourcePermission.MANAGE
    assert resolve_skill_permission(_user(uid="user-2"), resource) == ResourcePermission.NONE


def test_manage_only_scope_also_grants_read_to_matching_users():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": None,
            "manage_scope": {"access_level": "department", "department_ids": [1]},
        }
    )

    assert (
        resolve_knowledge_base_permission(_user(role="admin", department_id=1), resource) == ResourcePermission.MANAGE
    )
    assert resolve_knowledge_base_permission(_user(department_id=1), resource) == ResourcePermission.READ
    assert resolve_knowledge_base_permission(_user(role="admin", department_id=2), resource) == ResourcePermission.NONE


def test_require_permission_rejects_insufficient_access():
    from yuxi.permissions import require_resource_permission

    with pytest.raises(ResourcePermissionDenied):
        require_resource_permission(ResourcePermission.READ, ResourcePermission.MANAGE)


def test_require_knowledge_base_permission_uses_resolved_resource_permission():
    resource = _resource(
        share_config={
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": None,
        }
    )

    assert (
        require_knowledge_base_permission(_user(role="admin"), resource, ResourcePermission.READ)
        == ResourcePermission.READ
    )
    with pytest.raises(ResourcePermissionDenied):
        require_knowledge_base_permission(_user(role="admin"), resource, ResourcePermission.MANAGE)


def test_v2_scope_validation_rejects_disallowed_access_level():
    from yuxi.permissions import normalize_permission_config

    with pytest.raises(ValueError, match="共享范围"):
        normalize_permission_config(
            {
                "version": 2,
                "read_scope": {"access_level": "global"},
                "manage_scope": None,
            },
            allowed_access_levels={"user"},
        )


def _kb(created_by="owner", share_config=None, scope="shared"):
    return SimpleNamespace(
        created_by=created_by,
        # 运行期知识库的 share_config 恒为规范化后的 v2 字典（knowledge/manager.py 的
        # _normalize_share_config 会把空列补成全局范围），传 None 会被直接拒绝。
        share_config=share_config
        if share_config is not None
        else {
            "version": 2,
            "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
            "manage_scope": None,
        },
        scope=scope,
    )


def _user_scope(user_uids):
    return {"access_level": "user", "department_ids": [], "user_uids": list(user_uids)}


def test_personal_knowledge_base_owner_gets_manage_even_for_plain_user_role():
    """个人库创建者拿到 MANAGE：创建者分支早于角色上限返回，普通角色不会被夹成只读。"""
    from yuxi.permissions import resolve_knowledge_base_permission

    owner = _user(uid="u-1", role="user", department_id=3)

    assert resolve_knowledge_base_permission(owner, _kb(created_by="u-1", scope="personal")) == (
        ResourcePermission.MANAGE
    )


def test_personal_knowledge_base_sharee_gets_read():
    from yuxi.permissions import resolve_knowledge_base_permission

    sharee = _user(uid="u-2", role="user", department_id=3)
    resource = _kb(
        created_by="u-1",
        share_config={"version": 2, "read_scope": _user_scope(["u-2"]), "manage_scope": None},
        scope="personal",
    )

    assert resolve_knowledge_base_permission(sharee, resource) == ResourcePermission.READ


def test_personal_knowledge_base_stranger_gets_none():
    from yuxi.permissions import resolve_knowledge_base_permission

    stranger = _user(uid="u-3", role="user", department_id=3)
    resource = _kb(
        created_by="u-1",
        share_config={"version": 2, "read_scope": _user_scope(["u-2"]), "manage_scope": None},
        scope="personal",
    )

    assert resolve_knowledge_base_permission(stranger, resource) == ResourcePermission.NONE


def test_personal_knowledge_base_superadmin_gets_manage():
    """超管管理一切：别人的个人库对超管仍是 MANAGE——个人库的回退是「仅创建者 + 超管可见」。"""
    from yuxi.permissions import resolve_knowledge_base_permission

    superadmin = _user(uid="u-9", role="superadmin", department_id=None)
    resource = _kb(
        created_by="u-1",
        share_config={"version": 2, "read_scope": _user_scope(["u-2"]), "manage_scope": None},
        scope="personal",
    )

    assert resolve_knowledge_base_permission(superadmin, resource) == ResourcePermission.MANAGE


@pytest.mark.parametrize(
    "read_scope",
    [
        pytest.param({"access_level": "global", "department_ids": [], "user_uids": []}, id="global"),
        pytest.param({"access_level": "department", "department_ids": [3], "user_uids": []}, id="department"),
    ],
)
def test_personal_knowledge_base_is_not_widened_by_global_or_department_scope(read_scope):
    """个人库只认 user 级定向分享：read_scope 被误设成 global/部门时，陌生人不该被放宽到 READ。"""
    from yuxi.permissions import resolve_knowledge_base_permission

    stranger = _user(uid="u-3", role="user", department_id=3)
    resource = _kb(
        created_by="u-1",
        share_config={"version": 2, "read_scope": read_scope, "manage_scope": None},
        scope="personal",
    )

    assert resolve_knowledge_base_permission(stranger, resource) == ResourcePermission.NONE


def test_shared_knowledge_base_non_owner_plain_user_with_matching_scope_gets_read():
    """共享库的非创建者普通用户命中 read_scope 时仍是 READ——角色上限继续生效。"""
    from yuxi.permissions import resolve_knowledge_base_permission

    reader = _user(uid="u-2", role="user", department_id=3)

    assert resolve_knowledge_base_permission(reader, _kb(created_by="u-1", scope="shared")) == (
        ResourcePermission.READ
    )


def test_shared_knowledge_base_plain_user_creator_keeps_manage():
    """既有行为记录（非本次目标）：创建者分支是绕过角色上限的早返回，普通用户对自己的共享库仍是 MANAGE。"""
    from yuxi.permissions import resolve_knowledge_base_permission

    owner = _user(uid="u-1", role="user", department_id=3)

    assert resolve_knowledge_base_permission(owner, _kb(created_by="u-1", scope="shared")) == (
        ResourcePermission.MANAGE
    )


def _new_scope(levels, department_ids=(), user_uids=()):
    return {
        "access_levels": list(levels),
        "department_ids": list(department_ids),
        "user_uids": list(user_uids),
    }


def test_union_scope_matches_department_member_and_named_user():
    """并集：部门成员与指定用户都命中。"""
    resource = _kb(
        created_by="owner",
        share_config={
            "version": 2,
            "read_scope": _new_scope(["department", "user"], department_ids=[3], user_uids=["u-7"]),
            "manage_scope": None,
        },
    )

    assert resolve_knowledge_base_permission(_user(uid="u-1", department_id=3), resource) == ResourcePermission.READ
    assert resolve_knowledge_base_permission(_user(uid="u-7", department_id=9), resource) == ResourcePermission.READ
    assert resolve_knowledge_base_permission(_user(uid="u-9", department_id=9), resource) == ResourcePermission.NONE


def test_legacy_scope_normalizes_to_single_level():
    """旧形状读时归一：判定结果与今天一致（这是存量数据的回归保护）。"""
    from yuxi.permissions import normalize_permission_config

    legacy = normalize_permission_config(
        {
            "version": 2,
            "read_scope": {"access_level": "department", "department_ids": [3], "user_uids": []},
            "manage_scope": None,
        }
    )

    assert legacy["read_scope"] == _new_scope(["department"], department_ids=[3])


def test_scope_matches_ignores_a_scope_that_was_never_normalized():
    """未归一的形状（两个字段都没有）不命中任何人——fail-closed，不是默认全局。"""
    from yuxi.permissions import scope_matches

    assert scope_matches(_user(department_id=3), {}) is False
    assert scope_matches(_user(department_id=3), {"access_level": "department", "department_ids": [3]}) is False


@pytest.mark.parametrize(
    "read_scope",
    [
        pytest.param(
            {"access_levels": ["user"], "department_ids": [1], "user_uids": ["u-1"]},
            id="user-with-departments",
        ),
        pytest.param(
            {"access_levels": ["department"], "department_ids": [], "user_uids": []},
            id="department-without-ids",
        ),
        pytest.param({"access_levels": ["user"], "department_ids": [], "user_uids": []}, id="user-without-uids"),
        pytest.param(
            {"access_levels": ["global", "user"], "department_ids": [], "user_uids": ["u-1"]},
            id="global-not-alone",
        ),
        pytest.param({"access_levels": [], "department_ids": [], "user_uids": []}, id="empty-levels"),
    ],
)
def test_scope_shape_rejects_self_contradicting_configs(read_scope):
    """只有一种合法表达：自相矛盾的形状显式失败，不静默清空。"""
    from yuxi.permissions import normalize_permission_config

    with pytest.raises(ValueError):
        normalize_permission_config({"version": 2, "read_scope": read_scope, "manage_scope": None})


def test_write_path_rejects_legacy_access_level_key():
    """写入路径（strict）只接受 access_levels；读路径仍接受旧键（存量行靠它）。

    这条规则把「旧 bundle + 新后端」的静默放大挡在写入口，而不是只靠前端自觉：
    老前端提交的 `{access_level: ...}` 会在写入时被拒，而不是被归一后落库。
    """
    from yuxi.permissions import normalize_permission_config

    legacy = {
        "version": 2,
        "read_scope": {"access_level": "department", "department_ids": [1]},
        "manage_scope": None,
    }

    assert normalize_permission_config(legacy)["read_scope"]["access_levels"] == ["department"]
    with pytest.raises(ValueError, match="access_levels"):
        normalize_permission_config(legacy, strict=True)


def test_global_scope_with_member_lists_is_rejected_on_write_and_canonicalized_on_read():
    """global 独占：写入路径拒绝携带名单的自相矛盾形状，读路径宽容为干净 global（存量行靠它）。"""
    from yuxi.permissions import normalize_permission_config

    contradictory = {
        "version": 2,
        "read_scope": {"access_levels": ["global"], "department_ids": [1]},
        "manage_scope": None,
    }

    assert normalize_permission_config(contradictory)["read_scope"] == _new_scope(["global"])
    with pytest.raises(ValueError, match="全局范围"):
        normalize_permission_config(contradictory, strict=True)


def test_v2_scope_validation_rejects_union_with_any_disallowed_level():
    """并集里每一档都必须被允许：只看第一档会漏掉越权档。"""
    from yuxi.permissions import normalize_permission_config

    read_scope = {"access_levels": ["department", "user"], "department_ids": [1], "user_uids": ["u-1"]}

    with pytest.raises(ValueError, match="共享范围"):
        normalize_permission_config(
            {"version": 2, "read_scope": read_scope, "manage_scope": None},
            allowed_access_levels={"user"},
        )


@pytest.mark.parametrize(
    ("container", "target", "expected"),
    [
        pytest.param(_new_scope(["global"]), _new_scope(["department"], department_ids=[1]), True, id="global-covers-all"),
        pytest.param(_new_scope(["department"], department_ids=[1]), _new_scope(["global"]), False, id="nothing-covers-global"),
        pytest.param(_new_scope(["department"], department_ids=[1]), _new_scope(["department"], department_ids=[1]), True, id="same-department"),
        pytest.param(_new_scope(["department"], department_ids=[1]), _new_scope(["department"], department_ids=[2]), False, id="other-department"),
        pytest.param(_new_scope(["user"], user_uids=["u-1"]), _new_scope(["user"], user_uids=["u-2"]), False, id="other-user"),
        pytest.param(_new_scope(["user"], user_uids=["u-1"]), _new_scope(["user"], user_uids=["u-1"]), True, id="same-user"),
        pytest.param(_new_scope(["user"], user_uids=["u-1"]), _new_scope(["department"], department_ids=[1]), False, id="cross-level-is-not-derivable"),
        pytest.param(
            _new_scope(["department", "user"], department_ids=[1], user_uids=["u-7"]),
            _new_scope(["user"], user_uids=["u-7"]),
            True,
            id="union-covers-its-part",
        ),
        pytest.param(
            _new_scope(["department", "user"], department_ids=[1], user_uids=["u-7"]),
            _new_scope(["department", "user"], department_ids=[1, 2], user_uids=["u-7"]),
            False,
            id="union-does-not-cover-superset",
        ),
        pytest.param(_new_scope(["department"], department_ids=[1]), None, True, id="empty-target"),
    ],
)
def test_scope_contains_is_principal_wise(container, target, expected):
    """包含关系逐个主体判断，不做「用户属于某部门」的跨档推导。"""
    from yuxi.permissions import scope_contains

    assert scope_contains(container, target) is expected


def test_manage_scope_must_be_covered_by_read_scope():
    """走真实归一器（写入路径 strict）验证包含关系：越界被拒，被并集覆盖则放行。"""
    from yuxi.permissions import normalize_permission_config

    with pytest.raises(ValueError, match="管理范围"):
        normalize_permission_config(
            {
                "version": 2,
                "read_scope": _new_scope(["department"], department_ids=[2]),
                "manage_scope": _new_scope(["department"], department_ids=[3]),
            },
            strict=True,
        )

    allowed = normalize_permission_config(
        {
            "version": 2,
            "read_scope": _new_scope(["department", "user"], department_ids=[2], user_uids=["u-7"]),
            "manage_scope": _new_scope(["user"], user_uids=["u-7"]),
        },
        strict=True,
    )
    assert allowed["manage_scope"]["access_levels"] == ["user"]


def test_skill_dependency_accepts_a_union_parent_scope():
    """技能依赖：依赖方的并集范围覆盖父技能的单档时放行，父技能含外部主体时拒绝。"""
    from yuxi.services.skills.shared import can_skill_depend_on

    def _skill(share_config, created_by="owner"):
        return SimpleNamespace(
            enabled=True,
            source_type="upload",
            created_by=created_by,
            share_config={"version": 2, "read_scope": share_config, "manage_scope": None},
        )

    union_parent = _skill(_new_scope(["department", "user"], department_ids=[2], user_uids=["u-7"]))
    department_only = _skill(_new_scope(["department"], department_ids=[2]))
    outsider = _skill(_new_scope(["user"], user_uids=["u-9"]))

    # 实参顺序：can_skill_depend_on(parent, dependency) 要求 dependency 的范围覆盖 parent
    # （既有 test_owner_only_skill_can_depend_on_visible_skill 钉住了这个方向），所以并集必须
    # 落在依赖方才能覆盖单档的父技能——brief 原文把并集放在了 parent 位，与它自己的期望值和
    # 计划 Review Focus #2「依赖方的范围是并集、父技能是单档时放行」相反。
    assert can_skill_depend_on(department_only, union_parent) is True
    assert can_skill_depend_on(outsider, union_parent) is False
