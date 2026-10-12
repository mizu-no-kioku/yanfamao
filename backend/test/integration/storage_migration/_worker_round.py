"""Step C 的 worker 轮次：可控生命周期的真实 worker 子进程 + 健康记录的**归属**证明。

## 为什么"健康键存在"不算证据

`yuxi:worker:health:*` 的键名是**全局的**（不带前缀开关），`redis-test` 又跨轮复用。键存在只说明
"某个 worker 报过到"——可能是开发栈的 worker、可能是上一轮的残留。所以本轮的判定链是：

1. 从**本轮 worker 子进程自己的日志**里取 `worker_id`（`_worker_startup` 发出的启动行，
   格式 `worker startup ready: worker_id=<id>`）；
2. 再读两个收敛健康键，要求值与该 `worker_id` **精确相等**。

这样"健康键在"才等于"**我启动的那个进程**活着"。取 ID 只从日志取，不从环境变量或我们自己的
构造里取——那样只能证明"值变过"，不能证明它来自目标子进程。

## 互斥

健康键全局、实例跨轮复用，因此**同一时刻只允许一个**使用它的测试 worker。本轮用 Redis 上的一个
租约键做互斥，拿不到就**直接失败**（不是 skip）：两个测试 worker 同时跑，断言会互相污染，
而失败会以"偶发"的面目出现——正是这一步要根除的东西。

## 前置

worker 的 `_worker_startup` 第一步是 `require_current_schema()`，要求库精确记着
`business=13 / knowledge=2`；那是迁移轮次的产物（见 `conftest.py` 的 `migrated_test_database`）。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import redis as redis_lib
from sqlalchemy.engine import make_url

from yuxi.services.run_queue_service import WORKER_RECONCILIATION_HEALTH_KEY
from yuxi.services.task_queue_service import TASK_RECONCILIATION_HEALTH_KEY

from _migration_round import QUIESCENCE_ENV_VARS, build_round_roots, new_round_root
from _tenant_schema_test_db import tenant_schema_url

REDIS_TEST_URL_ENV = "REDIS_TEST_URL"
DEVELOPMENT_REDIS_URL_ENV = "REDIS_URL"

# 两个健康键的值都是 WORKER_ID（见 run_worker 的 _publish_*_reconciliation_health）。
HEALTH_KEYS = (WORKER_RECONCILIATION_HEALTH_KEY, TASK_RECONCILIATION_HEALTH_KEY)

MUTEX_KEY = "yuxi:test:part2:worker-round-lock"
MUTEX_TTL_MS = 300_000

WORKER_ENTRY_MODULE = "server.worker_main"
STARTUP_MARKER = "Worker startup ready"
WORKER_ID_PATTERN = re.compile(r"worker_id=(worker-[0-9a-f]{32})")

_MISSING_REDIS_CONFIG = (
    "测试 worker 需要 REDIS_TEST_URL 指向独立 Redis 实例（不会回退到 REDIS_URL）。\n"
    "起法：docker compose --profile test-db up -d redis-test；"
    "改过 compose 的**环境变量**必须 `up -d` 重建容器，`restart` 不生效。"
)


def redis_test_url() -> str:
    """独立测试 Redis 的连接串；未配置直接报错，不静默回退。"""

    raw = os.environ.get(REDIS_TEST_URL_ENV, "").strip()
    if not raw:
        raise RuntimeError(_MISSING_REDIS_CONFIG)
    return raw


def _redis_target(raw: str) -> tuple[str | None, int | None, str]:
    parsed = urlparse(raw)
    return parsed.hostname, parsed.port, parsed.path or "/0"


def assert_redis_is_not_the_development_instance() -> None:
    """核对测试 Redis 与开发 Redis 不是同一个目标。不回退、不 skip。"""

    test_url = redis_test_url()
    development = os.environ.get(DEVELOPMENT_REDIS_URL_ENV, "").strip()
    if development and _redis_target(development) == _redis_target(test_url):
        parsed = urlparse(test_url)
        raise RuntimeError(
            f"REDIS_TEST_URL 与 REDIS_URL 指向同一个目标（{parsed.hostname}:{parsed.port}{parsed.path or ''}）。"
            "测试 worker 用全局健康键与默认队列，拒绝在开发 Redis 上运行。"
        )


class WorkerRoundMutex:
    """用 Redis 租约键保证同一时刻只有一个测试 worker 使用全局健康键。"""

    def __init__(self, url: str) -> None:
        self._client = redis_lib.from_url(url, socket_timeout=5, socket_connect_timeout=5)
        self._token = f"{os.getpid()}-{time.monotonic_ns()}"

    def __enter__(self) -> WorkerRoundMutex:
        acquired = self._client.set(MUTEX_KEY, self._token, nx=True, px=MUTEX_TTL_MS)
        if not acquired:
            raise RuntimeError(
                f"另一个测试 worker 正在使用 {redis_test_url()!s}（互斥键 {MUTEX_KEY} 已被占用）。"
                "健康键是全局的，两个 worker 同时跑会让断言互相污染——等它结束或清掉该键再跑。"
            )
        return self

    def __exit__(self, *_exc_info: object) -> None:
        # 只删自己的租约：ttl 到期后别人可能已经拿到，删别人的锁会造出"两个人同时持有"。
        if self._client.get(MUTEX_KEY) == self._token.encode():
            self._client.delete(MUTEX_KEY)
        self._client.close()


@dataclass
class WorkerRound:
    """一轮 worker：唯一临时根、子进程环境、日志与归属到的 worker_id。"""

    root: Path
    roots: dict[str, Path]
    child_cwd: Path
    env: dict[str, str]
    log_path: Path
    process: subprocess.Popen[str] | None = None
    lines: list[str] = field(default_factory=list)
    worker_id: str | None = None

    def log_text(self) -> str:
        return "".join(self.lines)

    def log_tail(self, count: int = 40) -> str:
        return "\n".join(self.log_text().splitlines()[-count:])


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
    """构造 worker 子进程环境：四个连接、五个文件根与导入路径全部显式指定。"""

    env = dict(os.environ)
    for name in QUIESCENCE_ENV_VARS:
        env.pop(name, None)
    env["POSTGRES_URL"] = tenant_schema_url()
    # 父进程的 POSTGRES_APP_URL / REDIS_URL 指向开发实例，必须**换掉**而不是留着继承。
    env["POSTGRES_APP_URL"] = restricted_runtime_url()
    env["REDIS_URL"] = redis_url
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(application_root()), env.get("PYTHONPATH", "")) if part
    )
    for name, path in roots.items():
        env[name] = str(path)
    return env


def start_worker_round(*, start_timeout_seconds: float = 60.0) -> WorkerRound:
    """起 worker 子进程，等它从自己的日志里报出 worker_id。"""

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

    process = subprocess.Popen(
        [sys.executable, "-m", WORKER_ENTRY_MODULE],
        cwd=str(child_cwd),
        env=round_.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        # 独立进程组：回收时整组一起收，不留孤儿。
        start_new_session=True,
    )
    round_.process = process
    _pump_lines(round_, deadline=time.monotonic() + start_timeout_seconds)
    round_.log_path.write_text(round_.log_text(), encoding="utf-8")
    return round_


def _pump_lines(round_: WorkerRound, *, deadline: float) -> None:
    """把子进程输出读进内存，直到看到启动行、进程退出或超时。"""

    lock = threading.Lock()

    def reader() -> None:
        stream = round_.process.stdout
        if stream is None:
            return
        for line in stream:
            with lock:
                round_.lines.append(line)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    while time.monotonic() < deadline:
        with lock:
            found = _extract_worker_id(round_.lines)
        if found is not None:
            round_.worker_id = found
            return
        if round_.process.poll() is not None:
            time.sleep(0.2)  # 让 reader 把剩余输出读完
            with lock:
                round_.worker_id = _extract_worker_id(round_.lines)
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
    """读两个收敛健康键当前的值。"""

    client = redis_lib.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
    try:
        return {key: client.get(key) for key in HEALTH_KEYS}
    finally:
        client.close()


def stop_worker_round(round_: WorkerRound) -> None:
    """整组回收；成功清理本轮资源，失败保留日志与临时产物。"""

    process = round_.process
    if process is not None and process.poll() is None:
        _terminate_process_group(process)

    succeeded = round_.worker_id is not None and process is not None and process.returncode == 0
    if succeeded:
        shutil.rmtree(round_.root, ignore_errors=True)
        return
    print(f"[part2] 本轮 worker 未正常退出（returncode={None if process is None else process.returncode}）")
    print(f"[part2] 保留临时根：{round_.root}")
    print(round_.log_tail())


def _terminate_process_group(process: subprocess.Popen[str], *, grace_seconds: float = 20.0) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=10)
