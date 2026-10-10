"""知识库共享范围的落库形状与存量 NULL 行的读取归一。"""

from __future__ import annotations

import tempfile

import pytest
from yuxi.knowledge.manager import KnowledgeBaseManager

pytestmark = pytest.mark.unit


def _manager() -> KnowledgeBaseManager:
    """构造只绑定临时工作目录的管理器；本文件的用例不触碰工作目录内容。"""
    return KnowledgeBaseManager(tempfile.mkdtemp(prefix="kb-share-config-shape-"))


def test_plain_user_personal_default_is_user_only():
    config = _manager()._normalize_share_config(None, user_uid="u-1", scope="personal")

    assert config["read_scope"] == {"access_levels": ["user"], "department_ids": [], "user_uids": ["u-1"]}


def test_shared_default_uses_creator_department():
    config = _manager()._normalize_share_config(None, user_uid="u-1", department_id=2, scope="shared")

    assert config["read_scope"] == {"access_levels": ["department"], "department_ids": [2], "user_uids": []}


def test_shared_without_department_falls_back_to_creator_only():
    """无部门 fail-closed：只留创建者，绝不写 global。"""
    config = _manager()._normalize_share_config(None, user_uid="u-1", scope="shared")

    assert config["read_scope"]["access_levels"] == ["user"]


def test_union_round_trips_through_normalization():
    """并集配置归一后仍是并集：部门与指定人同时保留，且不额外并入操作者本人。

    操作者对自己库的访问来自所有权判定（`resolve_resource_permission` 的所有者早返回），
    不靠把自己塞进名单；显式声明了名单就照单保存，否则用户无法主动排除自己所在部门。
    """
    config = _manager()._normalize_share_config(
        {
            "version": 2,
            "read_scope": {"access_levels": ["department", "user"], "department_ids": [2], "user_uids": ["u-7"]},
            "manage_scope": None,
        },
        user_uid="u-1",
        department_id=2,
    )

    assert config["read_scope"]["access_levels"] == ["department", "user"]
    assert config["read_scope"]["department_ids"] == [2]
    assert config["read_scope"]["user_uids"] == ["u-7"]


def test_explicit_list_is_not_widened_by_operator_identity():
    """显式给出的名单不得被操作者自己的部门撑大——那是一次静默放大授权。"""
    config = _manager()._normalize_share_config(
        {
            "version": 2,
            "read_scope": {"access_levels": ["department"], "department_ids": [2], "user_uids": []},
            "manage_scope": None,
        },
        user_uid="u-1",
        department_id=5,
    )

    assert config["read_scope"]["department_ids"] == [2]
