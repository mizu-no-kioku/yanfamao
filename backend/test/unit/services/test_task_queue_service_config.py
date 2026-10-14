"""补发周期必须能被注入，且坏值一律硬失败。

Step D 的两个补发场景靠**注入短周期**才能快速、确定地拿到证据；而一个静默回落到 30 秒的
解析器会让"我注入了 1 秒"变成一句不成立的话——所以坏值不是"用默认值兜住"，是报错。
"""

from __future__ import annotations

import os

import pytest

from yuxi.services import task_queue_service

pytestmark = [pytest.mark.unit]


def test_the_default_reconciliation_period_is_thirty_seconds() -> None:
    assert task_queue_service.reconciliation_seconds_from_env(None) == 30.0
    assert task_queue_service.reconciliation_seconds_from_env("") == 30.0
    assert task_queue_service.reconciliation_seconds_from_env("   ") == 30.0


def test_a_positive_finite_value_is_taken_as_is() -> None:
    assert task_queue_service.reconciliation_seconds_from_env("1") == 1.0
    assert task_queue_service.reconciliation_seconds_from_env("0.25") == 0.25


@pytest.mark.parametrize(
    "raw",
    ["nan", "NaN", "inf", "-inf", "Infinity", "0", "-1", "abc", "3s"],
)
def test_every_bad_value_is_a_hard_failure(raw: str) -> None:
    """NaN/±inf/≤0 都不会自己报错——它们只会让周期补发静默失效，所以必须在这里就炸。"""

    with pytest.raises(RuntimeError, match=task_queue_service.TASK_RECONCILIATION_SECONDS_ENV):
        task_queue_service.reconciliation_seconds_from_env(raw)


def test_the_module_constant_is_derived_from_the_environment() -> None:
    """常量必须**由解析器**得出：写死 30.0 会让注入的子进程拿到错误周期而不自知。"""

    expected = task_queue_service.reconciliation_seconds_from_env(
        os.getenv(task_queue_service.TASK_RECONCILIATION_SECONDS_ENV)
    )
    assert task_queue_service.TASK_RECONCILIATION_SECONDS == expected


def test_the_health_ttl_is_derived_from_the_reconciliation_period() -> None:
    """TTL 短于一个周期的话，健康键会在 worker 仍健康时先过期，readiness 会误判。

    断言的是**跟着周期走**（注入 1 秒周期时 TTL 必须随之变小），不是钉死 65。
    """

    period = task_queue_service.TASK_RECONCILIATION_SECONDS
    assert task_queue_service.TASK_RECONCILIATION_HEALTH_TTL_SECONDS == int(period * 2 + 5)
    assert task_queue_service.TASK_RECONCILIATION_HEALTH_TTL_SECONDS > period


def test_the_cross_tenant_discovery_query_exposes_only_tenant_ids() -> None:
    """跨租户发现是全库唯一的跨租户读，边界是"只暴露租户 id"——不能顺带带出租户元数据。

    这条钉住 SQL 文本：一旦有人在这里加列（name / code / issuer …），普通运行时角色能看到的
    租户信息就超出"发现任务"所需，测试会立刻变红。
    """

    sql = task_queue_service.KNOWN_TENANT_IDS_SQL
    assert sql == "SELECT id FROM tenants ORDER BY id"
    assert "*" not in sql


async def test_known_tenant_ids_fails_closed_without_a_discovery_credential(monkeypatch) -> None:
    """没有发现凭据时，跨租户发现必须**直接失败**，不得回落到运行时 / owner 连接。

    这正是 API 的行为：API 不配置 `POSTGRES_DISCOVERY_URL`，所以它**枚举不了**租户——
    方案 (b) 的"消费者区分"在代码路径上就成立，而不是只靠部署约定。
    """

    from yuxi.storage.postgres.manager import pg_manager

    monkeypatch.setattr(pg_manager, "DiscoverySession", None)
    monkeypatch.setattr(pg_manager, "_initialized", True)  # 别让 initialize() 去连库

    with pytest.raises(RuntimeError, match="POSTGRES_DISCOVERY_URL"):
        await task_queue_service.known_tenant_ids()
