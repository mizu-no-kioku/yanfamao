"""运行时角色的启动护栏。

superuser / BYPASSRLS / 表 owner 三者任一成立，RLS 都是静默失效的——不报错、不告警，
只是边界没了。所以这条必须在启动上显式失败，不能只靠事后测试发现。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from yuxi.storage.postgres.manager import PostgresManager

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


async def test_guard_rejects_a_superuser_runtime_role():
    """以 owner 连接必须被拒——这正是「未配置 POSTGRES_APP_URL」时的回退结果。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    try:
        with pytest.raises(RuntimeError, match="不足以承载租户隔离"):
            await _manager_with_runtime_engine(engine).require_restricted_runtime_role()
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
