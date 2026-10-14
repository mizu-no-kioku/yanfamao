"""worker 启动等待 PostgreSQL 就绪：有上限、可观测、正常路径不被拖慢。

分类必须按生产真实异常判定。SQLAlchemy 引擎抛出的**不是** asyncpg 异常本身：
方言先把 asyncpg 异常翻译成 `AsyncAdapt_asyncpg_dbapi.*`，引擎再用
`DBAPIError.instance` 包装成 `sqlalchemy.exc.*`。因此本文件的用例通过
`sqlalchemy_wrapped()` 复刻这两步真实包装，而不是向被测函数直接抛裸 asyncpg 异常
（那种「抛什么就捕什么」的循环自证无法发现生产路径上的漏判）。

项目 logger 是 loguru，不进入 stdlib logging，因此这里直接替换 worker 模块的
logger 为记录桩，按 loguru 的 `{}` 风格格式化后断言——比 caplog 更直接。
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import DBAPIError
from yuxi.services import run_worker

_DBAPI = AsyncAdapt_asyncpg_dbapi(asyncpg)


def sqlalchemy_wrapped(asyncpg_error: BaseException) -> BaseException:
    """按 SQLAlchemy asyncpg 方言的真实路径把 asyncpg 异常包装成引擎会抛出的异常。

    与 `AsyncAdapt_asyncpg_dbapi._handle_exception` + `DBAPIError.instance` 等价：
    先按 MRO 命中方言翻译表得到 `AsyncAdapt_asyncpg_dbapi.*`，再按 MRO 名称查找
    包装成 `sqlalchemy.exc.*`。被测函数看到的就是生产里真实抛出的类型。
    """
    translate = _DBAPI._asyncpg_error_translate
    translated = None
    for super_ in type(asyncpg_error).__mro__:
        if super_ in translate:
            translated = translate[super_](f"{type(asyncpg_error)}: {asyncpg_error}")
            break
    assert translated is not None, f"asyncpg 异常未被方言翻译：{type(asyncpg_error)}"
    translated.__cause__ = asyncpg_error
    wrapped = DBAPIError.instance("SELECT 1", None, translated, _DBAPI.Error)
    assert isinstance(wrapped, DBAPIError), f"未按预期包装为 DBAPIError：{wrapped!r}"
    return wrapped


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


@pytest.fixture
def env(monkeypatch):
    """提供假时钟与记录型 logger，并清掉可能影响结果的环境变量。

    刻意**不**替换分类函数：分类必须由被测代码的真实实现决定，
    否则测试只是把断言抄了一遍实现。
    """
    clock = FakeClock()
    logger = RecordingLogger()
    monkeypatch.setattr(run_worker.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(run_worker.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(run_worker, "logger", logger)
    monkeypatch.delenv("YUXI_WORKER_DB_READY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("YUXI_WORKER_DB_READY_INTERVAL_SECONDS", raising=False)
    return clock, logger


def _raiser(exc: BaseException, attempts: list[int], *, fail_times: int = 10**9):
    """返回一个前 `fail_times` 次抛 `exc` 的 async 桩。"""

    async def call() -> None:
        attempts.append(1)
        if len(attempts) <= fail_times:
            raise exc

    return call


# --- 分类：真实 SQLAlchemy 包装链 ---


@pytest.mark.parametrize(
    ("exc_name", "label"),
    [
        ("CannotConnectNowError", "数据库正在启动"),
        ("ConnectionDoesNotExistError", "连接被中断（含 pre-ping 复用失效连接）"),
        ("AdminShutdownError", "数据库正在关闭"),
        ("CrashShutdownError", "数据库崩溃重启中"),
        ("TooManyConnectionsError", "连接数已满"),
    ],
)
def test_wrapped_transient_errors_are_classified_transient(exc_name: str, label: str) -> None:
    """这些经 SQLAlchemy 包装后仍必须被判为可重试。"""
    raw = getattr(asyncpg.exceptions, exc_name)("simulated")
    wrapped = sqlalchemy_wrapped(raw)

    assert run_worker._classify_database_error(wrapped) == "transient", label


@pytest.mark.parametrize(
    ("exc_name", "label"),
    [
        ("InvalidPasswordError", "密码错误"),
        ("InvalidCatalogNameError", "库名不存在"),
        ("InvalidSchemaNameError", "Schema 不存在"),
        ("UndefinedTableError", "表不存在"),
        ("UndefinedColumnError", "字段不存在"),
        ("InsufficientPrivilegeError", "权限不足"),
    ],
)
def test_wrapped_permanent_errors_are_classified_permanent(exc_name: str, label: str) -> None:
    """配置、认证与 Schema 类错误经包装后必须判为永久，不得重试。"""
    raw = getattr(asyncpg.exceptions, exc_name)("simulated")
    wrapped = sqlalchemy_wrapped(raw)

    assert run_worker._classify_database_error(wrapped) == "permanent", label


def test_outer_exception_type_alone_would_misclassify() -> None:
    """只按最外层类型判定会漏判——这正是修复前的缺陷。

    引擎把「连接已失效」包成泛化的 `DBAPIError`，它既不是
    `asyncpg.PostgresConnectionError` 也不是 `sqlalchemy.exc.OperationalError`，
    因此旧实现会把它当作未知错误直接失败，无法在 PostgreSQL 重启后自动恢复。
    """
    wrapped = sqlalchemy_wrapped(asyncpg.exceptions.ConnectionDoesNotExistError("gone"))

    assert type(wrapped) is DBAPIError
    assert not isinstance(wrapped, asyncpg.exceptions.PostgresConnectionError)
    assert not isinstance(wrapped, run_worker.OperationalError)
    assert run_worker._is_transient_database_error(wrapped), "必须沿 orig/cause 链判定为可重试"


def test_wrong_password_wrapped_does_not_inherit_postgres_connection_error() -> None:
    """文档化一个反直觉事实：密码错误并不是 `PostgresConnectionError`。

    它继承自 `PostgresError`，因此任何「按 PostgresConnectionError 粗判」的写法
    都会把密码错误误判成可重试，把部署错误拖到超时。
    """
    assert not issubclass(asyncpg.exceptions.InvalidPasswordError, asyncpg.exceptions.PostgresConnectionError)
    assert not issubclass(asyncpg.exceptions.InvalidCatalogNameError, asyncpg.exceptions.PostgresConnectionError)
    assert not issubclass(asyncpg.exceptions.CannotConnectNowError, asyncpg.exceptions.PostgresConnectionError)


def test_unknown_exception_is_not_transient() -> None:
    assert run_worker._classify_database_error(ValueError("schema version mismatch")) == "unknown"
    assert not run_worker._is_transient_database_error(ValueError("schema version mismatch"))


# --- 启动等待：行为 ---


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
    """数据库正在启动（真实包装链）→ 有界重试 → 恢复后自动继续，并留下可观测记录。"""
    clock, logger = env
    attempts: list[int] = []
    wrapped = sqlalchemy_wrapped(asyncpg.exceptions.CannotConnectNowError("the database system is starting up"))
    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", _raiser(wrapped, attempts, fail_times=2))

    asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 3
    assert len(clock.slept) == 2, "前两次失败应各等待一次"
    assert any("暂不可用" in w for w in logger.warnings), "重试过程必须可见"
    assert any("已就绪" in w for w in logger.warnings), "恢复必须有明确记录"


def test_stale_pooled_connection_is_retried(env, monkeypatch):
    """pre-ping 发现连接已失效（包装成泛化 DBAPIError）属于可恢复，必须重试。"""
    clock, _logger = env
    attempts: list[int] = []
    wrapped = sqlalchemy_wrapped(asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed"))
    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", _raiser(wrapped, attempts, fail_times=1))

    asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 2
    assert len(clock.slept) == 1


def test_raw_connection_refused_is_retried(env, monkeypatch):
    """PostgreSQL 不可达（端口拒绝）在连上之前就抛出，未经包装，属于可恢复。"""
    clock, _logger = env
    attempts: list[int] = []
    monkeypatch.setattr(
        run_worker.pg_manager,
        "require_current_schema",
        _raiser(ConnectionRefusedError("connection refused"), attempts, fail_times=1),
    )

    asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 2
    assert len(clock.slept) == 1


def test_timeout_raises_with_clear_error(env, monkeypatch):
    """超过上限仍不可用 → 放弃启动，日志点明上限、次数与错误类型。"""
    clock, logger = env
    monkeypatch.setenv("YUXI_WORKER_DB_READY_TIMEOUT_SECONDS", "5")
    wrapped = sqlalchemy_wrapped(asyncpg.exceptions.CannotConnectNowError("the database system is starting up"))
    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", _raiser(wrapped, []))

    with pytest.raises(DBAPIError):
        asyncio.run(run_worker._await_database_ready())

    assert any("未在 5 秒内就绪" in e and "attempts=" in e for e in logger.errors), logger.errors
    assert clock.now <= 5, "重试总时长必须受上限约束"


def test_non_transient_error_fails_fast(env, monkeypatch):
    """非连接类错误立即抛出，不被重试拖到超时——真问题必须立刻可见。"""
    clock, logger = env
    attempts = []

    async def broken():
        attempts.append(1)
        raise ValueError("schema version mismatch")

    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", broken)

    with pytest.raises(ValueError):
        asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 1
    assert clock.slept == []
    assert any("classification=unknown" in e for e in logger.errors), logger.errors


def test_wrapped_wrong_password_fails_immediately(env, monkeypatch):
    """密码错误经真实 SQLAlchemy 包装后必须立刻失败，不得重试到超时。"""
    clock, logger = env
    monkeypatch.setenv("YUXI_WORKER_DB_READY_TIMEOUT_SECONDS", "300")
    attempts: list[int] = []
    wrapped = sqlalchemy_wrapped(asyncpg.exceptions.InvalidPasswordError("password authentication failed"))
    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", _raiser(wrapped, attempts))

    with pytest.raises(DBAPIError):
        asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 1, f"永久性错误只应尝试一次，实际 {len(attempts)} 次"
    assert clock.slept == [], "永久性错误不得进入等待"
    assert any("classification=permanent" in e for e in logger.errors), logger.errors


def test_wrapped_nonexistent_database_fails_immediately(env, monkeypatch):
    """库名不存在经真实 SQLAlchemy 包装后必须立刻失败。"""
    clock, _logger = env
    monkeypatch.setenv("YUXI_WORKER_DB_READY_TIMEOUT_SECONDS", "300")
    attempts: list[int] = []
    wrapped = sqlalchemy_wrapped(asyncpg.exceptions.InvalidCatalogNameError('database "nonexistent" does not exist'))
    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", _raiser(wrapped, attempts))

    with pytest.raises(DBAPIError):
        asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 1
    assert clock.slept == []


def test_wrapped_schema_error_fails_immediately(env, monkeypatch):
    """Schema 不符（表不存在）同样立即失败，不重试。"""
    clock, _logger = env
    monkeypatch.setenv("YUXI_WORKER_DB_READY_TIMEOUT_SECONDS", "300")
    attempts: list[int] = []
    wrapped = sqlalchemy_wrapped(asyncpg.exceptions.UndefinedTableError('relation "schema_version" does not exist'))
    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", _raiser(wrapped, attempts))

    with pytest.raises(DBAPIError):
        asyncio.run(run_worker._await_database_ready())

    assert len(attempts) == 1
    assert clock.slept == []
