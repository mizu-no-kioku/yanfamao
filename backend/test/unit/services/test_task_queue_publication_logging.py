"""发布失败的**日志内容**与异常分类。

本项目 `logger` 是 Loguru：位置参数按 `str.format` 的 `{}` 插值，`%s` 会原样打进日志；
`exc_info=True` 也不是 Loguru 的写法（被当作具名参数忽略）。所以这里断言的是**真实日志文本**
里出现了 task_id / tenant_id 与异常堆栈，而不是"logger 被调用过"。
"""

from __future__ import annotations

import pytest
from redis.exceptions import ResponseError

from yuxi.services.task_queue_service import (
    TaskPublicationUnavailableError,
    _is_queue_unavailable_error,
    _log_publication_failure,
)
from yuxi.utils.logging_config import logger

pytestmark = [pytest.mark.unit]


def _capture_errors() -> tuple[list[str], int]:
    captured: list[str] = []
    handler_id = logger.add(captured.append, format="{message}", level="ERROR")
    return captured, handler_id


def test_publication_failure_log_carries_the_ids_and_the_traceback() -> None:
    captured, handler_id = _capture_errors()
    try:
        try:
            raise ConnectionError("queue down")
        except ConnectionError as exc:
            _log_publication_failure("task-abc123", 7, exc)
    finally:
        logger.remove(handler_id)

    text = "\n".join(captured)

    # `%s` 版会在这里失败：占位符原样输出、参数被丢弃，task_id 不会出现。
    assert "task-abc123" in text
    assert "tenant_id=7" in text
    # 异常必须带上原始信息与堆栈（`exc_info=True` 版两者都不会出现）。
    assert "queue down" in text
    assert "ConnectionError" in text
    assert "Traceback" in text


def test_publication_failure_log_does_not_leak_the_literal_placeholder() -> None:
    captured, handler_id = _capture_errors()
    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            _log_publication_failure("task-xyz", 3, exc)
    finally:
        logger.remove(handler_id)

    assert "%s" not in "\n".join(captured)


@pytest.mark.parametrize(
    "exc",
    [ConnectionError("down"), TimeoutError("slow")],
)
def test_builtin_connection_errors_count_as_queue_unavailable(exc: BaseException) -> None:
    assert _is_queue_unavailable_error(exc) is True


def test_redis_connection_errors_count_as_queue_unavailable() -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    assert _is_queue_unavailable_error(RedisConnectionError("down")) is True
    assert _is_queue_unavailable_error(RedisTimeoutError("slow")) is True


@pytest.mark.parametrize(
    "exc",
    [
        # 关键反例：这些**都是** `OSError` 子类，但都不是连接问题。
        PermissionError("denied"),
        FileNotFoundError("missing"),
        IsADirectoryError("is a directory"),
        # redis 的协议/数据错误也不算"不可用"。
        ResponseError("WRONGTYPE"),
    ],
)
def test_non_connection_errors_are_not_classified_as_queue_unavailable(exc: BaseException) -> None:
    """不能用宽泛的 `OSError` / `RedisError` 通配——那会把权限、文件、协议错误算成"队列不可用"。"""

    assert _is_queue_unavailable_error(exc) is False


@pytest.mark.parametrize(
    "exc",
    [ValueError("bad payload"), TypeError("cannot serialize"), KeyError("missing")],
)
def test_programming_errors_are_not_classified_as_queue_unavailable(exc: BaseException) -> None:
    """程序/数据错误不能被归为"队列不可用"——那会把根因藏起来。"""

    assert _is_queue_unavailable_error(exc) is False


def test_all_queue_errors_report_a_suspicion_without_claiming_the_root_cause() -> None:
    error = TaskPublicationUnavailableError(attempted=3, failures=3, queue_unavailable_suspected=True)

    assert error.attempted == 3
    assert error.failures == 3
    assert error.queue_unavailable_suspected is True
    assert "全部发布尝试失败" in str(error)
    # 措辞是"疑似"，不是根因结论。
    assert "疑似队列不可用" in str(error)
    assert "系统级故障" not in str(error)


def test_mixed_failures_are_reported_as_possibly_programming_errors() -> None:
    error = TaskPublicationUnavailableError(attempted=2, failures=2, queue_unavailable_suspected=False)

    assert error.queue_unavailable_suspected is False
    assert "全部发布尝试失败" in str(error)
    assert "非连接类异常" in str(error)
