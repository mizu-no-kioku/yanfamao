from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from yuxi.storage.neo4j import get_shared_neo4j_connection, safe_neo4j_label
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeGraphEntity


@pytest_asyncio.fixture
async def pg_pool():
    # integration 的 schema fixture 用的是自己的 anyio loop，这里在当前 loop 里重建连接池。
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
async def test_list_entities_with_attributes_skips_empty_and_pages_by_id(pg_pool):
    suffix = uuid4().hex
    kb_id = f"pytest_backfill_{suffix}"
    repo = KnowledgeGraphRepository()

    try:
        async with pg_manager.get_async_session_context() as session:
            session.add(KnowledgeBase(kb_id=kb_id, name="attribute backfill", kb_type="milvus"))
            await session.flush()
            session.add(
                KnowledgeGraphEntity(
                    entity_id=f"e1_{suffix}",
                    kb_id=kb_id,
                    normalized_name="上海项目",
                    label="项目",
                    name="上海项目",
                    attributes=[{"text": "上海", "label": "城市"}],
                )
            )
            await session.flush()
            session.add(
                KnowledgeGraphEntity(
                    entity_id=f"e2_{suffix}",
                    kb_id=kb_id,
                    normalized_name="北京项目",
                    label="项目",
                    name="北京项目",
                    attributes=[{"text": "北京", "label": "城市"}],
                )
            )
            await session.flush()
            session.add(
                KnowledgeGraphEntity(
                    entity_id=f"e3_{suffix}",
                    kb_id=kb_id,
                    normalized_name="无属性实体",
                    label="项目",
                    name="无属性实体",
                    attributes=[],
                )
            )

        rows = await repo.list_entities_with_attributes_by_kb_id(kb_id, limit=10)
        assert [row.entity_id for row in rows] == [f"e1_{suffix}", f"e2_{suffix}"]

        paged = await repo.list_entities_with_attributes_by_kb_id(kb_id, limit=10, after_id=rows[0].id)
        assert [row.entity_id for row in paged] == [f"e2_{suffix}"]

        assert await repo.list_entities_with_attributes_by_kb_id(kb_id, limit=10, after_id=rows[-1].id) == []
    finally:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(KnowledgeGraphEntity).where(KnowledgeGraphEntity.kb_id == kb_id))
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_entities_with_attributes_does_not_stop_on_attribute_less_page(pg_pool):
    """属性为空的实体不能占满一页就把分页顶死。

    `attributes=[]` 在 PostgreSQL 里存成 `'[]'` 而不是 NULL；若「无属性」只在取回之后用 Python
    过滤，一页全是无属性实体时这一页会变成空列表，调用方据此判定「取完」并停止，后续真正带属性
    的实体永远取不到，回填也就永远做不完。
    """
    suffix = uuid4().hex
    kb_id = f"pytest_backfill_page_{suffix}"
    repo = KnowledgeGraphRepository()

    try:
        async with pg_manager.get_async_session_context() as session:
            session.add(KnowledgeBase(kb_id=kb_id, name="backfill page", kb_type="milvus"))
            await session.flush()
            for index in range(3):
                session.add(
                    KnowledgeGraphEntity(
                        entity_id=f"empty_{index}_{suffix}",
                        kb_id=kb_id,
                        normalized_name=f"无属性实体{index}",
                        label="项目",
                        name=f"无属性实体{index}",
                        attributes=[],
                    )
                )
            await session.flush()
            session.add(
                KnowledgeGraphEntity(
                    entity_id=f"real_{suffix}",
                    kb_id=kb_id,
                    normalized_name="有属性实体",
                    label="项目",
                    name="有属性实体",
                    attributes=[{"text": "上海", "label": "城市"}],
                )
            )

        # 页大小 2 小于前面的无属性实体数量，无属性实体足以占满第一页。
        rows = await repo.list_entities_with_attributes_by_kb_id(kb_id, limit=2)

        assert [row.entity_id for row in rows] == [f"real_{suffix}"]
    finally:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(KnowledgeGraphEntity).where(KnowledgeGraphEntity.kb_id == kb_id))
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_attribute_projection_is_idempotent(pg_pool):
    suffix = uuid4().hex
    kb_id = f"pytest_backfill_write_{suffix}"
    entity_id = f"e_{suffix}"
    label = safe_neo4j_label(kb_id)
    connection = get_shared_neo4j_connection()
    service = MilvusGraphService(neo4j_connection=connection)

    try:
        async with pg_manager.get_async_session_context() as session:
            session.add(KnowledgeBase(kb_id=kb_id, name="attribute backfill write", kb_type="milvus"))
            await session.flush()
            session.add(
                KnowledgeGraphEntity(
                    entity_id=entity_id,
                    kb_id=kb_id,
                    normalized_name="上海项目",
                    label="项目",
                    name="上海项目",
                    attributes=[{"text": "上海", "label": "城市"}],
                )
            )

        # Neo4j 侧只写 attributes JSON 字符串，模拟回填前的老数据。
        with connection.driver.session() as session:
            session.run(
                f"""
                CREATE (e:Entity:MilvusKB:`{label}` {{
                    kb_id: $kb_id, entity_id: $entity_id, name: '上海项目', label: '项目',
                    attributes: $attributes
                }})
                """,
                kb_id=kb_id,
                entity_id=entity_id,
                attributes='[{"text": "上海", "label": "城市"}]',
            ).consume()

        assert await service.attributes_ready(kb_id) is False

        first = await service.backfill_attribute_projection(kb_id)
        second = await service.backfill_attribute_projection(kb_id)

        assert first == second == {"kb_id": kb_id, "scanned": 1, "updated": 1}
        assert await service.attributes_ready(kb_id) is True

        with connection.driver.session() as session:
            projection = session.run(
                f"MATCH (e:Entity:MilvusKB:`{label}` {{entity_id: $entity_id}}) "
                "RETURN e.attribute_names AS names, e.attribute_values AS values",
                entity_id=entity_id,
            ).single()
            entity_count = session.run(
                f"MATCH (n:Entity:MilvusKB:`{label}`) RETURN count(n) AS total"
            ).single()

        assert projection["names"] == ["城市"]
        assert projection["values"] == ["上海"]
        # 回填只更新已有节点，不创建新节点。
        assert entity_count["total"] == 1
    finally:
        with connection.driver.session() as session:
            session.run(f"MATCH (n:MilvusKB:`{label}`) DETACH DELETE n").consume()
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(KnowledgeGraphEntity).where(KnowledgeGraphEntity.kb_id == kb_id))
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))
