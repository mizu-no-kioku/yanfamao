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
