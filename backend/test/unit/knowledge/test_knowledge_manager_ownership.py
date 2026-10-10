import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.knowledge.manager import KnowledgeBaseManager
from yuxi.knowledge.read_models import KnowledgeBaseConfig

pytestmark = pytest.mark.asyncio


async def test_executor_construction_is_offloaded_from_event_loop(tmp_path, monkeypatch):
    manager = KnowledgeBaseManager(str(tmp_path))
    event_loop_thread = threading.get_ident()
    construction_threads: list[int] = []
    executor = object()

    def create_executor(_kb_type: str, _work_dir: str):
        construction_threads.append(threading.get_ident())
        return executor

    monkeypatch.setattr("yuxi.knowledge.manager.KnowledgeBaseFactory.create", create_executor)

    assert await manager._get_or_create_kb_instance("fake") is executor
    assert construction_threads and construction_threads[0] != event_loop_thread


async def test_concurrent_executor_construction_is_deduplicated(tmp_path, monkeypatch):
    manager = KnowledgeBaseManager(str(tmp_path))
    construction_count = 0
    executor = object()

    def create_executor(_kb_type: str, _work_dir: str):
        nonlocal construction_count
        construction_count += 1
        return executor

    monkeypatch.setattr("yuxi.knowledge.manager.KnowledgeBaseFactory.create", create_executor)

    results = await asyncio.gather(
        manager._get_or_create_kb_instance("fake"),
        manager._get_or_create_kb_instance("fake"),
    )

    assert results == [executor, executor]
    assert construction_count == 1


async def test_delete_database_cleans_resources_before_deleting_record(tmp_path, monkeypatch):
    """无独立凭据的旧个人库仍先清理资源再删除记录。"""
    calls = []
    manager = KnowledgeBaseManager(str(tmp_path))

    class FakeExecutor:
        async def cleanup_database_resources(self, kb_id: str) -> dict:
            calls.append(("cleanup", kb_id))
            return {"message": "删除成功"}

    class FakeRepository:
        async def get_by_kb_id(self, kb_id: str):
            return SimpleNamespace(kb_type="notion", scope="personal", additional_params={})

        async def delete(self, kb_id: str) -> None:
            calls.append(("delete_record", kb_id))

    async def get_executor(kb_type: str):
        assert kb_type == "notion"
        return FakeExecutor()

    monkeypatch.setattr(manager, "_get_or_create_kb_instance", get_executor)
    monkeypatch.setattr(
        "yuxi.repositories.knowledge_base_repository.KnowledgeBaseRepository",
        FakeRepository,
    )

    result = await manager.delete_database("kb_1")

    assert result == {"message": "删除成功"}
    assert calls == [("cleanup", "kb_1"), ("delete_record", "kb_1")]


@pytest.mark.parametrize("refresh_stats_fails", [False, True])
async def test_parse_file_refreshes_stats_and_keeps_original_error_after_executor_failure(
    tmp_path, monkeypatch, refresh_stats_fails
):
    manager = KnowledgeBaseManager(str(tmp_path))
    refreshed = []

    class FakeExecutor:
        async def parse_file(self, *args, **kwargs):
            raise ValueError("parse failed")

    async def get_kb_config(_kb_id: str):
        return KnowledgeBaseConfig(kb_id="kb_1", kb_type="fake")

    if refresh_stats_fails:

        async def refresh_database_stats(_kb_id: str):
            raise RuntimeError("stats failed")
    else:

        async def refresh_database_stats(kb_id: str):
            refreshed.append(kb_id)
            return {}

    monkeypatch.setattr(manager, "get_kb_config", get_kb_config)
    monkeypatch.setattr(manager, "_get_or_create_kb_instance", AsyncMock(return_value=FakeExecutor()))
    monkeypatch.setattr(manager, "_refresh_database_stats", refresh_database_stats)

    with pytest.raises(ValueError, match="parse failed"):
        await manager.parse_file("kb_1", "file_1")

    if not refresh_stats_fails:
        assert refreshed == ["kb_1"]


async def test_update_query_params_delegates_persistence_to_manager(tmp_path, monkeypatch):
    manager = KnowledgeBaseManager(str(tmp_path))
    calls = []

    class FakeExecutor:
        @staticmethod
        def validate_query_params(_kb_id: str, params: dict):
            return dict(params)

    class FakeRepository:
        async def merge_query_params_options(self, kb_id: str, params: dict):
            calls.append((kb_id, params))
            return object()

    monkeypatch.setattr(
        "yuxi.repositories.knowledge_base_repository.KnowledgeBaseRepository",
        FakeRepository,
    )
    monkeypatch.setattr(
        manager,
        "get_kb_config",
        AsyncMock(return_value=KnowledgeBaseConfig(kb_id="kb_1", kb_type="fake")),
    )
    monkeypatch.setattr(manager, "_get_or_create_kb_instance", AsyncMock(return_value=FakeExecutor()))

    await manager.update_kb_query_params("kb_1", {"top_k": 5})

    assert calls == [("kb_1", {"top_k": 5})]


async def test_consistency_check_delegates_type_resources_to_executor(tmp_path, monkeypatch):
    manager = KnowledgeBaseManager(str(tmp_path))
    calls = []

    class FakeExecutor:
        async def detect_data_inconsistencies(self, known_kb_ids: set[str], managed_kb_ids: set[str]):
            calls.append((known_kb_ids, managed_kb_ids))
            return {"missing_collections": [], "missing_files": []}

    class FakeRepository:
        async def get_all(self):
            return [
                SimpleNamespace(kb_id="kb_1", kb_type="milvus"),
                SimpleNamespace(kb_id="kb_2", kb_type="dify"),
            ]

    manager.kb_instances["milvus"] = FakeExecutor()
    monkeypatch.setattr(
        "yuxi.repositories.knowledge_base_repository.KnowledgeBaseRepository",
        FakeRepository,
    )

    result = await manager.detect_data_inconsistencies()

    assert calls == [({"kb_1", "kb_2"}, {"kb_1"})]
    assert result["total_missing_collections"] == 0
    assert result["total_missing_files"] == 0


@pytest.mark.parametrize("role", ["admin", "superadmin"])
async def test_personal_notion_create_requires_token_for_admin(tmp_path, monkeypatch, role):
    """管理员也不能创建运行时无法读取的无凭据个人库。"""
    manager = KnowledgeBaseManager(str(tmp_path))
    from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository

    executor = SimpleNamespace(
        normalize_additional_params=lambda params: params,
        requires_embedding_model=False,
        work_dir=str(tmp_path),
        get_default_query_params=lambda kb_id: {},
    )
    monkeypatch.setattr(KnowledgeBaseRepository, "get_by_kb_id", AsyncMock(return_value=None))
    monkeypatch.setattr(KnowledgeBaseRepository, "create", AsyncMock())
    monkeypatch.setattr(manager, "get_database_info", AsyncMock(return_value=object()))
    monkeypatch.setattr("yuxi.knowledge.manager.KnowledgeBaseFactory.is_type_supported", lambda kind: True)
    monkeypatch.setattr(manager, "database_name_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(manager, "_get_or_create_kb_instance", AsyncMock(return_value=executor))
    with pytest.raises(ValueError, match="个人 Notion 知识库必须配置独立"):
        await manager.create_database(
            "test", "", kb_type="notion", scope="personal", created_by="test-user", created_by_role=role
        )


@pytest.mark.parametrize("role", ["admin", "superadmin"])
async def test_personal_notion_update_cannot_remove_token_for_admin(tmp_path, monkeypatch, role):
    """管理员清空个人库凭据时在写入前拒绝。"""
    from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository

    manager = KnowledgeBaseManager(str(tmp_path))
    monkeypatch.setattr("yuxi.knowledge.manager.KnowledgeBaseFactory.is_type_supported", lambda kind: True)
    monkeypatch.setattr(
        "yuxi.knowledge.manager.KnowledgeBaseFactory.get_kb_class",
        lambda kind: SimpleNamespace(normalize_additional_params=lambda params: params),
    )
    record = SimpleNamespace(
        kb_type="notion",
        scope="personal",
        additional_params={"notion_token": "test-token", "notion_data_source_id": "test-source"},
    )
    monkeypatch.setattr(KnowledgeBaseRepository, "get_by_kb_id", AsyncMock(return_value=record))
    monkeypatch.setattr(KnowledgeBaseRepository, "update", AsyncMock())
    monkeypatch.setattr(manager, "get_database_info", AsyncMock(return_value=record))
    with pytest.raises(ValueError, match="个人 Notion 知识库必须配置独立"):
        await manager.update_database("test-kb", "test", "", additional_params={"notion_token": ""}, operator_role=role)
