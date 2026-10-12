"""worker 启动等待 PostgreSQL 就绪：有上限、可观测、正常路径不被拖慢。

项目 logger 是 loguru，不进入 stdlib logging，因此这里直接替换 worker 模块的
logger 为记录桩，按 loguru 的 `{}` 风格格式化后断言——比 caplog 更直接。
"""

from __future__ import annotations

import asyncio

import pytest

from yuxi.services import run_worker


class RecordingLogger:
    """按 loguru 的 `{}` 风格记录消息。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def _render(self, message, args) -> str:
        return message.format(*args) if args else message

    def warning(self, message, *args) -> None:
        self.warnings.append(self._render(message, args))

    def error(self, message, *args) -> None:
        self.errors.append(self._render(message, args))

    def info(self, message, *args) -> None:  # pragma: no cover - 本测试不关心
        pass


class FakeClock:
    """可控时钟与 sleep，避免测试真的等待。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeCannotConnectNow(Exception):
    """替代 asyncpg 的「数据库正在启动」异常。"""


@pytest.fixture
def env(monkeypatch):
    """提供假时钟与记录型 logger，并清掉可能影响结果的环境变量。"""
    clock = FakeClock()
    logger = RecordingLogger()
    monkeypatch.setattr(run_worker.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(run_worker.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(run_worker, "logger", logger)
    monkeypatch.setattr(run_worker, "_transient_database_errors", lambda: (FakeCannotConnectNow,))
    monkeypatch.delenv("YUXI_WORKER_DB_READY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("YUXI_WORKER_DB_READY_INTERVAL_SECONDS", raising=False)
    return clock, logger


def test_first_attempt_success_does_not_sleep_or_warn(env, monkeypatch):
    """正常启动路径：一次就绪即返回，不等待、不产生告警，不被拖慢。"""
    clock, logger = env
    calls = []

    async def ready():
        calls.append(1)

    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", ready)

    asyncio.run(run_worker._await_database_ready())

    assert len(calls) == 1
    assert clock.slept == [], "就绪时不得有任何等待"
    assert logger.warnings == [], "就绪时不应有重试告警"


def test_retries_until_database_is_ready(env, monkeypatch):
    """数据库暂时不可用 → 有界重试 → 恢复后自动继续，并留下可观测记录。"""
    clock, logger = env
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise FakeCannotConnectNow("the database system is starting up")

    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", flaky)

    asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 3
    assert len(clock.slept) == 2, "前两次失败应各等待一次"
    assert any("暂不可用" in w for w in logger.warnings), "重试过程必须可见"
    assert any("已就绪" in w for w in logger.warnings), "恢复必须有明确记录"


def test_timeout_raises_with_clear_error(env, monkeypatch):
    """超过上限仍不可用 → 放弃启动，日志点明上限、次数与错误类型。"""
    clock, logger = env
    monkeypatch.setenv("YUXI_WORKER_DB_READY_TIMEOUT_SECONDS", "5")

    async def never_ready():
        raise FakeCannotConnectNow("the database system is starting up")

    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", never_ready)

    with pytest.raises(FakeCannotConnectNow):
        asyncio.run(run_worker._await_database_ready())

    assert any("未在 5 秒内就绪" in e and "attempts=" in e for e in logger.errors), logger.errors
    assert clock.now <= 5, "重试总时长必须受上限约束"


def test_non_transient_error_fails_fast(env, monkeypatch):
    """非连接类错误立即抛出，不被重试拖到超时——真问题必须立刻可见。"""
    clock, _logger = env
    attempts = []

    async def broken():
        attempts.append(1)
        raise ValueError("schema version mismatch")

    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", broken)

    with pytest.raises(ValueError):
        asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 1
    assert clock.slept == []


def test_cannot_connect_now_is_covered_by_transient_errors():
    """`CannotConnectNowError` 继承自 OperatorInterventionError，必须被显式覆盖。"""
    import asyncpg

    assert asyncpg.exceptions.CannotConnectNowError in run_worker._transient_database_errors()


def test_permanent_auth_error_fails_immediately(env, monkeypatch):
    """密码错误等永久性配置/认证问题必须立刻失败，不得重试到超时。

    这类错误重试再多次也不会好，把部署配置错误拖成 300 秒静默等待，
    只会让排障更难。
    """
    clock, _logger = env
    monkeypatch.setenv("YUXI_WORKER_DB_READY_TIMEOUT_SECONDS", "300")

    import asyncpg

    attempts = []

    async def wrong_password():
        attempts.append(1)
        raise asyncpg.exceptions.InvalidPasswordError("password authentication failed")

    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", wrong_password)

    with pytest.raises(asyncpg.exceptions.InvalidPasswordError):
        asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 1, f"永久性错误只应尝试一次，实际 {len(attempts)} 次"
    assert clock.slept == [], "永久性错误不得进入等待"


def test_permanent_database_config_error_fails_immediately(env, monkeypatch):
    """数据库不存在等永久性配置问题同样立刻失败。

    用 asyncpg 的真实异常类型，而不是泛化 RuntimeError——以下四种情形已对
    真实 PostgreSQL 实测：密码错误 → InvalidPasswordError、库名不存在 →
    InvalidCatalogNameError，两者都不该重试；PostgreSQL 不可达 →
    ConnectionRefusedError、数据库正在启动 → CannotConnectNowError，两者该重试。
    """
    clock, _logger = env
    monkeypatch.setenv("YUXI_WORKER_DB_READY_TIMEOUT_SECONDS", "300")

    import asyncpg

    attempts = []

    async def missing_database():
        attempts.append(1)
        raise asyncpg.exceptions.InvalidCatalogNameError('database "nonexistent" does not exist')

    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", missing_database)

    with pytest.raises(asyncpg.exceptions.InvalidCatalogNameError):
        asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 1
    assert clock.slept == []


def test_connection_refused_is_covered_by_transient_errors():
    """PostgreSQL 不可达抛的是 ConnectionRefusedError，必须落在真实的可重试集合里。

    它是 ConnectionError/OSError 的子类，因此以基类形式被覆盖——按子类关系判定。
    """
    assert any(issubclass(ConnectionRefusedError, item) for item in run_worker._transient_database_errors())


def test_connection_refused_is_retried(env, monkeypatch):
    """PostgreSQL 不可达（端口拒绝）属于可恢复，必须重试。"""
    clock, _logger = env
    monkeypatch.setattr(run_worker, "_transient_database_errors", lambda: (ConnectionRefusedError,))
    attempts = []

    async def refused():
        attempts.append(1)
        if len(attempts) < 2:
            raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", refused)

    asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 2
    assert len(clock.slept) == 1
