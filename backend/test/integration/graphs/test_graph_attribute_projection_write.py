from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from yuxi.knowledge.graphs.extractors.base import normalize_extraction_result
from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from yuxi.storage.neo4j import get_shared_neo4j_connection, safe_neo4j_label
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeFile,
    KnowledgeGraphEntity,
)


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
async def test_write_chunk_graph_records_can_be_written_to_postgres_mirror(pg_pool):
    """真实链路：`write_chunk_graph` 产出的实体记录必须能写进 PostgreSQL 镜像。

    为了 Neo4j 侧的属性投影，实体记录多了 `attribute_names` / `attribute_values` 两个键；
    它们不是 `knowledge_graph_entities` 的列，落库前必须被过滤，否则整条写入语句编译失败。
    """
    suffix = uuid4().hex
    kb_id = f"pytest_write_mirror_{suffix}"
    file_id = f"file_{suffix}"
    chunk_id = f"chunk_{suffix}"
    label = safe_neo4j_label(kb_id)
    connection = get_shared_neo4j_connection()
    repo = KnowledgeGraphRepository()
    service = MilvusGraphService(neo4j_connection=connection)

    try:
        async with pg_manager.get_async_session_context() as session:
            session.add(KnowledgeBase(kb_id=kb_id, name="write mirror", kb_type="milvus"))
            await session.flush()
            session.add(KnowledgeFile(file_id=file_id, kb_id=kb_id, filename="test.md"))
            await session.flush()
            session.add(
                KnowledgeChunk(
                    chunk_id=chunk_id,
                    file_id=file_id,
                    kb_id=kb_id,
                    chunk_index=0,
                    content="张三任职于公司",
                )
            )

        chunk = SimpleNamespace(
            chunk_id=chunk_id,
            file_id=file_id,
            kb_id=kb_id,
            chunk_index=0,
            content="张三任职于公司",
            start_char_pos=0,
            end_char_pos=8,
        )
        extraction_result = normalize_extraction_result(
            {
                "relations": [
                    {
                        "source": {
                            "text": "张三",
                            "label": "Person",
                            "attributes": [{"text": "工程师", "label": "Occupation"}],
                        },
                        "target": {"text": "公司", "label": "Organization"},
                        "text": "任职于",
                        "label": "WORKS_AT",
                    }
                ],
            },
            "llm",
        )

        entities, triples = service.write_chunk_graph(kb_id, chunk, extraction_result)
        await repo.upsert_chunk_graph(
            kb_id=kb_id,
            file_id=file_id,
            chunk_id=chunk_id,
            entities=entities,
            triples=triples,
        )

        async with pg_manager.get_async_session_context() as session:
            rows = list(
                await session.scalars(
                    select(KnowledgeGraphEntity).where(KnowledgeGraphEntity.kb_id == kb_id)
                )
            )

        assert sorted(row.normalized_name for row in rows) == ["公司", "张三"]
        person = next(row for row in rows if row.normalized_name == "张三")
        assert person.attributes == [{"text": "工程师", "label": "Occupation"}]
    finally:
        with connection.driver.session() as session:
            session.run(f"MATCH (n:MilvusKB:`{label}`) DETACH DELETE n").consume()
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(KnowledgeGraphEntity).where(KnowledgeGraphEntity.kb_id == kb_id))
            await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.kb_id == kb_id))
            await session.execute(delete(KnowledgeFile).where(KnowledgeFile.kb_id == kb_id))
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))
