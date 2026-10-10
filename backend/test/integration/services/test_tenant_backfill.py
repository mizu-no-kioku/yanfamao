"""默认租户建立、归属根回填，以及插入路径的租户填充。

隔离 schema 上跑，不依赖运行中的 API，因此不需要 TEST_USERNAME/TEST_PASSWORD，
只需要容器内的 POSTGRES_URL。三个 session 级 autouse fixture 由本文件覆盖，
照 test_schema_migration_version.py 的先例。
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.storage.postgres.manager import DEFAULT_TENANT_ID, TENANT_ROOT_TABLES, PostgresManager
from yuxi.storage.postgres.models_business import Department
from yuxi.storage.postgres.tenant_context import use_tenant

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件自行创建隔离 Schema，不依赖运行中的 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    yield


def _scoped_manager(engine) -> PostgresManager:
    """创建不触碰进程单例的隔离 manager。"""

    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager._initialized = True
    return manager


@pytest_asyncio.fixture()
async def isolated_tenant_schema():
    schema = f"tenant_backfill_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        yield _scoped_manager(scoped_engine)
    finally:
        await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def _tenant_id_nullability(engine) -> dict[str, str]:
    async with engine.begin() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT table_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND column_name = 'tenant_id'"
                )
            )
        ).all()
    return {table_name: is_nullable for table_name, is_nullable in rows}


async def test_orm_insert_fills_tenant_id_from_the_context(isolated_tenant_schema):
    """tenant_id 是 NOT NULL 且**没有数据库默认值**：ORM 不填就会让所有写入失败。

    这条守着一次真实故障：列被收紧为 NOT NULL 后，插入路径没有任何地方填值，
    建部门/建用户/建知识库全部报 not-null 违反。
    """
    manager = isolated_tenant_schema
    await manager.create_business_tables()
    await manager.create_knowledge_tables()

    factory = async_sessionmaker(manager.async_engine, expire_on_commit=False)
    async with factory() as session:
        department = Department(name="新部门")
        session.add(department)
        await session.commit()

    assert department.tenant_id == DEFAULT_TENANT_ID


async def test_orm_insert_follows_the_active_tenant(isolated_tenant_schema):
    """切换租户上下文后，插入落到那个租户——认证计划靠这条把宿主 Claim 接进来。"""
    manager = isolated_tenant_schema
    await manager.create_business_tables()
    await manager.create_knowledge_tables()

    factory = async_sessionmaker(manager.async_engine, expire_on_commit=False)
    with use_tenant(7):
        async with factory() as session:
            department = Department(name="租户 7 的部门")
            session.add(department)
            await session.commit()

        assert department.tenant_id == 7

    # 离开上下文后，插入重新落到默认租户。
    async with factory() as session:
        other = Department(name="默认租户的部门")
        session.add(other)
        await session.commit()

    assert other.tenant_id == DEFAULT_TENANT_ID


async def test_backfill_puts_existing_rows_into_default_tenant(isolated_tenant_schema):
    """模拟租户化之前的老库：列可空且存量行为 NULL，迁移后落到默认租户并被收紧为 NOT NULL。"""
    manager = isolated_tenant_schema
    await manager.create_business_tables()
    await manager.create_knowledge_tables()

    # ORM 现在会把它建成 NOT NULL；老库没有这一约束，所以先退回去再放 NULL 行。
    async with manager.async_engine.begin() as connection:
        for table in TENANT_ROOT_TABLES:
            await connection.execute(text(f"ALTER TABLE {table} ALTER COLUMN tenant_id DROP NOT NULL"))
        await connection.execute(text("INSERT INTO departments (name, tenant_id) VALUES ('默认部门', NULL)"))

    await manager.ensure_tenant_schema()

    async with manager.async_engine.begin() as connection:
        tenant_ids = (await connection.execute(text("SELECT DISTINCT tenant_id FROM departments"))).scalars().all()

    assert tenant_ids == [DEFAULT_TENANT_ID]

    nullability = await _tenant_id_nullability(manager.async_engine)
    for table in TENANT_ROOT_TABLES:
        assert nullability.get(table) == "NO", f"{table} 的 tenant_id 仍是可空"


async def test_raw_insert_without_tenant_context_lands_in_the_default_tenant(isolated_tenant_schema):
    """裸 SQL 插入必须仍能落库。

    列没有默认值时，任何不带 tenant_id 的 INSERT 都被 NOT NULL 挡住——迁移刚上线而
    进程还在跑旧代码时，这等于让全部写入失败（本条就是那次故障的回归）。
    """
    manager = isolated_tenant_schema
    await manager.create_business_tables()
    await manager.create_knowledge_tables()
    await manager.ensure_tenant_schema()

    async with manager.async_engine.begin() as connection:
        await connection.execute(text("INSERT INTO departments (name) VALUES ('裸插入部门')"))
        tenant_id = (await connection.execute(text("SELECT tenant_id FROM departments"))).scalar()

    assert tenant_id == DEFAULT_TENANT_ID


async def test_raw_insert_follows_the_session_tenant(isolated_tenant_schema):
    """会话设了 app.tenant_id 时裸插入落到那个租户——RLS 上线后靠这条保持一致。"""
    manager = isolated_tenant_schema
    await manager.create_business_tables()
    await manager.create_knowledge_tables()
    await manager.ensure_tenant_schema()

    async with manager.async_engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.tenant_id', '5', true)"))
        await connection.execute(text("INSERT INTO departments (name) VALUES ('租户 5 的部门')"))
        tenant_id = (await connection.execute(text("SELECT tenant_id FROM departments"))).scalar()

    assert tenant_id == 5


async def test_backfill_is_idempotent(isolated_tenant_schema):
    """迁移会被重放（容器重跑、测试跑两遍），再跑一次不得改变任何行。"""
    manager = isolated_tenant_schema
    await manager.create_business_tables()
    await manager.create_knowledge_tables()
    await manager.ensure_tenant_schema()

    async with manager.async_engine.begin() as connection:
        before = (await connection.execute(text("SELECT count(*) FROM tenants"))).scalar()

    await manager.ensure_tenant_schema()

    async with manager.async_engine.begin() as connection:
        after = (await connection.execute(text("SELECT count(*) FROM tenants"))).scalar()

    assert before == after == 1


async def test_default_tenant_sequence_is_advanced_past_the_seeded_row(isolated_tenant_schema):
    """手工插入 id=1 不推进 IDENTITY 序列；不校准就会让第一个真实租户撞主键。"""
    manager = isolated_tenant_schema
    await manager.create_business_tables()
    await manager.create_knowledge_tables()
    await manager.ensure_tenant_schema()

    async with manager.async_engine.begin() as connection:
        next_id = (
            await connection.execute(
                text(
                    "INSERT INTO tenants (external_tenant_id, issuer, name) "
                    "VALUES ('t-next', 'local', '下一个租户') RETURNING id"
                )
            )
        ).scalar()

    assert next_id > DEFAULT_TENANT_ID
