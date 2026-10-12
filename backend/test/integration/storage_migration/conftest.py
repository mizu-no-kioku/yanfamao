"""迁移验收目录的夹具：不需要运行中的 API，也不需要任何凭证。

三个 session 级 autouse 夹具按 `test_tenant_backfill.py` 的先例覆盖掉。它们在缺凭证时本来就会
直接返回，但显式覆盖能保证本目录**永远**不去 ping 真实 API——这里的验收对象是迁移子进程、
worker 子进程与独立测试实例，与运行中的服务无关。

`migrated_test_database` 与 `worker_round` 是本目录两轮的前置与主体，都放在这里。

**产物保留的依据是 pytest 的判定结果，不是子进程的退出码**：退出码为 0 只说明"迁移/worker 跑完了"，
版本记录、RLS、健康归属、目录断言任何一条失败时，日志与临时产物同样必须留下。所以这里用
`pytest_runtest_makereport` 记录已判定的失败，夹具在 teardown 时一并参考。

**覆盖范围（不要夸大）**：本夹具的 teardown 与"本轮 teardown 阶段的报告"是同一批 finalizer，
报告产生于清理**之后**，所以它保证的是 **setup / call 阶段的失败**（以及在本夹具之前完成清理的
其它 finalizer 的失败）。某个**在它之后**才失败的 teardown 步骤（含本夹具自己的 teardown 出错）
无法被它提前看到。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence

import anyio
import pytest

import _migration_round as round_module
import _worker_round as worker_round_module
from _migration_round import MigrationRound
from _worker_round import WorkerRound, WorkerRoundMutex

# 本次会话里已判定失败的用例。夹具 teardown 时看它，决定是清理还是保留产物。
# 用模块级可变对象而不是夹具：hook 拿不到夹具，而一个 conftest 每个会话只有一个实例。
_FAILED_TESTS: list[str] = []


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.failed:
        _FAILED_TESTS.append(report.nodeid)
    return report


def decide_keep_artifacts(*, round_failed: bool, failed_tests: Sequence[str]) -> bool:
    """轮次本身不成功，或任一相关用例已失败，都保留产物。**纯函数**，便于自检用例驱动。"""

    return round_failed or bool(failed_tests)


def keep_artifacts(*, round_failed: bool) -> bool:
    return decide_keep_artifacts(round_failed=round_failed, failed_tests=_FAILED_TESTS)


@pytest.fixture()
def artifact_decider() -> Callable[..., bool]:
    """把判定函数交给脚手架自检用例——它**不**暴露真实登记簿，自检用例不得改会话记录。"""

    return decide_keep_artifacts


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
    """

    round_ = round_module.start_round()
    try:
        anyio.run(round_module.assert_target_is_the_test_instance_and_database_is_empty)
        round_module.run_full_migration(round_)
        yield round_
    finally:
        round_module.finish_round(
            round_,
            keep_artifacts=keep_artifacts(round_failed=round_module.must_keep_artifacts(round_)),
        )


@pytest.fixture(scope="module")
def worker_round(migrated_test_database: MigrationRound) -> Iterator[WorkerRound]:
    """拿互斥、起 worker、等它报出 worker_id；结束时整组回收。"""

    # 守卫必须在**任何 Redis 写入之前**：拿互斥本身就是一次 Redis 写。
    worker_round_module.assert_redis_is_not_the_development_instance()

    with WorkerRoundMutex(worker_round_module.redis_test_url()) as mutex:
        round_ = worker_round_module.start_worker_round(mutex=mutex)
        try:
            yield round_
        finally:
            # 顺序是有意的：先回收整组、先保留产物，再报错——失败时现场还在。
            worker_round_module.stop_worker_round(
                round_,
                keep_artifacts=keep_artifacts(round_failed=worker_round_module.must_keep_artifacts(round_)),
            )
            worker_round_module.raise_if_round_failed(round_)
