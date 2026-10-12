"""迁移验收目录的夹具：不需要运行中的 API，也不需要任何凭证。

三个 session 级 autouse 夹具按 `test_tenant_backfill.py` 的先例覆盖掉。它们在缺凭证时本来就会
直接返回，但显式覆盖能保证本目录**永远**不去 ping 真实 API——这里的验收对象是迁移子进程、
worker 子进程与独立测试实例，与运行中的服务无关。

`migrated_test_database` 是**整个目录的前置**：两轮（Step B 的迁移、Step C 的 worker）都要求
独立实例记着 `business=13 / knowledge=2`，所以它放在这里，session 级只跑一次。
"""

from __future__ import annotations

from collections.abc import Iterator

import anyio
import pytest

import _migration_round as round_module
from _migration_round import MigrationRound


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本目录不碰真实 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    yield


@pytest.fixture(scope="session")
def migrated_test_database() -> Iterator[MigrationRound]:
    """在独立测试实例上从**空库**跑一遍完整迁移，产出 `business=13 / knowledge=2`。

    前置是全新库（重建 `postgres-test`，tmpfs 重建即空）；不是全新库就直接失败，不做清残留。
    成功清理本轮临时根，失败保留日志与临时产物。
    """

    round_ = round_module.start_round()
    try:
        anyio.run(round_module.assert_target_is_the_test_instance_and_database_is_empty)
        round_module.run_full_migration(round_)
        yield round_
    finally:
        round_module.finish_round(round_)
