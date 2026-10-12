"""迁移验收目录的夹具：不需要运行中的 API，也不需要任何凭证。

三个 session 级 autouse 夹具按 `test_tenant_backfill.py` 的先例覆盖掉。它们在缺凭证时本来就会
直接返回，但显式覆盖能保证本目录**永远**不去 ping 真实 API——这里的验收对象是迁移子进程与
独立测试实例，与运行中的服务无关。
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本目录不碰真实 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    yield
