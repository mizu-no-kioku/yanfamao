"""Step C 的 worker 轮次：可控生命周期的真实 worker 子进程 + 健康记录的**归属**证明。

## 为什么"健康键存在"不算证据

`yuxi:worker:health:*` 的键名是**全局的**（不带前缀开关），`redis-test` 又跨轮复用。键存在只说明
"某个 worker 报过到"——可能是开发栈的 worker、可能是上一轮的残留。所以本轮的判定链是：

1. 从**本轮 worker 子进程自己的日志**里取 `worker_id`（`_worker_startup` 发出的启动行，
   格式 `Worker startup ready: worker_id=<id>`）；
2. 再读两个收敛健康键，要求值与该 `worker_id` **精确相等**。

这样"健康键在"才等于"**我启动的那个进程**活着"。取 ID 只从日志取，不从环境变量或我们自己的
构造里取——那样只能证明"值变过"，不能证明它来自目标子进程。

## 顺序：守卫必须在**任何 Redis 写入之前**

`start_worker_round()` 第一件事就是跑防误连守卫，之后才可能删除健康键、起子进程。守卫没接线
过一次：它只被负向测试调用，真实启动路径直接连库删键——那样"负向测试通过"完全不等于"误配会被拒绝"。

## 互斥

健康键全局、实例跨轮复用，因此**同一时刻只允许一个**使用它的测试 worker。本轮用 Redis 上的一个
租约键做互斥：拿不到**直接失败**（不是 skip）；租约**自动续期**，续期失败会让本轮**立即终止**；
释放用原子 compare-and-delete，不会删掉别人的新租约。

## 隔离边界（不要夸大）

显式指定的只有：四个连接（Postgres owner / Postgres 受限角色 / Redis / 导入路径）与五个文件根。
沙箱 provisioner 被指向**拒绝外联**的测试端点，因此真走到沙箱会立刻失败而不是打到开发 provisioner。
其它外部依赖（Neo4j / Milvus / MinIO）**没有**隔离——本轮不投递任何 job，所以不作相关声明。

## 前置

worker 的 `_worker_startup` 第一步是 `require_current_schema()`，要求库精确记着
`business=13 / knowledge=2`；那是迁移轮次的产物（见 `conftest.py` 的 `migrated_test_database`）。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import redis as redis_lib
from sqlalchemy.engine import make_url

from yuxi.services.run_queue_service import WORKER_RECONCILIATION_HEALTH_KEY
from yuxi.services.task_queue_service import TASK_RECONCILIATION_HEALTH_KEY

from _migration_round import QUIESCENCE_ENV_VARS, build_round_roots, new_round_root
from _round_process import CapturedProcess, spawn_captured
from _tenant_schema_test_db import tenant_schema_url

REDIS_TEST_URL_ENV = "REDIS_TEST_URL"
DEVELOPMENT_REDIS_URL_ENV = "REDIS_URL"

DEFAULT_REDIS_PORT = 6379
DEFAULT_REDIS_DB = 0

# 两个健康键的值都是 WORKER_ID（见 run_worker 的 _publish_*_reconciliation_health）。
HEALTH_KEYS = (WORKER_RECONCILIATION_HEALTH_KEY, TASK_RECONCILIATION_HEALTH_KEY)

MUTEX_KEY = "yuxi:test:part2:worker-round-lock"
MUTEX_TTL_MS = 300_000

WORKER_ENTRY_MODULE = "server.worker_main"
STARTUP_MARKER = "Worker startup ready"
WORKER_ID_PATTERN = re.compile(r"worker_id=(worker-[0-9a-f]{32})")

# 拒绝外联的测试端点：真的走到沙箱时**立刻失败**，而不是打到开发 provisioner 上。
# 令牌换成占位值（长度必须过 `require_security_secrets` 的 ≥32 校验），这样开发凭证
# 根本不进子进程环境。
SANDBOX_ISOLATION_OVERRIDES = {
    "SANDBOX_PROVISIONER_URL": "http://127.0.0.1:1",
    "SANDBOX_PROVISIONER_TOKEN": "part2-round-refuses-outbound-not-a-real-token",
}

_MISSING_REDIS_CONFIG = (
    "测试 worker 需要 REDIS_TEST_URL 指向独立 Redis 实例（不会回退到 REDIS_URL）。\n"
    "起法：docker compose --profile test-db up -d redis-test；"
    "改过 compose 的**环境变量**必须 `up -d` 重建容器，`restart` 不生效。"
)

# 释放/续期都必须是原子的 compare-and-X：先 GET 后 DEL 之间租约可能过期并被别人拿到，
# 那时 DEL 删掉的是**新持有者**的锁。
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

def redis_test_url() -> str:
    """独立测试 Redis 的连接串；未配置直接报错，不静默回退。"""

    raw = os.environ.get(REDIS_TEST_URL_ENV, "").strip()
    if not raw:
        raise RuntimeError(_MISSING_REDIS_CONFIG)
    return raw


def _redis_target(raw: str) -> tuple[str | None, int, int]:
    """归一化目标：缺省端口与库号都补上默认值，否则 `redis://h/0` 与 `redis://h:6379/0` 会被当成两个目标。"""

    parsed = urlparse(raw)
    if parsed.scheme not in {"redis", "rediss"}:
        raise RuntimeError(f"不认识的 Redis 连接串 scheme：{parsed.scheme!r}（只支持 redis:// 与 rediss://）")
    path = (parsed.path or "").lstrip("/")
    return parsed.hostname, parsed.port or DEFAULT_REDIS_PORT, int(path) if path else DEFAULT_REDIS_DB


def describe_redis(raw: str) -> str:
    """只暴露 host/port/db——这个字符串会进异常与 pytest 日志，不能带凭证。"""

    host, port, db = _redis_target(raw)
    return f"{host}:{port}/{db}"


def redis_run_id(url: str) -> str:
    """读实例身份（`INFO server` 的 run_id）。只读，不写。"""

    client = redis_lib.from_url(url, socket_timeout=5, socket_connect_timeout=5)
    try:
        run_id = client.info("server").get("run_id")
    finally:
        client.close()
    if not run_id:
        raise RuntimeError(f"读不到 Redis 实例身份（run_id）：{describe_redis(url)}")
    return str(run_id)


def assert_redis_is_not_the_development_instance() -> None:
    """核对测试 Redis 确实是独立实例。不回退、不 skip、**不缓存**。

    两道：① 归一化后的 (host, port, db) 不同；② 实例身份 `run_id` 不同。"目标串不同"挡不住
    **连接别名**（hosts / DNS / 端口转发指向同一实例），`run_id` 是实例自己的随机身份，能挡住。
    读开发的 `run_id` 是一次只读 INFO——守卫要证明的正是"这两个不是同一个"，不连过去就无法证明。

    **不缓存**：任何只缓存部分输入的缓存都有绕过路径——只记测试 URL 的话，"测试 URL 不变、
    `REDIS_URL` 被换成同一目标"就会直接跳过核对。两次只读 INFO 的成本远低于绕过的代价。
    """

    test_url = redis_test_url()
    development = os.environ.get(DEVELOPMENT_REDIS_URL_ENV, "").strip()
    if not development:
        raise RuntimeError(
            "无法核对测试 Redis：REDIS_URL 未配置，就没有可比对的对象，也就无法证明两份配置不是同一实例。"
        )

    if _redis_target(development) == _redis_target(test_url):
        raise RuntimeError(
            f"REDIS_TEST_URL 与 REDIS_URL 指向同一个目标（{describe_redis(test_url)}）。"
            "测试 worker 用全局健康键与默认队列，拒绝在开发 Redis 上运行。"
        )

    if redis_run_id(test_url) == redis_run_id(development):
        raise RuntimeError(
            f"REDIS_TEST_URL 与 REDIS_URL 连到同一个 Redis 实例（run_id 相同，目标 {describe_redis(test_url)}）。"
            "目标串不同但实例相同——别名 hosts/DNS/端口转发，同样会污染开发队列。"
        )


class WorkerRoundMutex:
    """Redis 租约键：保证同一时刻只有一个测试 worker 使用全局健康键。"""

    def __init__(self, url: str, *, ttl_ms: int = MUTEX_TTL_MS, renew_interval_seconds: float | None = None) -> None:
        self._url = url
        self._client = redis_lib.from_url(url, socket_timeout=5, socket_connect_timeout=5)
        self._token = f"{os.getpid()}-{time.monotonic_ns()}"
        self._ttl_ms = ttl_ms
        self._renew_interval = renew_interval_seconds or max(ttl_ms / 3000, 0.05)
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._renewer: threading.Thread | None = None

    def __enter__(self) -> WorkerRoundMutex:
        if not self._client.set(MUTEX_KEY, self._token, nx=True, px=self._ttl_ms):
            raise RuntimeError(
                f"另一个测试 worker 正在使用 {describe_redis(self._url)}（互斥键 {MUTEX_KEY} 已被占用）。"
                "健康键是全局的，两个 worker 同时跑会让断言互相污染——等它结束或清掉该键再跑。"
            )
        self._renewer = threading.Thread(target=self._renew_forever, daemon=True)
        self._renewer.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._stop.set()
        if self._renewer is not None:
            self._renewer.join(timeout=2)
        try:
            self._client.eval(_RELEASE_SCRIPT, 1, MUTEX_KEY, self._token)
        finally:
            self.close()

    def close(self) -> None:
        self._client.close()

    def check(self) -> None:
        """本轮是否仍持有租约。丢锁就必须终止——否则两个 worker 会同时持有全局健康键。

        不只信续期线程的标记，还直接**问一次 Redis**：键被删/被别人抢走而续期线程尚未醒来时，
        只看标记会漏判。
        """

        if self._lost.is_set():
            raise RuntimeError(
                f"互斥租约已失效（{describe_redis(self._url)}）——本轮必须终止，不能继续跑。"
            )
        try:
            held = self._client.get(MUTEX_KEY)
        except Exception as exc:
            raise RuntimeError(
                f"无法核对互斥租约（{describe_redis(self._url)}）：{type(exc).__name__}——本轮必须终止。"
            ) from exc
        if held != self._token.encode():
            self._lost.set()
            raise RuntimeError(
                f"互斥租约已不再属于本轮（{describe_redis(self._url)}）——本轮必须终止，不能继续跑。"
            )

    def _renew_forever(self) -> None:
        while not self._stop.wait(self._renew_interval):
            try:
                renewed = self._client.eval(_RENEW_SCRIPT, 1, MUTEX_KEY, self._token, self._ttl_ms)
            except Exception:
                self._lost.set()
                return
            if not renewed:
                self._lost.set()
                return


@dataclass
class WorkerRound:
    """一轮 worker：唯一临时根、子进程环境、日志与归属到的 worker_id。"""

    root: Path
    roots: dict[str, Path]
    child_cwd: Path
    env: dict[str, str]
    log_path: Path
    captured: CapturedProcess | None = None
    worker_id: str | None = None
    lease_lost: bool = False
    watchdog_stop: threading.Event | None = None
    watchdog: threading.Thread | None = None

    @property
    def returncode(self) -> int | None:
        return None if self.captured is None else self.captured.process.returncode

    def log_text(self) -> str:
        return "" if self.captured is None else self.captured.log_text()

    def log_tail(self, count: int = 40) -> str:
        return "" if self.captured is None else self.captured.log_tail(count)


def restricted_runtime_url() -> str:
    """测试实例上的受限角色连接串（运行时连接走它，RLS 才有效）。"""

    url = make_url(tenant_schema_url())
    password = os.environ.get("POSTGRES_APP_PASSWORD", "postgres")
    return url.set(username="yuxi_app", password=password).render_as_string(hide_password=False)


def application_root() -> Path:
    """装着 `server/` 与 `package/` 的应用根。

    入口 `server.worker_main` 是**源码目录**而不是安装进 venv 的包：`yuxi` 无论 cwd 在哪都能导入，
    `server` 只有在应用根可见时才行。诱饵 cwd 会把这件事暴露成 `ModuleNotFoundError`，所以这里
    显式把应用根放进 `PYTHONPATH`，而不是把 cwd 挪回去。
    """

    for parent in Path(__file__).resolve().parents:
        if (parent / "server").is_dir() and (parent / "package").is_dir():
            return parent
    raise RuntimeError("找不到应用根（同时含 server/ 与 package/ 的目录）")


def build_worker_env(roots: dict[str, Path], *, redis_url: str) -> dict[str, str]:
    """构造 worker 子进程环境：四个连接、五个文件根、导入路径与沙箱端点全部显式指定。"""

    env = dict(os.environ)
    for name in QUIESCENCE_ENV_VARS:
        env.pop(name, None)
    env["POSTGRES_URL"] = tenant_schema_url()
    # 父进程的 POSTGRES_APP_URL / REDIS_URL / 沙箱地址指向开发实例，必须**换掉**而不是留着继承。
    env["POSTGRES_APP_URL"] = restricted_runtime_url()
    env["REDIS_URL"] = redis_url
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(application_root()), env.get("PYTHONPATH", "")) if part
    )
    env.update(SANDBOX_ISOLATION_OVERRIDES)
    for name, path in roots.items():
        env[name] = str(path)
    return env


def must_keep_artifacts(round_: WorkerRound) -> bool:
    """worker 没起来、没报出身份、非 0 退出、或中途丢了租约 → 一定保留产物。"""

    if round_.lease_lost:
        return True
    return round_.captured is None or round_.worker_id is None or round_.returncode != 0


def start_worker_round(
    *,
    mutex: WorkerRoundMutex,
    start_timeout_seconds: float = 60.0,
) -> WorkerRound:
    """起 worker 子进程，等它从自己的日志里报出 worker_id。

    顺序是有意的：**守卫** → **确认自己真的持有租约** → 才允许删健康键、起子进程。
    `mutex` 是**必填**参数：健康键是全局的，靠"调用方记得拿锁"的约定挡不住第二条启动路径。
    """

    assert_redis_is_not_the_development_instance()
    mutex.check()

    root = new_round_root()
    roots, child_cwd = build_round_roots(root)
    redis_url = redis_test_url()
    round_ = WorkerRound(
        root=root,
        roots=roots,
        child_cwd=child_cwd,
        env=build_worker_env(roots, redis_url=redis_url),
        log_path=root / "worker.log",
    )

    # 清掉上一轮可能留下的健康键：否则"键在"可能来自别的进程，而我们要的是归属证明。
    client = redis_lib.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
    try:
        client.delete(*HEALTH_KEYS)
    finally:
        client.close()

    round_.captured = spawn_captured(
        [sys.executable, "-m", WORKER_ENTRY_MODULE],
        cwd=child_cwd,
        env=round_.env,
        log_path=round_.log_path,
    )
    _start_lease_watchdog(round_, mutex)
    try:
        _pump_lines(round_, deadline=time.monotonic() + start_timeout_seconds)
    except BaseException:
        # 起完就抛（含丢锁）时不能把子进程留在后台。
        round_.captured.reap()
        raise
    return round_


def _start_lease_watchdog(round_: WorkerRound, mutex: WorkerRoundMutex, *, interval: float = 0.2) -> None:
    """轮次**整个生命周期**内持续核对租约；丢锁就回收整组并把本轮判失败。

    只等启动日志时核对是不够的：worker 就绪之后如果没人再看一眼，丢锁会静默地继续用全局健康键
    （两个 worker 同时活着却都以为自己是唯一的）——这正是要根除的东西。
    """

    stop = threading.Event()
    round_.watchdog_stop = stop

    def watchdog() -> None:
        while not stop.wait(interval):
            try:
                mutex.check()
            except BaseException:
                round_.lease_lost = True
                if round_.captured is not None:
                    round_.captured.reap()
                return

    round_.watchdog = threading.Thread(target=watchdog, daemon=True)
    round_.watchdog.start()


def _stop_lease_watchdog(round_: WorkerRound) -> None:
    if round_.watchdog_stop is not None:
        round_.watchdog_stop.set()
    if round_.watchdog is not None:
        round_.watchdog.join(timeout=5)


def _pump_lines(round_: WorkerRound, *, deadline: float) -> None:
    """等启动行。丢锁由看门狗负责（它会回收整组并置 `lease_lost`），这里只看进程与时间。"""

    captured = round_.captured
    assert captured is not None

    while True:
        found = _extract_worker_id(captured.lines())
        if found is not None:
            round_.worker_id = found
            return
        if captured.process.poll() is not None:
            time.sleep(0.2)  # 让读线程把剩余输出读完
            round_.worker_id = _extract_worker_id(captured.lines())
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(0.2)


def _extract_worker_id(lines: list[str]) -> str | None:
    for line in lines:
        if STARTUP_MARKER not in line:
            continue
        match = WORKER_ID_PATTERN.search(line)
        if match:
            return match.group(1)
    return None


def health_record_values(redis_url: str) -> dict[str, str | None]:
    """读两个收敛健康键当前的值（`None` 表示键不存在/已过期）。"""

    client = redis_lib.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
    try:
        return {key: None if (value := client.get(key)) is None else value.decode() for key in HEALTH_KEYS}
    finally:
        client.close()


def stop_worker_round(round_: WorkerRound, *, keep_artifacts: bool) -> None:
    """整组回收；`keep_artifacts` 为真时保留日志与临时产物并打印路径与日志尾部。"""

    _stop_lease_watchdog(round_)
    if round_.captured is not None:
        round_.captured.reap()

    if not keep_artifacts:
        shutil.rmtree(round_.root, ignore_errors=True)
        return
    print(f"[part2] 本轮 worker 产物保留（returncode={round_.returncode}，worker_id={round_.worker_id}）")
    print(f"[part2] 临时根：{round_.root}")
    print(round_.log_tail())


