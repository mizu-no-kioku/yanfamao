"""Step D：补发**时机**的证据——启动补发与周期补发各一条，最后都**读库**验终态。

## 两个场景只差一件事：意图相对 worker 启动的时刻，以及注入的周期

| 场景 | 意图创建于 | 注入周期 | 唯一能救它的路径 |
|---|---|---|---|
| 启动补发 | worker 启动**之前** | 3600s（周期等于不发生） | `_worker_startup` 里的 `reconcile_and_publish_tasks()` |
| 周期补发 | worker **就绪之后** | 1s | `_reconcile_durable_tasks_forever()` 的那次 tick |

把启动场景的周期设成 3600s 是这条用例的**关键设计**，不是"顺手调大超时"：它让两条路径
不可能互相冒充——启动路径若没接线，这一轮不会靠 30 秒后的 tick 侥幸变绿，它会直接失败。
反过来，周期场景的意图在就绪之后才创建，启动路径**已经跑完了**，所以只有 tick 能救它。

## 证据是库，不是日志

见 `_durable_task_probe` 的 docstring：`pending → running → success` 与数据集 `completed`
都是 worker 自己写下的事务结果。日志只能说明"收敛循环跑了"，不说明任务被发布、被消费。

## 单次绿不算证据

这一整条链的原始缺陷就是"约 50% 通过"。README 里写了连续重跑的命令。
"""

from __future__ import annotations

import pytest

import _durable_task_probe as probe_module
import _worker_round as worker_round_module
from _durable_task_probe import TERMINAL_TASK_STATUSES
from _worker_round import managed_worker_round
from yuxi.services import task_queue_service

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# 一轮业务结果的**上界**，不是期望耗时：启动场景实测在 10 秒量级完成，周期场景在 2 秒量级。
# 真正提供确定性的是注入的周期，不是这个数字。
RUN_TIMEOUT_SECONDS = 45.0

# 周期设成"等于不发生"：启动场景里周期路径被彻底排除在外。
STARTUP_SCENARIO_PERIOD = "3600"
# 周期场景的短周期：tick 在 1 秒内就会来，所以窗口不需要长。
PERIODIC_SCENARIO_PERIOD = "1"


def _period_override(period: str) -> dict[str, str]:
    """环境变量名从生产模块取，不写字面量——改名会在导入处报错，而不是让注入静默失效。"""

    return {task_queue_service.TASK_RECONCILIATION_SECONDS_ENV: period}


def _terminal(state: dict) -> bool:
    return state["status"] in TERMINAL_TASK_STATUSES


async def test_startup_reconciliation_republishes_an_intent_committed_before_startup(
    migrated_test_database, session_failed_tests
) -> None:
    """意图**先**落库、worker **后**启动 → 只能由 `_worker_startup` 的收敛把它推到终态。"""

    probe = probe_module.new_probe_task("startup")
    try:
        async with managed_worker_round(
            prepare=lambda: probe_module.insert_pending_intent(probe),
            env_overrides=_period_override(STARTUP_SCENARIO_PERIOD),
            failed_tests=session_failed_tests,
        ) as round_:
            state = await worker_round_module.wait_for_round_result(
                round_,
                lambda: probe_module.read_task_state(probe.task_id),
                terminal=_terminal,
                timeout_seconds=RUN_TIMEOUT_SECONDS,
                description="启动补发没有把启动前落库的 pending 意图推到终态",
            )
            metadata = await probe_module.read_dataset_metadata(probe.dataset_id)

        assert state["status"] == probe_module.EXPECTED_TERMINAL_STATUS, (
            f"启动补发把意图推到了 {state['status']}（error={state['error']!r}），不是期望的 success"
        )
        # attempt_count == 1：证明它被**消费过一次**，而不是被收敛循环直接判失败（那条路径
        # 不会增加 attempt_count，`fail_pending` 只写 status/error）。
        assert state["attempt_count"] == 1, f"attempt_count={state['attempt_count']}，不是恰好执行一次"
        assert (metadata or {}).get("status") == probe_module.EXPECTED_DATASET_STATUS, (
            f"领域 success hook 没有把数据集收敛到 completed：{metadata!r}"
        )
    finally:
        await probe_module.delete_probe_rows(probe)


async def test_periodic_reconciliation_republishes_an_intent_committed_after_startup(
    migrated_test_database, session_failed_tests
) -> None:
    """worker 就绪**之后**才落库的意图 → 启动收敛已经跑完，只能由周期 tick 推到终态。"""

    probe = probe_module.new_probe_task("periodic")
    try:
        async with managed_worker_round(
            env_overrides=_period_override(PERIODIC_SCENARIO_PERIOD),
            failed_tests=session_failed_tests,
        ) as round_:
            # 就绪之后再落库：这条顺序本身就是本用例的意图，启动补发对它无能为力。
            assert round_.worker_id is not None, "worker 没就绪，本用例的前提不成立"
            await probe_module.insert_pending_intent(probe)
            assert (await probe_module.read_task_state(probe.task_id))["status"] == "pending"

            state = await worker_round_module.wait_for_round_result(
                round_,
                lambda: probe_module.read_task_state(probe.task_id),
                terminal=_terminal,
                timeout_seconds=RUN_TIMEOUT_SECONDS,
                description="周期补发没有把就绪后落库的 pending 意图推到终态",
            )
            metadata = await probe_module.read_dataset_metadata(probe.dataset_id)

        assert state["status"] == probe_module.EXPECTED_TERMINAL_STATUS, (
            f"周期补发把意图推到了 {state['status']}（error={state['error']!r}），不是期望的 success"
        )
        assert state["attempt_count"] == 1, f"attempt_count={state['attempt_count']}，不是恰好执行一次"
        assert (metadata or {}).get("status") == probe_module.EXPECTED_DATASET_STATUS, (
            f"领域 success hook 没有把数据集收敛到 completed：{metadata!r}"
        )
    finally:
        await probe_module.delete_probe_rows(probe)
