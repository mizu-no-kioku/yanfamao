"""运行时角色的启动护栏。

superuser / BYPASSRLS / 表 owner 三者任一成立，RLS 都是静默失效的——不报错、不告警，
只是边界没了。所以这条必须在启动上显式失败，不能只靠事后测试发现。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from yuxi.storage.postgres.manager import PostgresManager

from _tenant_schema_test_db import tenant_schema_url

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件只读角色属性，不依赖运行中的 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    yield


def _manager_with_runtime_engine(engine) -> PostgresManager:
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.runtime_engine = engine
    manager._initialized = True
    return manager


def _manager_with_discovery_engine(engine) -> PostgresManager:
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.discovery_engine = engine
    manager._initialized = True
    return manager


async def test_guard_rejects_a_superuser_runtime_role():
    """以 owner 连接必须被拒——这正是「未配置 POSTGRES_APP_URL」时的回退结果。

    同时钉住「查全部基表的 owner」这条：只查 users 的话，运行时不巧拥有的另一张表会静默
    失去 RLS 保护。postgres 拥有 public 下的全部基表，所以这里必须同时报出所有权。
    """
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    try:
        with pytest.raises(RuntimeError, match="不足以承载租户隔离") as excinfo:
            await _manager_with_runtime_engine(engine).require_restricted_runtime_role()
        message = str(excinfo.value)
        assert "superuser" in message
        assert "拥有基表" in message
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not os.getenv("POSTGRES_APP_URL"),
    reason="需要 POSTGRES_APP_URL 与受限角色；api/worker 容器重建并注入该变量后才会执行。",
)
async def test_guard_accepts_the_configured_runtime_role():
    """yuxi_app 必须通过：非 superuser、无 BYPASSRLS、非受保护表 owner。"""
    engine = create_async_engine(os.environ["POSTGRES_APP_URL"], pool_pre_ping=True)
    try:
        await _manager_with_runtime_engine(engine).require_restricted_runtime_role()
    finally:
        await engine.dispose()


async def test_discovery_guard_rejects_a_missing_credential():
    """未配置 `POSTGRES_DISCOVERY_URL` 时发现能力必须 fail-closed。

    这条钉住「**不**回落到运行时 / owner 连接」：`discovery_engine is None` 时直接抛错，
    而不是拿一个高权限连接顶上——回落会把"发现只属于 worker 的专用角色"退化成
    "任何进程都能枚举租户"，正是方案 (b) 要消除的。
    """
    manager = _manager_with_discovery_engine(None)
    with pytest.raises(RuntimeError, match="POSTGRES_DISCOVERY_URL"):
        await manager.require_discovery_credential()


async def test_discovery_guard_rejects_a_privileged_discovery_role():
    """发现连接落在 superuser（= 未配置时回落 owner 的结果）必须被拒。

    角色名不符、superuser、拥有基表三条都会报出——`yuxi_worker_discovery` 之外的任何角色都不该
    被当成发现凭据，即便它只是"忘了配"而落到了 owner。postgres 拥有 public 下的全部基表，
    所以"拥有基表"也必须同时报出。
    """
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    try:
        with pytest.raises(RuntimeError, match="不足以承载跨租户发现") as excinfo:
            await _manager_with_discovery_engine(engine).require_discovery_credential()
        message = str(excinfo.value)
        assert "superuser" in message
        assert "期望 yuxi_worker_discovery" in message
        assert "拥有基表" in message
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not os.getenv("POSTGRES_DISCOVERY_URL"),
    reason="需要 POSTGRES_DISCOVERY_URL 与发现角色；worker 容器注入该变量后才会执行。",
)
async def test_discovery_guard_accepts_the_configured_discovery_role():
    """配置了 `yuxi_worker_discovery` 连接、且与运行时同库时必须通过。

    同时把运行时连接也设上——这样"连接目标一致"这条检查**正向**被覆盖，而不是被跳过。
    """
    engine = create_async_engine(os.environ["POSTGRES_DISCOVERY_URL"], pool_pre_ping=True)
    runtime_engine = create_async_engine(tenant_schema_url(), pool_pre_ping=True)
    try:
        manager = _manager_with_discovery_engine(engine)
        manager.runtime_engine = runtime_engine
        await manager.require_discovery_credential()
    finally:
        await engine.dispose()
        await runtime_engine.dispose()


@pytest.mark.skipif(
    not os.getenv("POSTGRES_DISCOVERY_URL"),
    reason="需要 POSTGRES_DISCOVERY_URL 与发现角色；worker 容器注入该变量后才会执行。",
)
async def test_discovery_guard_rejects_extra_table_privileges():
    """发现角色多拿了**一张业务表**的读权限 → 必须被拒。

    只核对角色名 / superuser 会让"配置漂移"（有人手工 `GRANT` 过、或经角色继承拿到）静默通过。
    这条钉住"有效权限超出最小集"会被启动守卫抓住。
    """
    owner_engine = create_async_engine(tenant_schema_url(), pool_pre_ping=True)
    engine = create_async_engine(os.environ["POSTGRES_DISCOVERY_URL"], pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(text("GRANT SELECT ON knowledge_bases TO yuxi_worker_discovery"))
        try:
            with pytest.raises(RuntimeError, match="持有表级权限"):
                await _manager_with_discovery_engine(engine).require_discovery_credential()
        finally:
            async with owner_engine.begin() as connection:
                await connection.execute(text("REVOKE SELECT ON knowledge_bases FROM yuxi_worker_discovery"))
    finally:
        await owner_engine.dispose()
        await engine.dispose()


@pytest.mark.skipif(
    not os.getenv("POSTGRES_DISCOVERY_URL"),
    reason="需要 POSTGRES_DISCOVERY_URL 与发现角色；worker 容器注入该变量后才会执行。",
)
async def test_discovery_guard_rejects_extra_column_privileges():
    """发现角色多拿了 `tenants` 上**另一列**的读权限 → 必须被拒。"""
    owner_engine = create_async_engine(tenant_schema_url(), pool_pre_ping=True)
    engine = create_async_engine(os.environ["POSTGRES_DISCOVERY_URL"], pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(text("GRANT SELECT (name) ON tenants TO yuxi_worker_discovery"))
        try:
            with pytest.raises(RuntimeError, match="持有列级权限") as excinfo:
                await _manager_with_discovery_engine(engine).require_discovery_credential()
            assert "tenants.name:SELECT" in str(excinfo.value)
        finally:
            async with owner_engine.begin() as connection:
                await connection.execute(text("REVOKE SELECT (name) ON tenants FROM yuxi_worker_discovery"))
    finally:
        await owner_engine.dispose()
        await engine.dispose()


@pytest.mark.skipif(
    not os.getenv("POSTGRES_DISCOVERY_URL"),
    reason="需要 POSTGRES_DISCOVERY_URL 与发现角色；worker 容器注入该变量后才会执行。",
)
async def test_discovery_guard_rejects_a_target_mismatch():
    """发现连接与运行时连接不是同一个库 → 必须被拒。

    `runtime_engine` 只被读 `.url`、不会被连接，所以这里用一个**不存在**的目标就够——
    它证明的是"目标一致性"这条检查本身，不是连通性。
    """
    engine = create_async_engine(os.environ["POSTGRES_DISCOVERY_URL"], pool_pre_ping=True)
    runtime_engine = create_async_engine("postgresql+asyncpg://someone:secret@elsewhere:5432/otherdb")
    try:
        manager = _manager_with_discovery_engine(engine)
        manager.runtime_engine = runtime_engine
        with pytest.raises(RuntimeError, match="发现连接目标与运行时不一致"):
            await manager.require_discovery_credential()
    finally:
        await engine.dispose()
        await runtime_engine.dispose()
