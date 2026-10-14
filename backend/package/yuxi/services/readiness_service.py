"""API 接流量前的核心依赖就绪探针。"""

from __future__ import annotations

import asyncio
import copy
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from yuxi.config.multi_tenant import multi_tenant_enabled
from yuxi.services.run_queue_service import (
    WORKER_RECONCILIATION_HEALTH_KEY,
    WORKER_RECONCILIATION_HEALTH_TTL_SECONDS,
    get_redis_client,
)
from yuxi.services.task_queue_service import (
    TASK_RECONCILIATION_HEALTH_KEY,
    TASK_RECONCILIATION_HEALTH_TTL_SECONDS,
)
from yuxi.services.worker_health import WORKER_HEALTH_KEY, WORKER_HEALTH_MAX_TTL_MS
from yuxi.storage.postgres.manager import pg_manager

READINESS_PROBE_TIMEOUT_SECONDS = float(os.getenv("READINESS_PROBE_TIMEOUT_SECONDS", "2"))
READINESS_CACHE_TTL_SECONDS = float(os.getenv("READINESS_CACHE_TTL_SECONDS", "1"))
# Postgres 探针是**纯连通性**探针，不是业务查询。
#
# 为什么钉成常量：readiness 在**无租户上下文**下运行（免租户端点），若这里改成查按租户隔离的
# 业务表，RLS 会返回 **0 行**而不是报错——"查询成功"于是变成**错误的就绪信号**。`SELECT 1`
# 不引用任何表，空结果问题从根上不存在。测试断言这条 SQL 不含表名。
POSTGRES_PROBE_SQL = "SELECT 1"
Probe = Callable[[], Awaitable[None]]
_readiness_cache: tuple[tuple, float, dict[str, Any]] | None = None
_readiness_inflight: dict[tuple, asyncio.Task] = {}


async def _probe_postgres() -> None:
    """验证业务 PostgreSQL 连接能够执行查询（纯连通性，不读任何业务表）。"""

    async with pg_manager.get_async_session_context() as session:
        await session.execute(text(POSTGRES_PROBE_SQL))


async def _probe_redis() -> None:
    """验证 Run 队列 Redis 能够响应命令。"""

    redis = await get_redis_client()
    await redis.ping()


class WorkerUnavailableError(RuntimeError):
    """当前队列没有完成启动且仍在续租的兼容 worker。"""


async def _probe_worker() -> None:
    """验证兼容 AgentRun worker 的短 TTL 健康事实仍然存在。"""

    redis = await get_redis_client()
    leases = (
        (WORKER_HEALTH_KEY, WORKER_HEALTH_MAX_TTL_MS),
        (WORKER_RECONCILIATION_HEALTH_KEY, WORKER_RECONCILIATION_HEALTH_TTL_SECONDS * 1000),
        (TASK_RECONCILIATION_HEALTH_KEY, TASK_RECONCILIATION_HEALTH_TTL_SECONDS * 1000),
    )
    for key, max_ttl_ms in leases:
        value = await redis.get(key)
        ttl_ms = await redis.pttl(key)
        if not value or ttl_ms <= 0 or ttl_ms > max_ttl_ms:
            raise WorkerUnavailableError("worker health lease missing or invalid")


async def _run_probe(probe: Probe) -> dict[str, str]:
    try:
        await asyncio.wait_for(probe(), timeout=READINESS_PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return {"status": "error", "code": "timeout"}
    except Exception as exc:
        return {"status": "error", "code": type(exc).__name__}
    return {"status": "ok"}


def _component_snapshot(components: dict[str, dict[str, Any]] | None) -> tuple:
    """把启动组件状态规整为缓存 key，不包含异常消息或其他敏感值。"""

    return tuple(
        sorted(
            (
                str(name),
                str(component.get("status") or "unknown"),
                bool(component.get("required", False)),
                str(component.get("code") or ""),
            )
            for name, component in (components or {}).items()
            if isinstance(component, dict)
        )
    )


def _tenant_context_check() -> dict[str, str]:
    """多租户模式下租户声明解析未接入 → **不能**报就绪。

    readiness 的语义是"可以接业务流量"。多租户部署当前会把**所有业务请求 503**（Claim 解析
    尚未接入），此时若仍报"就绪"，就是在探活通过、业务全挂的状态下发出错误信号。所以这里
    显式失败，让编排器不要往这个实例发流量。
    """

    if multi_tenant_enabled():
        return {"status": "error", "code": "tenant_claim_resolution_unavailable"}
    return {"status": "ok"}


async def _compute_readiness(*, startup_complete: bool, component_snapshot: tuple) -> dict[str, Any]:
    """执行一次真实探针并合并启动组件事实。"""

    postgres, redis, worker = await asyncio.gather(
        _run_probe(_probe_postgres),
        _run_probe(_probe_redis),
        _run_probe(_probe_worker),
    )
    components = {
        name: {
            "status": status,
            "required": required,
            **({"code": code} if code else {}),
        }
        for name, status, required, code in component_snapshot
    }
    checks = {
        "startup": {"status": "ok"} if startup_complete else {"status": "error", "code": "not_complete"},
        "postgres": postgres,
        "redis": redis,
        "worker": worker,
        "tenant_context": _tenant_context_check(),
    }
    required_components_ready = all(
        component["status"] == "ok" for component in components.values() if component["required"]
    )
    ready = all(check["status"] == "ok" for check in checks.values()) and required_components_ready
    degraded = any(component["status"] == "error" for component in components.values() if not component["required"])
    return {
        "status": "ready" if ready else "not_ready",
        "degraded": degraded,
        "checks": checks,
        "components": components,
    }


async def get_readiness(
    *,
    startup_complete: bool,
    startup_components: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """返回带短缓存与 single-flight 的结构化接流量事实。"""

    global _readiness_cache

    component_snapshot = _component_snapshot(startup_components)
    cache_key = (
        startup_complete,
        component_snapshot,
        id(_probe_postgres),
        id(_probe_redis),
        id(_probe_worker),
        # 多租户开关参与缓存键：它决定 `tenant_context` 检查的结果，不带上就会在开关变化后
        # 继续返回旧结论（1 秒 TTL 内，或单飞期间）。
        multi_tenant_enabled(),
    )
    now = time.monotonic()
    if _readiness_cache is not None:
        cached_key, expires_at, cached_result = _readiness_cache
        if cached_key == cache_key and now < expires_at:
            return copy.deepcopy(cached_result)

    task = _readiness_inflight.get(cache_key)
    if task is None:
        task = asyncio.create_task(
            _compute_readiness(startup_complete=startup_complete, component_snapshot=component_snapshot)
        )
        _readiness_inflight[cache_key] = task
    try:
        result = await asyncio.shield(task)
    finally:
        if task.done() and _readiness_inflight.get(cache_key) is task:
            _readiness_inflight.pop(cache_key, None)

    _readiness_cache = (cache_key, time.monotonic() + max(0.0, READINESS_CACHE_TTL_SECONDS), result)
    return copy.deepcopy(result)
