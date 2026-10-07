import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.routers import knowledge_router
from server.utils.knowledge_response import serialize_knowledge_base
from yuxi.knowledge.read_models import KnowledgeBaseSummary


def test_serialize_knowledge_base_redacts_credentials_from_compatibility_fields():
    database = KnowledgeBaseSummary(
        kb_id="kb-1",
        name="知识库",
        description=None,
        kb_type="dify",
        embedding_model_spec=None,
        llm_model_spec=None,
        query_params={},
        additional_params={"dify_token": "secret", "chunk_size": 100},
        share_config={"version": 2, "read_scope": None, "manage_scope": None},
        created_by=None,
        created_at=None,
    )

    response = serialize_knowledge_base(database, redact_secrets=True)

    assert response["additional_params"]["chunk_size"] == 100
    assert response["metadata"]["chunk_size"] == 100
    assert "dify_token" not in response["additional_params"]
    assert "dify_token" not in response["metadata"]


@pytest.mark.parametrize(("uid", "role", "can_read"), [("admin-1", "admin", True), ("other-user", "user", False)])
@pytest.mark.asyncio
async def test_non_manager_cannot_manage_global_read_knowledge_base(monkeypatch, uid, role, can_read):
    database = {
        "created_by": "owner",
        "share_config": {
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": None,
        },
    }

    async def fake_get_database_info(_kb_id):
        return database

    monkeypatch.setattr(knowledge_router.knowledge_base, "get_database_info", fake_get_database_info)
    user = SimpleNamespace(uid=uid, role=role, department_id=2)

    if can_read:
        assert await knowledge_router.require_knowledge_base_read("kb-1", user) is user

    with pytest.raises(HTTPException) as exc_info:
        await knowledge_router.require_knowledge_base_manage("kb-1", user)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_query_parameter_routes_apply_knowledge_base_acl(monkeypatch):
    database = {
        "created_by": "owner",
        "share_config": {
            "version": 2,
            "read_scope": {"access_level": "user", "user_uids": ["admin-1"]},
            "manage_scope": None,
        },
    }

    async def fake_get_database_info(_kb_id):
        return database

    monkeypatch.setattr(knowledge_router.knowledge_base, "get_database_info", fake_get_database_info)
    readonly_admin = SimpleNamespace(uid="admin-1", role="admin", department_id=2)

    assert await knowledge_router.require_knowledge_base_read("kb-1", readonly_admin) is readonly_admin

    with pytest.raises(HTTPException) as exc_info:
        await knowledge_router.require_knowledge_base_read(
            "kb-1", SimpleNamespace(uid="admin-2", role="admin", department_id=2)
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_plain_user_with_scope_match_passes_read_adapter(monkeypatch):
    """普通角色命中读取范围时可以通过适配器——这正是本特性要放开的能力。"""
    database = {
        "created_by": "owner",
        "scope": "shared",
        "share_config": {
            "version": 2,
            "read_scope": {"access_level": "user", "user_uids": ["u-1"]},
            "manage_scope": None,
        },
    }

    async def fake_get_database_info(_kb_id):
        return database

    monkeypatch.setattr(knowledge_router.knowledge_base, "get_database_info", fake_get_database_info)
    plain_user = SimpleNamespace(uid="u-1", role="user", department_id=2)

    assert await knowledge_router.require_knowledge_base_read("kb-1", plain_user) is plain_user


@pytest.mark.asyncio
async def test_plain_user_without_scope_match_is_still_denied(monkeypatch):
    """放开底座不能变成"人人可读"：范围不命中仍必须 403。"""
    database = {
        "created_by": "owner",
        "scope": "shared",
        "share_config": {
            "version": 2,
            "read_scope": {"access_level": "user", "user_uids": ["u-1"]},
            "manage_scope": None,
        },
    }

    async def fake_get_database_info(_kb_id):
        return database

    monkeypatch.setattr(knowledge_router.knowledge_base, "get_database_info", fake_get_database_info)
    other_user = SimpleNamespace(uid="u-9", role="user", department_id=2)

    with pytest.raises(HTTPException) as exc_info:
        await knowledge_router.require_knowledge_base_read("kb-1", other_user)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_plain_user_without_scope_match_cannot_manage(monkeypatch):
    database = {
        "created_by": "owner",
        "scope": "shared",
        "share_config": {
            "version": 2,
            "read_scope": {"access_level": "user", "user_uids": ["u-1"]},
            "manage_scope": None,
        },
    }

    async def fake_get_database_info(_kb_id):
        return database

    monkeypatch.setattr(knowledge_router.knowledge_base, "get_database_info", fake_get_database_info)
    sharee = SimpleNamespace(uid="u-1", role="user", department_id=2)

    with pytest.raises(HTTPException) as exc_info:
        await knowledge_router.require_knowledge_base_manage("kb-1", sharee)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_adapter_base_is_logged_in_user_not_admin():
    """底座是登录用户：普通角色不应在适配器的依赖层被管理员闸门 403 拦下。"""

    plain_user = SimpleNamespace(uid="u-1", role="user", department_id=2)

    for adapter in (
        knowledge_router.require_knowledge_base_read,
        knowledge_router.require_knowledge_base_manage,
    ):
        dependency = inspect.signature(adapter).parameters["current_user"].default
        assert await dependency.dependency(plain_user) is plain_user


@pytest.mark.parametrize(
    ("route_handler", "expected_dependency"),
    [
        # 静态元数据：普通用户建库与上传流程都要用，底座是登录用户。
        ("get_knowledge_base_types", "get_required_user"),
        ("get_knowledge_chunk_presets", "get_required_user"),
        ("get_supported_file_types", "get_required_user"),
        ("generate_description", "get_required_user"),
        # 跨知识库的全量/聚合视图：仍限管理员，普通用户走各自的可访问列表。
        ("get_databases", "get_admin_user"),
        ("get_mindmap_databases", "get_admin_user"),
        ("get_knowledge_base_statistics", "get_admin_user"),
    ],
)
def test_knowledge_route_dependency_class(route_handler, expected_dependency):
    """固定这几个知识库路由的底座归属，防止静态路由被重新收紧或全量视图被放开。"""

    dependency = inspect.signature(getattr(knowledge_router, route_handler)).parameters["current_user"].default
    assert dependency.dependency is getattr(knowledge_router, expected_dependency)


@pytest.mark.asyncio
async def test_generic_upload_channel_without_kb_id_stays_admin_only():
    """未带 kb_id 的上传/抓取通道不含知识库范围，仍限管理员；带 kb_id 时由按库 MANAGE 判定。"""

    plain_user = SimpleNamespace(uid="u-1", role="user", department_id=2)
    admin_user = SimpleNamespace(uid="a-1", role="admin", department_id=1)

    with pytest.raises(HTTPException) as exc_info:
        await knowledge_router._require_admin_when_no_kb_id(None, plain_user)
    assert exc_info.value.status_code == 403

    assert await knowledge_router._require_admin_when_no_kb_id("kb-1", plain_user) is None
    assert await knowledge_router._require_admin_when_no_kb_id(None, admin_user) is None
