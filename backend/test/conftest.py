from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _close_leaked_pg_pool_before_loop_close() -> None:
    """事件循环关闭前，关闭可能被测试泄漏的全局 PostgreSQL 连接池。

    `pg_manager` 是进程级单例。有测试会让它连上真实 PostgreSQL（从而创建 psycopg
    连接池）却不关闭；pytest-asyncio 随后关闭事件循环，`_cancel_all_tasks` 会取消并
    **等待**池的 worker 协程，而该协程若正阻塞在建连上就永远等不到——整套测试因此挂起。
    实测复现率约 50%：表现为某个用例的 teardown 超时，其余用例全部通过。

    这里在事件循环真正关闭之前把池关掉，使取消阶段没有等待对象。**不掩盖任何测试结果**：
    只关闭连接池，用例数、断言与 pass/fail 判定都未改变。
    """
    import asyncio.runners

    original = asyncio.runners.Runner.close
    if getattr(original, "_yuxi_pool_guard", False):
        return

    def guarded(self) -> None:
        from yuxi.storage.postgres.manager import pg_manager

        # 有测试会把 pg_manager 替换成桩对象，因此逐项防御式读取；
        # 缺少 close 或没有任何资源时直接跳过，不改变测试结果。
        close = getattr(pg_manager, "close", None)
        pool = getattr(pg_manager, "langgraph_pool", None)
        engine = getattr(pg_manager, "async_engine", None)
        if callable(close) and (pool is not None or engine is not None):
            try:
                self._loop.run_until_complete(close())
            except Exception as exc:  # 关闭失败不应改变测试结果
                from yuxi.utils.logging_config import logger

                logger.warning("关闭测试泄漏的 PostgreSQL 连接池失败：{}: {}", type(exc).__name__, exc)
        return original(self)

    guarded._yuxi_pool_guard = True  # type: ignore[attr-defined]
    asyncio.runners.Runner.close = guarded  # type: ignore[method-assign]


def pytest_configure(config: pytest.Config) -> None:
    """Register shared markers without binding every test to a live environment."""
    config.addinivalue_line("markers", "unit: marks tests that run without live services")
    config.addinivalue_line("markers", "auth: marks tests that require authentication")
    config.addinivalue_line("markers", "integration: marks tests that hit the live API service")
    config.addinivalue_line("markers", "e2e: marks tests that exercise an end-to-end workflow")
    config.addinivalue_line("markers", "slow: marks tests as slow")
    _close_leaked_pg_pool_before_loop_close()


pytest_plugins = ["pytest_asyncio"]
