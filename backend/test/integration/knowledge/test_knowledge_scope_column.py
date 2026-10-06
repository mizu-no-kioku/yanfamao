from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, text

from yuxi.knowledge.runtime import knowledge_base
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import KnowledgeBase


@pytest_asyncio.fixture
async def pg_pool():
    pg_manager._initialized = False
    pg_manager.async_engine = None
    pg_manager.AsyncSession = None
    pg_manager.initialize()
    await pg_manager.ensure_knowledge_schema()
    yield
    await pg_manager.async_engine.dispose()
    pg_manager._initialized = False
    pg_manager.async_engine = None
    pg_manager.AsyncSession = None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_existing_rows_default_to_shared_scope(pg_pool):
    """迁移后既有行必须落在 shared：它们今天的语义就是共享，不能被改变。"""
    suffix = uuid4().hex
    kb_id = f"pytest_scope_{suffix}"

    try:
        async with pg_manager.get_async_session_context() as session:
            # 显式不写 scope，模拟迁移前插入的行。
            await session.execute(
                text(
                    "INSERT INTO knowledge_bases (kb_id, name, kb_type) "
                    "VALUES (:kb_id, :name, 'milvus')"
                ),
                {"kb_id": kb_id, "name": "scope 迁移"},
            )

        detail = await knowledge_base.get_database_info(kb_id)

        assert detail is not None
        assert detail.scope == "shared"
    finally:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_database_defaults_to_shared_scope(pg_pool):
    suffix = uuid4().hex
    kb_id = None

    try:
        detail = await knowledge_base.create_database(
            database_name=f"scope 默认 {suffix}",
            description="scope 默认值",
            kb_type="milvus",
            # milvus 类型要求嵌入模型；这里只验证 scope 默认值，故取集成测试通用的 spec。
            embedding_model_spec="siliconflow-cn:Pro/BAAI/bge-m3",
        )
        kb_id = detail.kb_id

        assert detail.scope == "shared"
    finally:
        if kb_id:
            async with pg_manager.get_async_session_context() as session:
                await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))
