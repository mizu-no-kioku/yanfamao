"""租户相关集成测试的连接来源与防误连检查。

这组用例会执行**实例级全局 DDL**——`CREATE ROLE yuxi_app`、`ALTER ROLE yuxi_app`、
`GRANT ... ON SCHEMA public`（见 `manager.py` 的角色与授权语句）。跑在共享开发实例上时，
**即使把表建在隔离 schema 里，角色与公共 schema 的授权照样会被改**——隔离 schema 只隔离数据，
隔离不了实例级对象。

因此它们只允许连 `POSTGRES_TEST_URL` 指向的独立实例：

- 只读 `POSTGRES_TEST_URL`，**不回退**到 `POSTGRES_URL`；
- 库名必须是专用测试库（默认 `yuxi_test`）；
- 与 `POSTGRES_URL` 的 (host, port, database) 相同则直接拒绝；
- 连上之后核对 `current_database()` 就是那个专用库。

缺配置或核对失败**一律直接报错**，不 skip——skip 会让"忘了配"看起来像"跑过了"。
报错信息只含 host/port/database，**不含密码**。

起测试实例：`docker compose --profile test-db up -d postgres-test`
"""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

_MISSING_CONFIG = (
    "租户集成测试需要 POSTGRES_TEST_URL 指向独立测试实例（不会回退到 POSTGRES_URL）。\n"
    "起法：docker compose --profile test-db up -d postgres-test"
)

_verified = False


def tenant_schema_url() -> str:
    """独立测试实例的连接串；未配置直接报错，不静默回退。"""

    raw = os.environ.get("POSTGRES_TEST_URL", "").strip()
    if not raw:
        raise RuntimeError(_MISSING_CONFIG)
    return raw


def expected_test_database_name() -> str:
    return os.environ.get("POSTGRES_TEST_DB", "yuxi_test").strip() or "yuxi_test"


def _target(raw: str) -> tuple[str | None, int | None, str | None]:
    url = make_url(raw)
    return url.host, url.port, url.database


def _describe(raw: str) -> str:
    """只暴露 host/port/database——这个字符串会进报错信息，不能带密码。"""

    host, port, database = _target(raw)
    return f"{host}:{port}/{database}"


async def assert_target_is_the_test_instance() -> None:
    """核对连接目标确实是独立测试实例。每个进程只做一次。"""

    global _verified
    if _verified:
        return

    raw = tenant_schema_url()
    expected = expected_test_database_name()

    development = os.environ.get("POSTGRES_URL", "").strip()
    if development and _target(development) == _target(raw):
        raise RuntimeError(
            f"POSTGRES_TEST_URL 与 POSTGRES_URL 指向同一个目标（{_describe(raw)}）。"
            "这些用例会执行实例级角色 DDL，拒绝在开发实例上运行。"
        )

    engine = create_async_engine(raw, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            actual = (await connection.execute(text("SELECT current_database()"))).scalar()
    finally:
        await engine.dispose()

    if actual != expected:
        raise RuntimeError(
            f"POSTGRES_TEST_URL 连到的库是 {actual}，期望专用测试库 {expected}（目标 {_describe(raw)}）"
        )
    _verified = True


async def create_test_admin_engine():
    """建管理连接引擎，并先完成防误连核对。

    夹具只要用它替换裸的 `create_async_engine(os.environ["POSTGRES_URL"], ...)`，
    核对就自动生效——不需要每个夹具各自记得调一次守卫。
    """

    await assert_target_is_the_test_instance()
    return create_async_engine(tenant_schema_url(), pool_pre_ping=True)
