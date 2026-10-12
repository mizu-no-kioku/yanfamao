from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _close_leaked_pg_pool_before_loop_close() -> None:
    """**临时 workaround**：事件循环关闭前关闭被测试泄漏的全局 PostgreSQL 连接池。

    **这不是根因修复。** 泄漏该池的具体测试尚未定位——单元测试中查不到构造 psycopg
    池的地方，池构造探针也从未触发。本函数只消除泄漏造成的后果。

    **为什么必须替换全局 `asyncio.runners.Runner.close`，而不是范围更小的 fixture：**

    1. 泄漏的任务只能**在创建它们的那一个事件循环上**关闭。pytest-asyncio 由自身的
       `_scoped_runner` fixture 创建并关闭循环，用户 fixture 的 teardown 顺序无法保证
       早于它，等到用户 fixture 拿到循环时可能已经太晚。
    2. 本套件同时含同步与异步用例。异步 autouse fixture 无法覆盖同步用例，而同步用例
       同样会在**前一个异步用例泄漏的循环**关闭时被卡住。
    3. `Runner.close` 是循环关闭的唯一必经点，在这里收口才能覆盖全部用例形态。

    因此这里选择全局收口，并明确标注为临时措施；定位到泄漏源头后应当移除。

    **关闭失败不吞掉**：异常直接抛出使测试显式失败。guard 一旦失效必须立刻可见，
    静默降级成警告会让"挂起已解决"变成一句无证据的结论。
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
            # 不 try/except：关闭失败即让本用例失败，不允许静默降级。
            self._loop.run_until_complete(close())
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
