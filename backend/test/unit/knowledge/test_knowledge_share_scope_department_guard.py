"""普通用户的定向分享必须落在本部门：只约束 role == "user"。"""

from __future__ import annotations

import pytest

from yuxi.knowledge.manager import KnowledgeBaseManager
from yuxi.permissions import ResourcePermissionDenied
from yuxi.repositories.user_repository import UserRepository

pytestmark = pytest.mark.asyncio


class _FakeUser:
    def __init__(self, uid: str, department_id: int | None) -> None:
        self.uid = uid
        self.department_id = department_id


@pytest.fixture
def manager(tmp_path) -> KnowledgeBaseManager:
    return KnowledgeBaseManager(str(tmp_path))


@pytest.fixture
def directory(monkeypatch) -> dict[str, _FakeUser]:
    """用仓库层的批量查询假装用户目录；查不到的 uid 就是「未知 uid」。"""
    users: dict[str, _FakeUser] = {}

    async def fake_list_by_uids(self, uids):
        return [users[uid] for uid in uids if uid in users]

    monkeypatch.setattr(UserRepository, "list_by_uids", fake_list_by_uids)
    return users


def _user_scope(uids: list[str]) -> dict:
    return {
        "version": 2,
        "read_scope": {"access_level": "user", "department_ids": [], "user_uids": uids},
        "manage_scope": None,
    }


async def test_plain_user_can_share_with_own_department(manager, directory):
    directory["peer"] = _FakeUser("peer", 7)
    directory["me"] = _FakeUser("me", 7)

    await manager._ensure_share_scope_within_operator_department(
        _user_scope(["me", "peer"]), operator_role="user", operator_department_id=7
    )


async def test_plain_user_cannot_share_outside_own_department(manager, directory):
    directory["me"] = _FakeUser("me", 7)
    directory["other_dept"] = _FakeUser("other_dept", 8)

    with pytest.raises(ResourcePermissionDenied, match="本部门"):
        await manager._ensure_share_scope_within_operator_department(
            _user_scope(["me", "other_dept"]), operator_role="user", operator_department_id=7
        )


async def test_plain_user_cannot_share_with_unknown_uid(manager, directory):
    directory["me"] = _FakeUser("me", 7)

    with pytest.raises(ResourcePermissionDenied, match="本部门"):
        await manager._ensure_share_scope_within_operator_department(
            _user_scope(["me", "ghost"]), operator_role="user", operator_department_id=7
        )


async def test_manage_scope_is_checked_too(manager, directory):
    directory["me"] = _FakeUser("me", 7)
    directory["other_dept"] = _FakeUser("other_dept", 8)
    share_config = {
        "version": 2,
        "read_scope": {"access_level": "user", "department_ids": [], "user_uids": ["me", "other_dept"]},
        "manage_scope": {"access_level": "user", "department_ids": [], "user_uids": ["other_dept"]},
    }

    with pytest.raises(ResourcePermissionDenied):
        await manager._ensure_share_scope_within_operator_department(
            share_config, operator_role="user", operator_department_id=7
        )


async def test_admin_and_superadmin_are_not_restricted(manager, directory):
    directory["other_dept"] = _FakeUser("other_dept", 8)

    for role in ("admin", "superadmin"):
        await manager._ensure_share_scope_within_operator_department(
            _user_scope(["other_dept"]), operator_role=role, operator_department_id=7
        )


async def test_plain_user_without_department_fails_closed(manager, directory):
    directory["peer"] = _FakeUser("peer", 7)

    with pytest.raises(ResourcePermissionDenied):
        await manager._ensure_share_scope_within_operator_department(
            _user_scope(["peer"]), operator_role="user", operator_department_id=None
        )


async def test_non_user_access_level_needs_no_lookup(manager, directory):
    """部门级/全局级范围归一化后 user_uids 为空，普通用户可以直接保存。"""
    share_config = {
        "version": 2,
        "read_scope": {"access_level": "department", "department_ids": [7], "user_uids": []},
        "manage_scope": None,
    }

    await manager._ensure_share_scope_within_operator_department(
        share_config, operator_role="user", operator_department_id=7
    )
