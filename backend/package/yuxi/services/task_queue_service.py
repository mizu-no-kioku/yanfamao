from __future__ import annotations

import math
import os
from functools import partial

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import text
from yuxi.repositories.task_repository import TaskRepository
from yuxi.services.run_queue_service import get_arq_pool
from yuxi.services.task_registry import get_failure_task_definition, get_task_definition
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.tenant_context import use_tenant
from yuxi.utils.logging_config import logger

TASK_LEASE_SECONDS = 30.0
TASK_HEARTBEAT_SECONDS = 10.0

# 已知的**连接类**异常族：只列客户端实际会抛的那几种。
#
# 刻意**不**用宽泛的 `OSError`：它同时包含 `PermissionError` / `FileNotFoundError` /
# `IsADirectoryError` 等权限与文件错误，把那些也算成"队列不可用"就是把根因藏起来。内建
# `ConnectionError` / `TimeoutError` 虽是 `OSError` 子类，但语义明确，保留；redis-py 的异常
# **不**继承内建 `ConnectionError`（继承 `RedisError`），所以单独列出。`RedisError` 里还有
# `ResponseError`（协议/数据错误）等，**不**算连接类，故不用 `RedisError` 做通配。
#
# 这个分类**只是诊断提示**：为真时也只说"疑似队列不可用"，不构成根因结论；任何自动恢复
# 动作都不得以它为条件（见 `TaskPublicationUnavailableError`）。
QUEUE_UNAVAILABLE_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    RedisConnectionError,
    RedisTimeoutError,
)

DEFAULT_TASK_RECONCILIATION_SECONDS = 30.0
TASK_RECONCILIATION_SECONDS_ENV = "TASK_RECONCILIATION_SECONDS"


def reconciliation_seconds_from_env(raw: str | None) -> float:
    """解析 pending 意图补发周期：缺省 30，必须是**有限正数**。

    坏值一律硬失败，因为它们在运行期的表现既不可预期也不显眼——**异常、无效等待、或高频循环**，
    而它们都不会让"周期补发坏了"这件事自己浮出来。本机 Python 3.13.16 实测四种表现各不相同：
    `sleep(0)` / `sleep(-1)` 只是让出执行权就立刻返回（微秒级），`sleep(nan)` 直接抛
    `ValueError: Invalid delay: NaN`，`sleep(inf)` 则永不醒来——所以这里的判据是"有限且为正"，
    而不是给某一种坏值编一句行为描述。回落到默认值同样不可接受——"我注入了 1 秒"变成静默的
    30 秒，正是这一步要根除的"看起来生效其实没有"。
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
            "非有限值或非正周期会造成异常、无效等待或高频循环，因此启动时拒绝。"
        )
    return value


TASK_RECONCILIATION_SECONDS = reconciliation_seconds_from_env(os.getenv(TASK_RECONCILIATION_SECONDS_ENV))
TASK_RECONCILIATION_HEALTH_KEY = "yuxi:worker:health:durable-task-reconciliation-v1"
# 健康键的 TTL 必须覆盖**至少两个**周期：一个周期内没续上就说明补发能力已经不可信。
TASK_RECONCILIATION_HEALTH_TTL_SECONDS = int(TASK_RECONCILIATION_SECONDS * 2 + 5)


# 跨租户发现的**唯一**查询：只取 `id`，不取任何租户元数据（名称 / code / issuer 等）。
# 改这一行等于改动"worker 能看到哪些租户"的边界，必须配测试与说明。
KNOWN_TENANT_IDS_SQL = "SELECT id FROM tenants ORDER BY id"


async def known_tenant_ids() -> list[int]:
    """可处理的租户集合，来源是 `tenants` 表——**跨租户发现的受控入口**。

    `tenants` 不是归属根、**没有 RLS**，运行时可读——这正是"跨租户发现任务"的锚点：
    归属根表（`tasks` 等）在 RLS 下按当前租户过滤，必须先知道租户才能逐个进入其上下文。
    返回按 id 升序去重。

    **访问边界（必须保持）**：

    - 这是全代码库**唯一**的跨租户读。它只暴露**租户 id**（`KNOWN_TENANT_IDS_SQL`），
      不暴露任何租户元数据；不要在这里加列。
    - **权限边界（已实现，选项 b）**：本查询走**专用发现连接**
      （`get_discovery_session_context()` → 角色 `yuxi_worker_discovery`），该角色只有
      `SELECT (id) ON tenants`；运行时角色 `yuxi_app`（API 与 worker 共用）对 `tenants`
      **完全无权限**。API 不配置 `POSTGRES_DISCOVERY_URL`，因此**拿不到**这条能力。
      未配置发现连接串时本函数**直接抛错**，不回落。
      见 `docs/develop-guides/version-14-worker-tenant-chain-spec.md` §5。
    - 调用方只应是 **worker 侧收敛 / 补发**（`reconcile_and_publish_tasks` /
      `publish_pending_tasks`），**不应**出现在请求处理路径上。
    """

    async with pg_manager.get_discovery_session_context() as session:
        rows = await session.execute(text(KNOWN_TENANT_IDS_SQL))
        return sorted({int(row[0]) for row in rows})


async def publish_task(task_id: str, tenant_id: int) -> None:
    """把已提交的 PG Task 发布为 ARQ 唤醒消息。

    payload 携带 `tenant_id`，消费侧据此建立事务上下文；但**消费侧不信任 payload**，
    真正的归属以 `tasks.tenant_id` 为准（见 `process_task`）。重复消息由数据库 claim 拒绝。
    """

    pool = await get_arq_pool()
    await pool.enqueue_job("process_task", task_id, int(tenant_id))


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
    """**按租户**收敛失联 owner，并发布所有当前 pending Task。

    `tasks` 是归属根：单个租户上下文下只能看到该租户的行，所以收敛与补发都必须逐租户进入
    其上下文，而不是在某一个租户上下文里做一次全局扫描（那样会漏掉其他租户的任务）。
    """

    repository = TaskRepository()
    reconciled: list[tuple[str, str, int]] = []
    for tenant_id in await known_tenant_ids():
        with use_tenant(tenant_id):
            reconciled.extend(await repository.reconcile_expired_leases(before_fail=finalize_task_failure))
            await repository.prune_terminal()
    await publish_pending_tasks()
    return reconciled


class TaskPublicationUnavailableError(RuntimeError):
    """本轮**全部**发布尝试失败——**不**断言根因。

    只针对"有任务要发、却一条都没发出去"的整轮失败。单个任务发布失败属**隔离失败**：保留
    pending、记日志、继续处理其他任务与其他租户，下一轮再试——不升级为本异常。

    **为什么不说"系统级故障"**：全部失败也可能是任务参数、序列化或程序错误。这里只报告
    "全部发布尝试失败"，并用 `queue_unavailable_suspected` 说明失败是否**全部**落在已知的
    连接类异常上——那只是"**疑似**队列不可用"的诊断提示，不是根因结论。原始异常通过
    `__cause__` 保留类型与堆栈（`raise ... from`）。

    **不得据此自动恢复**：该字段不构成采取破坏性恢复动作（如清空队列、重建任务）的依据。
    """

    def __init__(self, attempted: int, failures: int, *, queue_unavailable_suspected: bool):
        if queue_unavailable_suspected:
            detail = "失败全部为连接类异常，疑似队列不可用"
        else:
            detail = "失败含非连接类异常，可能是任务参数 / 序列化 / 程序错误"
        super().__init__(f"本轮全部发布尝试失败：{failures}/{attempted} 条任务均未能入队（{detail}）。")
        self.attempted = attempted
        self.failures = failures
        # 名字里带 `suspected`：它是诊断提示，不是根因判定，也不得触发自动恢复。
        self.queue_unavailable_suspected = queue_unavailable_suspected


async def publish_pending_tasks(*, limit: int = 200) -> list[str]:
    """**按租户**重发 PG 中待执行的任务；重复 ARQ 消息由 task_id/attempt 去重。

    **失败隔离契约**（避免"一个租户的发布失败阻断其他租户"）：

    - **单任务**发布失败：保留 pending、记日志（含 task_id / tenant_id / 堆栈）、**继续**处理
      同租户的下一个任务以及其他租户——不中断整轮。下一轮补发会重试（任务没被改动，仍是 pending）。
    - **整轮全失败**（有任务要发但**一条都没发出去**）：走完所有租户后抛
      `TaskPublicationUnavailableError`，**显式**报告本轮失败，而不是静默返回空列表。
      已知边界：只有 1 个 pending 任务时，一次瞬时抖动也会被计入"整轮失败"而报错——这是有意
      的保守偏向（宁可报错也不静默），下一轮仍会重试。
    """

    published: list[str] = []
    attempted = 0
    failures: list[BaseException] = []
    repository = TaskRepository()
    for tenant_id in await known_tenant_ids():
        with use_tenant(tenant_id):
            for record in await repository.list_pending(limit=limit):
                try:
                    get_task_definition(record.type)
                except ValueError as exc:
                    logger.error("Cannot publish unknown durable task: task_id={}, type={}", record.id, record.type)

                    await repository.fail_pending(record.id, error=str(exc))
                    continue
                handler_version = 1 if record.handler_version is None else int(record.handler_version)
                if record.cancel_requested:
                    failure_handler = _load_failure_handler(record.type, handler_version)
                    before_cancel = partial(failure_handler, error="任务已取消") if failure_handler else None
                    await repository.request_cancel(record.id, before_cancel=before_cancel)
                    continue
                try:
                    get_task_definition(record.type, handler_version)
                except ValueError as exc:
                    error = str(exc)
                    logger.error("Cannot rebuild durable task: task_id={}, type={}", record.id, record.type)
                    failure_handler = _load_failure_handler(record.type, handler_version)
                    before_fail = partial(failure_handler, error=error) if failure_handler is not None else None
                    await repository.fail_pending(record.id, error=error, before_fail=before_fail)
                    continue
                attempted += 1
                try:
                    await publish_task(record.id, tenant_id)
                except Exception as exc:
                    # 隔离失败：不改动任务（仍是 pending），记诊断，继续下一个任务/租户。
                    failures.append(exc)
                    _log_publication_failure(record.id, tenant_id, exc)
                    continue
                published.append(record.id)
    if attempted and len(failures) == attempted:
        # 全部失败：报告"本轮全部发布尝试失败"，不判定根因；原始异常保留为 __cause__。
        # `queue_unavailable_suspected` 只是诊断提示，不得据此自动采取破坏性恢复动作。
        queue_unavailable_suspected = all(_is_queue_unavailable_error(exc) for exc in failures)
        raise TaskPublicationUnavailableError(
            attempted=attempted,
            failures=len(failures),
            queue_unavailable_suspected=queue_unavailable_suspected,
        ) from failures[-1]
    return published


def _load_failure_handler(task_type: str, handler_version: int):
    """尽力取失败收敛 Handler；类型/版本未知时返回 None（与内联 try/except 等价）。"""

    try:
        definition = get_failure_task_definition(task_type, handler_version)
    except ValueError:
        return None
    return definition.load_failure_handler()


def _is_queue_unavailable_error(exc: BaseException) -> bool:
    """该异常是否属已知的**连接类**异常族。仅用于诊断分类，不是根因结论。"""

    return isinstance(exc, QUEUE_UNAVAILABLE_ERRORS)


def _log_publication_failure(task_id: str, tenant_id: int, exc: BaseException) -> None:
    """记录单任务发布失败：**实际**输出 task_id / tenant_id 与异常堆栈。

    本项目 `logger` 是 Loguru：位置参数按 `str.format` 的 `{}` 占位符插值，`%s` 会**原样**
    打进日志（`%s` 与 args 都会被丢弃）；`exc_info=True` 也不是 Loguru 的写法（会被当作
    `str.format` 的具名参数忽略）。所以这里用 `{}` + `logger.opt(exception=...)`。
    """

    logger.opt(exception=exc).error(
        "Task publication failed; keeping pending for the next round: task_id={}, tenant_id={}",
        task_id,
        tenant_id,
    )
