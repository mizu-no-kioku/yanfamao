"""脚手架自身的验收：守卫接线、凭证不泄漏、租约原子性、整组回收、产物保留判定。

这些用例**不需要**迁移或 worker 轮次（也就不会改测试实例），只需要容器内的
`REDIS_TEST_URL` / `REDIS_URL`。它们存在的理由，正是审查指出的那几处：

- 守卫曾经只在负向测试里被调用，真实启动路径根本没接线——所以"负向测试通过"毫无意义；
- 互斥失败信息里带了完整连接串（含密码）；
- 租约释放是 GET 后 DEL，会把别人的新租约删掉，且没有续期；
- 产物保留原先只看退出码，断言失败时日志照样被删；
- 丢锁原先只在"等启动日志"时被核对，worker 就绪后没人再看——所以这里有一条**真实场景**：
  起真 worker、就绪后让租约失效、断言整组被回收且本轮被判失败；
- 互斥原先只是可选前置，第二条启动路径仍能删健康键、起第二个 worker——现在 `mutex` 是必填参数。

`test_losing_the_lease_...` 需要 `migrated_test_database`（worker 的启动闸门要求 13/2），
它是本文件里唯一会起真实 worker 的用例；其余用例不碰测试库。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
import redis as redis_lib

import _migration_round as migration_round_module
import _worker_round as worker_round_module
from _round_process import CapturedProcess, spawn_captured
from _worker_round import HEALTH_KEYS, WorkerRoundMutex

pytestmark = [pytest.mark.integration]


def _redis_client(url: str) -> redis_lib.Redis:
    return redis_lib.from_url(url, socket_timeout=5, socket_connect_timeout=5)


# ---------------------------------------------------------------- 守卫：接线与判定


def test_starting_a_worker_refuses_a_development_redis_before_touching_it(monkeypatch) -> None:
    """守卫必须在**删除健康键之前**生效：误配时不得有 Redis 写入，也不得起子进程。"""

    url = worker_round_module.redis_test_url()
    client = _redis_client(url)
    # 只构造、**不获取**：获取锁本身就是一次 Redis 写，会把这条用例的前提弄脏。
    mutex = WorkerRoundMutex(url)
    planted = {key: b"planted-by-the-test" for key in HEALTH_KEYS}
    try:
        for key, value in planted.items():
            client.set(key, value)
        monkeypatch.setenv(
            worker_round_module.REDIS_TEST_URL_ENV,
            os.environ[worker_round_module.DEVELOPMENT_REDIS_URL_ENV],
        )

        with pytest.raises(RuntimeError, match="指向同一个目标"):
            worker_round_module.start_worker_round(mutex=mutex)

        survivors = {key: client.get(key) for key in HEALTH_KEYS}
    finally:
        for key in HEALTH_KEYS:
            client.delete(key)
        client.close()
        mutex.close()
    assert survivors == planted, "守卫放行前就动了 Redis（健康键被删/改写）"


def test_starting_a_worker_requires_holding_the_round_lease() -> None:
    """`mutex` 是必填参数：靠"调用方记得拿锁"的约定挡不住第二条启动路径。"""

    with pytest.raises(TypeError):
        worker_round_module.start_worker_round()


def test_starting_a_worker_refuses_a_lease_that_is_not_held() -> None:
    """传了一个**没有真正持有**的租约也必须拒绝——否则等于没锁。"""

    url = worker_round_module.redis_test_url()
    mutex = WorkerRoundMutex(url)
    try:
        with pytest.raises(RuntimeError, match="不再属于本轮"):
            worker_round_module.start_worker_round(mutex=mutex)
    finally:
        mutex.close()


def test_a_default_port_alias_is_rejected(monkeypatch) -> None:
    """`redis://h/0` 与 `redis://h:6379/0` 是同一个目标——字符串不等，归一化后必须相等。"""

    monkeypatch.setenv(worker_round_module.DEVELOPMENT_REDIS_URL_ENV, "redis://shared-host:6379/0")
    monkeypatch.setenv(worker_round_module.REDIS_TEST_URL_ENV, "redis://shared-host/0")
    with pytest.raises(RuntimeError, match="指向同一个目标"):
        worker_round_module.assert_redis_is_not_the_development_instance()


def test_a_different_target_string_with_the_same_instance_is_rejected(monkeypatch) -> None:
    """目标串不同但实例相同（别名 hosts/DNS/端口转发）也必须拒绝——靠实例身份 run_id 判定。"""

    monkeypatch.setenv(worker_round_module.DEVELOPMENT_REDIS_URL_ENV, "redis://dev-alias:6379/0")
    monkeypatch.setenv(worker_round_module.REDIS_TEST_URL_ENV, "redis://redis-test:6379/0")
    monkeypatch.setattr(worker_round_module, "redis_run_id", lambda _url: "same-instance-run-id")
    with pytest.raises(RuntimeError, match="run_id 相同"):
        worker_round_module.assert_redis_is_not_the_development_instance()


def test_a_missing_test_redis_url_is_a_hard_failure(monkeypatch) -> None:
    """缺配置一律硬失败：skip 会让"忘了配"看起来像"跑过了"。"""

    monkeypatch.delenv(worker_round_module.REDIS_TEST_URL_ENV, raising=False)
    with pytest.raises(RuntimeError, match="需要 REDIS_TEST_URL"):
        worker_round_module.assert_redis_is_not_the_development_instance()


def test_the_guard_messages_never_leak_the_redis_credentials(monkeypatch) -> None:
    """守卫与互斥的报错都会进 pytest 日志，绝不能带凭证。

    本机的 `REDIS_TEST_URL` 是**不带密码**的，所以这里主动把凭证注入到 URL 再触发报错——
    否则换个无密码配置，这条用例就等于什么都没验。
    """

    url = worker_round_module.redis_test_url()
    parsed = urlparse(url)
    assert parsed.hostname, "测试 Redis URL 没有主机名，这条用例覆盖不到任何东西"
    secret = "s3cr3t-redis-password"
    credential = f"someone:{secret}"
    credentialed = f"redis://{credential}@{parsed.hostname}:{parsed.port}{parsed.path}"

    # ① 守卫：两个变量指向同一目标（带凭证的别名）→ 报错里不能出现凭证。
    monkeypatch.setenv(worker_round_module.DEVELOPMENT_REDIS_URL_ENV, credentialed)
    monkeypatch.setenv(worker_round_module.REDIS_TEST_URL_ENV, credentialed)
    with pytest.raises(RuntimeError) as guard_error:
        worker_round_module.assert_redis_is_not_the_development_instance()
    assert secret not in str(guard_error.value)
    assert credential not in str(guard_error.value)

    # ② 互斥：报错用 URL 描述拼出来，描述本身必须先脱敏。
    assert secret not in worker_round_module.describe_redis(credentialed)
    real_client = _redis_client(url)
    try:
        real_client.set(worker_round_module.MUTEX_KEY, b"held-by-another")
        monkeypatch.setattr(worker_round_module.redis_lib, "from_url", lambda *_args, **_kwargs: real_client)
        with pytest.raises(RuntimeError) as mutex_error:
            with WorkerRoundMutex(credentialed):
                pass
        assert secret not in str(mutex_error.value)
        assert credential not in str(mutex_error.value)
    finally:
        real_client.delete(worker_round_module.MUTEX_KEY)
        real_client.close()


# ---------------------------------------------------------------- 租约：原子释放与续期


def test_releasing_an_old_lease_does_not_delete_a_newer_one() -> None:
    """旧持有者释放时不能删掉新租约——先 GET 后 DEL 之间租约可能已经易主。"""

    url = worker_round_module.redis_test_url()
    client = _redis_client(url)
    try:
        old = WorkerRoundMutex(url)
        old.__enter__()
        # 模拟"租约到期、新轮次拿到锁"：直接换掉键的持有者。
        client.set(worker_round_module.MUTEX_KEY, b"newer-holder")
        old.__exit__(None, None, None)
        assert client.get(worker_round_module.MUTEX_KEY) == b"newer-holder", "旧持有者删掉了新租约"
    finally:
        client.delete(worker_round_module.MUTEX_KEY)
        client.close()


def test_the_lease_stays_exclusive_past_its_initial_ttl() -> None:
    """租约自动续期：超过初始 TTL 之后仍然排他（否则长轮次会被第二个 worker 挤进来）。"""

    url = worker_round_module.redis_test_url()
    ttl_ms = 400
    with WorkerRoundMutex(url, ttl_ms=ttl_ms, renew_interval_seconds=0.1) as mutex:
        time.sleep(ttl_ms / 1000 * 2.5)
        mutex.check()  # 续期成功 → 不抛
        with pytest.raises(RuntimeError, match="另一个测试 worker 正在使用"):
            with WorkerRoundMutex(url, ttl_ms=ttl_ms):
                pass


def test_check_reports_a_lease_that_is_no_longer_ours() -> None:
    """`check()` 直接问一次 Redis：键被别人换掉时就地报错（不只依赖续期线程的标记）。"""

    url = worker_round_module.redis_test_url()
    client = _redis_client(url)
    try:
        with WorkerRoundMutex(url, ttl_ms=1000, renew_interval_seconds=10) as mutex:
            client.set(worker_round_module.MUTEX_KEY, b"held-by-another")
            with pytest.raises(RuntimeError, match="不再属于本轮"):
                mutex.check()
    finally:
        client.delete(worker_round_module.MUTEX_KEY)
        client.close()


def test_losing_the_lease_reaps_the_real_worker_and_fails_the_round(migrated_test_database) -> None:
    """真实场景：worker 就绪之后让租约失效 → 整组被回收，本轮被判失败、产物应保留。

    这条是审查指出的缺口：原先只在"等启动日志"时核对租约，worker 起来之后丢锁不会产生任何后果，
    于是"丢锁就终止本轮"只是一句话，不是行为。
    """

    url = worker_round_module.redis_test_url()
    client = _redis_client(url)
    mutex = WorkerRoundMutex(url, ttl_ms=60_000)
    mutex.__enter__()
    round_ = None
    try:
        round_ = worker_round_module.start_worker_round(mutex=mutex)
        assert round_.worker_id is not None, "worker 没起来，这条用例覆盖不到丢锁之后的行为"

        # 就绪之后让租约易主。
        client.set(worker_round_module.MUTEX_KEY, b"held-by-another")

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and round_.captured.process.poll() is None:
            time.sleep(0.1)

        assert round_.lease_lost is True, "丢锁之后本轮没有被判失败"
        assert round_.captured.process.poll() is not None, "丢锁之后 worker 进程组没有被回收"
        assert round_.captured.group_is_alive() is False
        assert worker_round_module.must_keep_artifacts(round_) is True, "丢锁那一轮的产物不该被清理"
    finally:
        # 判定为"应保留"之后，本用例自己把它清掉：它是自检用例，不是真失败。
        if round_ is not None:
            worker_round_module.stop_worker_round(round_, keep_artifacts=False)
        mutex.__exit__(None, None, None)
        client.delete(worker_round_module.MUTEX_KEY)
        client.close()


# ---------------------------------------------------------------- 进程：持续日志与整组回收


def _spawn_sleeper(tmp_path: Path, script: str) -> CapturedProcess:
    return spawn_captured(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "child.log",
    )


def test_the_child_log_is_written_before_the_process_exits(tmp_path: Path) -> None:
    """日志持续落盘：进程还在跑（甚至被杀）时，已经产出的行必须已经在文件里。"""

    captured = _spawn_sleeper(tmp_path, "import time; print('early-line', flush=True); time.sleep(120)")
    try:
        assert captured.wait_until(
            lambda lines: any("early-line" in line for line in lines),
            deadline=time.monotonic() + 20,
        )
        assert "early-line" in captured.log_path.read_text(encoding="utf-8")
    finally:
        captured.reap()


def test_the_reaper_kills_the_whole_group_when_the_leader_already_exited(tmp_path: Path) -> None:
    """组长先退出、后代仍活着：回收必须按 PGID 走，而不是看"组长还在不在"。"""

    captured = _spawn_sleeper(
        tmp_path,
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "time.sleep(0.3)\n",
    )
    captured.process.wait(timeout=30)
    captured.reap()
    assert captured.group_is_alive() is False, "组长已退出，后代却没人收"


def test_the_log_contains_the_child_last_line_after_the_reaper_returns(tmp_path: Path) -> None:
    """回收返回即代表日志**写完**：读线程必须被等待，否则清理/读取会与它抢尾部日志。"""

    captured = _spawn_sleeper(
        tmp_path,
        "import time\nfor n in range(5):\n    print(f'line-{n}', flush=True)\n    time.sleep(0.05)\ntime.sleep(120)",
    )
    try:
        assert captured.wait_until(
            lambda lines: any("line-4" in line for line in lines),
            deadline=time.monotonic() + 20,
        )
    finally:
        captured.reap()
    assert "line-4" in captured.log_path.read_text(encoding="utf-8"), "回收返回后日志里还缺最后一行"


def test_the_reaper_kills_a_descendant_that_ignores_sigterm(tmp_path: Path) -> None:
    """后代忽略 SIGTERM 时必须升级到 SIGKILL，且回收后整组确实为空。"""

    captured = _spawn_sleeper(
        tmp_path,
        "import signal, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        "'import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(120)'])\n"
        "time.sleep(0.3)\n",
    )
    captured.process.wait(timeout=30)
    captured.reap(grace_seconds=1.0)
    assert captured.group_is_alive() is False


# ---------------------------------------------------------------- 产物保留判定


def test_artifacts_are_kept_when_an_assertion_failed_even_if_the_child_succeeded(artifact_decider) -> None:
    """退出码为 0 但断言失败时也要保留产物——保留的依据是 pytest 判定，不是退出码。

    用**独立列表**驱动判定：这条用例绝不能清空真实的会话记录，否则会把此前别处的失败抹掉，
    让本该保留的产物被清理掉（那正是它要防的错）。
    """

    assert artifact_decider(round_failed=False, failed_tests=[]) is False
    assert artifact_decider(round_failed=False, failed_tests=["some/earlier/test.py::test_x"]) is True
    assert artifact_decider(round_failed=True, failed_tests=[]) is True


def test_the_migration_round_keeps_artifacts_on_timeout() -> None:
    """超时也必须保留：旧写法在超时后没有日志可看。"""

    round_ = migration_round_module.start_round()
    try:
        # 注入一个只睡觉的子进程：这条用例不该碰测试实例。
        migration_round_module.run_full_migration(
            round_,
            timeout_seconds=1.0,
            command=[sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(120)"],
        )
        assert round_.timed_out is True
        assert migration_round_module.must_keep_artifacts(round_) is True
        log = round_.log_path.read_text(encoding="utf-8")
        assert "partial" in log, "超时那一轮没有留下子进程已经产出的日志"
    finally:
        shutil.rmtree(round_.root, ignore_errors=True)
