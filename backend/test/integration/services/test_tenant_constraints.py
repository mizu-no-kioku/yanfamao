"""按租户重建的业务唯一性。

隔离 schema 上跑，不需要凭证，只需要容器内的 POSTGRES_URL。
三个 session 级 autouse fixture 由本文件覆盖，照 test_schema_migration_version.py 的先例。
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.storage.postgres.manager import PostgresManager
from yuxi.storage.postgres.models_business import APIKey, Department, User

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
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager._initialized = True
    return manager


@pytest_asyncio.fixture()
async def tenant_scoped_schema():
    """建好全部业务表并跑完租户迁移的隔离 schema，返回一个会话工厂。"""
    schema = f"tenant_constraints_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    manager = _scoped_manager(scoped_engine)
    try:
        await manager.create_business_tables()
        await manager.create_knowledge_tables()
        await manager.ensure_tenant_schema()
        yield async_sessionmaker(scoped_engine, expire_on_commit=False)
    finally:
        await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def test_department_name_is_unique_per_tenant(tenant_scoped_schema):
    """同名部门跨租户合法，同租户内重名必须被拒。"""
    factory = tenant_scoped_schema
    async with factory() as session:
        session.add_all([Department(name="财务部", tenant_id=1), Department(name="财务部", tenant_id=2)])
        await session.commit()

    async with factory() as session:
        session.add(Department(name="财务部", tenant_id=1))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_host_identity_is_unique_per_tenant(tenant_scoped_schema):
    """同一宿主主体在不同租户里是两条记录，同租户内同一主体不能重复。

    用户名与 uid 仍是全局唯一（按租户唯一会破坏"按用户名登录"，留给认证计划），
    所以这里刻意用不同的用户名/uid，让断言只落在宿主身份这一条上。
    """
    factory = tenant_scoped_schema
    async with factory() as session:
        session.add_all(
            [
                User(
                    uid="u-a",
                    username="张三-a",
                    password_hash="x",
                    tenant_id=1,
                    external_issuer="host",
                    external_subject="s-1",
                ),
                User(
                    uid="u-b",
                    username="张三-b",
                    password_hash="x",
                    tenant_id=2,
                    external_issuer="host",
                    external_subject="s-1",
                ),
            ]
        )
        await session.commit()

    async with factory() as session:
        session.add(
            User(
                uid="u-c",
                username="张三-c",
                password_hash="x",
                tenant_id=1,
                external_issuer="host",
                external_subject="s-1",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_api_key_request_id_is_unique_per_tenant(tenant_scoped_schema):
    """request_id 是调用方生成的幂等键：两个租户各自生成同一个值是正常的。"""
    factory = tenant_scoped_schema
    async with factory() as session:
        user_a = User(uid="key-u-a", username="key-user-a", password_hash="x", tenant_id=1)
        user_b = User(uid="key-u-b", username="key-user-b", password_hash="x", tenant_id=2)
        session.add_all([user_a, user_b])
        await session.flush()
        session.add_all(
            [
                APIKey(
                    key_hash="hash-a",
                    key_prefix="yxkey_a",
                    name="k1",
                    user_id=user_a.id,
                    request_id="same-request",
                    created_by="1",
                    tenant_id=1,
                ),
                APIKey(
                    key_hash="hash-b",
                    key_prefix="yxkey_b",
                    name="k2",
                    user_id=user_b.id,
                    request_id="same-request",
                    created_by="1",
                    tenant_id=2,
                ),
            ]
        )
        await session.commit()

    async with factory() as session:
        existing = (await session.execute(select(User).where(User.uid == "key-u-a"))).scalar_one()
        session.add(
            APIKey(
                key_hash="hash-c",
                key_prefix="yxkey_c",
                name="k3",
                user_id=existing.id,
                request_id="same-request",
                created_by="1",
                tenant_id=1,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_legacy_global_department_name_index_is_dropped(tenant_scoped_schema):
    """旧的全局唯一索引必须被删掉，否则跨租户同名部门仍会被拒。"""
    factory = tenant_scoped_schema
    async with factory() as session:
        indexes = set(
            (
                await session.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = current_schema() AND tablename = 'departments'"
                    )
                )
            ).scalars()
        )

    assert "ix_departments_name" not in indexes
    assert "uq_departments_tenant_name" in indexes


async def test_legacy_global_api_key_request_index_is_dropped(tenant_scoped_schema):
    factory = tenant_scoped_schema
    async with factory() as session:
        indexes = set(
            (
                await session.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = current_schema() AND tablename = 'api_keys'"
                    )
                )
            ).scalars()
        )

    assert "ix_api_keys_request_id" not in indexes
    assert "uq_api_keys_tenant_request_id" in indexes
