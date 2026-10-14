"""退休工具名清理：迁移前、迁移后、重复执行三种状态都要有断言。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.storage.postgres.models_business import Agent, Base
from yuxi.storage_migrations import v073_retire_business_database_tools as migration

LEGACY = ["business_database_schema", "business_database_query"]
CURRENT = ["list_business_database_tables", "describe_business_database_table", "query_business_database"]


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _agent(slug: str, tools: list[str] | str, *, extra_context: dict | None = None) -> Agent:
    context = {"tools": tools, "system_prompt": "回答业务问题"}
    context.update(extra_context or {})
    return Agent(
        slug=slug,
        backend_id="ChatbotAgent",
        name=slug,
        description=None,
        pics=[],
        config_json={"context": context},
        share_config={"version": 2, "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []}},
        is_default=False,
        is_subagent=False,
    )


async def _tools_of(session, slug: str):
    result = await session.execute(select(Agent).where(Agent.slug == slug))
    return result.scalar_one().config_json["context"]["tools"]


@pytest.mark.asyncio
async def test_migration_removes_only_the_two_retired_tools(db_session):
    """迁移前：五工具混合清单 → 迁移后只剩三工具，其余 context 键不动。"""
    db_session.add(_agent("business-database", [*LEGACY, *CURRENT], extra_context={"mcps": ["x"]}))
    await db_session.flush()

    removed = await migration.retire_business_database_tools(db_session)

    assert removed == 2
    assert await _tools_of(db_session, "business-database") == CURRENT
    result = await db_session.execute(select(Agent).where(Agent.slug == "business-database"))
    context = result.scalar_one().config_json["context"]
    assert context["system_prompt"] == "回答业务问题", "无关配置键必须原样保留"
    assert context["mcps"] == ["x"], "无关配置键必须原样保留"


@pytest.mark.asyncio
async def test_repeat_execution_is_a_no_op(db_session):
    """重复执行：第二次摘掉 0 条，且配置与第一次结果逐字相同。"""
    db_session.add(_agent("business-database", [*LEGACY, *CURRENT]))
    await db_session.flush()

    first = await migration.retire_business_database_tools(db_session)
    after_first = await _tools_of(db_session, "business-database")
    second = await migration.retire_business_database_tools(db_session)
    after_second = await _tools_of(db_session, "business-database")
    third = await migration.retire_business_database_tools(db_session)

    assert first == 2
    assert second == 0
    assert third == 0
    assert after_first == after_second == CURRENT


@pytest.mark.asyncio
async def test_similar_names_are_preserved(db_session):
    """只删这两个明确退休的名字；名字相近的不能被误伤。"""
    lookalikes = ["business_database_schema_v2", "business_database_query_legacy", "business_database_schema "]
    db_session.add(_agent("lookalike-holder", [*LEGACY, *lookalikes, *CURRENT]))
    await db_session.flush()

    removed = await migration.retire_business_database_tools(db_session)

    assert removed == 2
    remaining = await _tools_of(db_session, "lookalike-holder")
    assert remaining == [*lookalikes, *CURRENT]


@pytest.mark.asyncio
async def test_agents_without_retired_tools_are_untouched(db_session):
    """没有退休工具名的智能体不得被改动。"""
    db_session.add(_agent("default-chatbot", CURRENT))
    db_session.add(_agent("no-tools-key", CURRENT))
    await db_session.flush()

    removed = await migration.retire_business_database_tools(db_session)

    assert removed == 0
    assert await _tools_of(db_session, "default-chatbot") == CURRENT


def test_odd_context_shapes_are_left_alone():
    """tools 为 "all"、缺失、或不是列表时原样返回，不做推断式修补。"""
    for context in ({"tools": "all"}, {}, {"tools": None}, {"tools": {"a": 1}}):
        stripped, removed = migration.strip_retired_tools(context)
        assert stripped is context
        assert removed == 0

    stripped, removed = migration.strip_retired_tools(None)
    assert stripped is None
    assert removed == 0
