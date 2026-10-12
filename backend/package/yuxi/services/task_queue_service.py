from __future__ import annotations

import math
import os
from functools import partial

from yuxi.repositories.task_repository import TaskRepository
from yuxi.services.run_queue_service import get_arq_pool
from yuxi.services.task_registry import get_failure_task_definition, get_task_definition
from yuxi.utils.logging_config import logger

TASK_LEASE_SECONDS = 30.0
TASK_HEARTBEAT_SECONDS = 10.0

DEFAULT_TASK_RECONCILIATION_SECONDS = 30.0
TASK_RECONCILIATION_SECONDS_ENV = "TASK_RECONCILIATION_SECONDS"


def reconciliation_seconds_from_env(raw: str | None) -> float:
    """解析 pending 意图补发周期：缺省 30，必须是**有限正数**。

    三种坏值都不会自己报错，只会让周期补发静默失效，所以这里一律硬失败：
    `nan` 会让 `asyncio.sleep` 立刻返回（收敛循环退化成忙等）、`inf` 与 `<= 0` 会让它永不醒来，
    非数字则说明配置打错了。回落到默认值同样不可接受——"我注入了 1 秒"变成静默的 30 秒，
    正是这一步要根除的"看起来生效其实没有"。
    """

    if raw is None or not raw.strip():
        return DEFAULT_TASK_RECONCILIATION_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{TASK_RECONCILIATION_SECONDS_ENV}={raw!r} 不是数字。"
            f"留空用默认 {DEFAULT_TASK_RECONCILIATION_SECONDS:g} 秒。"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(
            f"{TASK_RECONCILIATION_SECONDS_ENV}={raw!r} 必须是有限正数——"
            "NaN 会让 asyncio.sleep 立刻返回（循环变忙等），±inf 与 ≤0 会让补发永不发生。"
        )
    return value


TASK_RECONCILIATION_SECONDS = reconciliation_seconds_from_env(os.getenv(TASK_RECONCILIATION_SECONDS_ENV))
TASK_RECONCILIATION_HEALTH_KEY = "yuxi:worker:health:durable-task-reconciliation-v1"
# 健康键的 TTL 必须覆盖**至少两个**周期：一个周期内没续上就说明补发能力已经不可信。
TASK_RECONCILIATION_HEALTH_TTL_SECONDS = int(TASK_RECONCILIATION_SECONDS * 2 + 5)


async def publish_task(task_id: str) -> None:
    """把已提交的 PG Task 发布为 ARQ 唤醒消息；重复消息由数据库 claim 拒绝。"""
    pool = await get_arq_pool()
    await pool.enqueue_job("process_task", task_id)


async def finalize_task_failure(session, record, error: str) -> None:
    """在 Task 失败事务内执行已注册的领域收敛。"""
    handler_version = 1 if record.handler_version is None else int(record.handler_version)
    try:
        definition = get_failure_task_definition(record.type, handler_version)
    except ValueError:
        logger.error(
            "Cannot finalize unknown durable task: task_id=%s, type=%s, handler_version=%s",
            record.id,
            record.type,
            handler_version,
        )
        return
    handler = definition.load_failure_handler()
    if handler is not None:
        await handler(session, record, error)


async def reconcile_and_publish_tasks() -> list[tuple[str, str, int]]:
    """收敛失联 owner，并发布所有当前 pending Task。"""
    repository = TaskRepository()
    reconciled = await repository.reconcile_expired_leases(
        before_fail=finalize_task_failure,
    )
    await publish_pending_tasks()
    await repository.prune_terminal()
    return reconciled


async def publish_pending_tasks(*, limit: int = 200) -> list[str]:
    """重发 PG 中待执行的任务；重复 ARQ 消息由 task_id/attempt 去重。"""
    published: list[str] = []
    repository = TaskRepository()
    for record in await repository.list_pending(limit=limit):
        try:
            get_task_definition(record.type)
        except ValueError as exc:
            logger.error("Cannot publish unknown durable task: task_id=%s, type=%s", record.id, record.type)
            await repository.fail_pending(record.id, error=str(exc))
            continue
        handler_version = 1 if record.handler_version is None else int(record.handler_version)
        if record.cancel_requested:
            try:
                failure_definition = get_failure_task_definition(record.type, handler_version)
            except ValueError:
                failure_definition = None
            failure_handler = failure_definition.load_failure_handler() if failure_definition is not None else None
            before_cancel = partial(failure_handler, error="任务已取消") if failure_handler is not None else None
            await repository.request_cancel(record.id, before_cancel=before_cancel)
            continue
        try:
            get_task_definition(record.type, handler_version)
        except ValueError as exc:
            error = str(exc)
            logger.error("Cannot rebuild durable task: task_id=%s, type=%s", record.id, record.type)
            try:
                failure_definition = get_failure_task_definition(record.type, handler_version)
            except ValueError:
                failure_definition = None
            failure_handler = failure_definition.load_failure_handler() if failure_definition is not None else None
            before_fail = partial(failure_handler, error=error) if failure_handler is not None else None
            await repository.fail_pending(record.id, error=error, before_fail=before_fail)
            continue
        await publish_task(record.id)
        published.append(record.id)
    return published
