"""`GET /api/knowledge/databases/accessible` 的列表项形状。

普通用户的知识库列表走这条接口（跨库全量列表是管理员专属），列表项就是卡片的数据源：
缺 `row_count` 会把有内容的库显示成「0 文件」，缺 `share_config`/`created_at` 会分别拼出
「只读用户(0)」这种假标签和丢失创建时间前缀。字段与管理员列表同源，避免两套形状再次漂移。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server.routers import knowledge_router
from yuxi.knowledge.read_models import KnowledgeBaseSummary
from yuxi.permissions import ResourcePermission

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _database(**overrides) -> KnowledgeBaseSummary:
    """构造一条知识库摘要；默认是只读调用者看到的形态。"""

    fields: dict = {
        "kb_id": "kb-1",
        "name": "研发知识库",
        "description": "研发部门",
        "kb_type": "milvus",
        "embedding_model_spec": None,
        "llm_model_spec": None,
        "query_params": {},
        "additional_params": {},
        "share_config": {
            "version": 2,
            "read_scope": {"access_level": "department", "department_ids": [2], "user_uids": []},
            "manage_scope": None,
        },
        "created_by": "owner",
        "created_at": datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        "file_count": 5,
        "row_count": 5,
        "effective_permission": ResourcePermission.READ,
    }
    fields.update(overrides)
    return KnowledgeBaseSummary(**fields)


async def _list_item(monkeypatch, database: KnowledgeBaseSummary) -> dict:
    """按普通用户的身份取回列表项，只替换可见性查询，其余走真实路由装配。"""

    async def fake_get_databases_by_uid(uid: str):
        assert uid == "plain-user"
        return [database]

    monkeypatch.setattr(knowledge_router.knowledge_base, "get_databases_by_uid", fake_get_databases_by_uid)
    response = await knowledge_router.get_accessible_databases(current_user=SimpleNamespace(uid="plain-user"))
    items = [entry for entry in response["databases"] if entry["kb_id"] == database.kb_id]
    assert items, "列表里没有目标知识库"
    return items[0]


async def test_accessible_item_carries_card_fields(monkeypatch):
    """卡片要的统计与创建时间必须出现，否则有文件的库会显示「0 文件」。"""

    item = await _list_item(monkeypatch, _database())

    assert item["row_count"] == 5
    assert item["stats"]["row_count"] == 5
    assert item["created_at"].startswith("2026-10-01T12:00:00")
    assert item["supports_documents"] is True


@pytest.mark.parametrize("permission", [ResourcePermission.READ, ResourcePermission.MANAGE])
async def test_accessible_item_carries_share_config_regardless_of_permission(monkeypatch, permission):
    """共享范围不按权限裁剪；卡片据此显示真实范围，而不是缺数据拼出的「只读用户(0)」。

    按权限删字段只会让这条路径与详情接口对同一资源给出不同契约，买不到保密性：
    同一份 share_config 在 `GET /databases/{kb_id}` 对只读调用者也照样返回。
    """

    item = await _list_item(monkeypatch, _database(effective_permission=permission))

    assert item["can_manage"] is (permission == ResourcePermission.MANAGE)
    share_config = item.get("share_config") or {}
    assert share_config.get("version") == 2
