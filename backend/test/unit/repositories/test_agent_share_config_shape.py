"""Agent/Skill 共享范围的默认形状与"全局"判断。"""

from __future__ import annotations

import pytest
from yuxi.repositories.agent_repository import (
    DEFAULT_SHARE_CONFIG,
    get_allowed_agent_access_levels,
    normalize_agent_share_config,
)
from yuxi.services.skills.shared import BUILTIN_SKILL_SHARE_CONFIG, normalize_skill_share_config

pytestmark = [pytest.mark.unit]


def test_agent_default_share_config_is_global_union_shape():
    assert DEFAULT_SHARE_CONFIG["read_scope"] == {
        "access_levels": ["global"],
        "department_ids": [],
        "user_uids": [],
    }


def test_agent_default_keeps_global_semantics():
    """默认 Agent 范围解析为全局——与既有的 DEFAULT_SHARE_CONFIG 语义一致，只换形状。"""
    config = normalize_agent_share_config(None)

    assert config["read_scope"]["access_levels"] == ["global"]


def test_agent_and_skill_union_config_is_preserved():
    union = {
        "version": 2,
        "read_scope": {"access_levels": ["department", "user"], "department_ids": [2], "user_uids": ["u-7"]},
        "manage_scope": None,
    }

    assert normalize_agent_share_config(union)["read_scope"]["user_uids"] == ["u-7"]
    assert normalize_skill_share_config(union, operator_uid="u-1")["read_scope"]["access_levels"] == [
        "department",
        "user",
    ]


def test_non_admin_cannot_select_department_level():
    class _User:
        role = "user"
        department_id = 1

    assert get_allowed_agent_access_levels(_User()) == ["user"]
    with pytest.raises(ValueError, match="无权"):
        normalize_agent_share_config(
            {
                "version": 2,
                "read_scope": {"access_levels": ["department"], "department_ids": [1]},
                "manage_scope": None,
            },
            allowed_access_levels={"user"},
        )


def test_builtin_share_config_is_global_union_shape():
    assert BUILTIN_SKILL_SHARE_CONFIG == {"access_levels": ["global"], "department_ids": [], "user_uids": []}
