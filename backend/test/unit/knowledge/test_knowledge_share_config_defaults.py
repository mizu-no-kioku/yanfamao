from __future__ import annotations

import pytest

from yuxi.knowledge.manager import KnowledgeBaseManager


@pytest.fixture
def manager(tmp_path) -> KnowledgeBaseManager:
    """构造只绑定临时工作目录的管理器，用于验证共享配置的默认值。"""
    return KnowledgeBaseManager(str(tmp_path))


def test_personal_default_scope_is_creator_only(manager):
    normalized = manager._normalize_share_config(None, user_uid="u-1", department_id=None, scope="personal")

    assert normalized["read_scope"] == {
        "access_level": "user",
        "department_ids": [],
        "user_uids": ["u-1"],
    }


def test_shared_default_scope_uses_creator_department(manager):
    normalized = manager._normalize_share_config(None, user_uid="u-1", department_id=7, scope="shared")

    assert normalized["read_scope"] == {
        "access_level": "department",
        "department_ids": [7],
        "user_uids": [],
    }


def test_shared_without_department_fails_closed_to_creator_only(manager):
    """没有部门时必须退到“仅创建者”——写成 global 会让一个本该私有的库人人可见。"""
    normalized = manager._normalize_share_config(None, user_uid="u-1", department_id=None, scope="shared")

    assert normalized["read_scope"] == {
        "access_level": "user",
        "department_ids": [],
        "user_uids": ["u-1"],
    }
    assert normalized["read_scope"]["access_level"] != "global"
