"""Step C：真实 worker 子进程的生命周期，以及健康记录确实**属于这个进程**。

判定链见 `_worker_round.py` 的模块 docstring：`worker_id` 只从**目标子进程自己的日志**里取，
再要求两个收敛健康键的值与它精确相等。少了后半步，"健康键存在"就可能是别的 worker 报的。

前置：conftest 的 `migrated_test_database`（独立实例上从空库跑到 `business=13 / knowledge=2`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import _worker_round as worker_round_module
from _migration_round import ROOT_ENV_VARS
from _worker_round import WorkerRound, WorkerRoundMutex


def test_the_worker_announces_its_own_identity_in_its_log(worker_round: WorkerRound) -> None:
    """没有这条启动行就无法把健康记录归属到某个进程——这一步同时是 C 的 RED 门。"""

    assert worker_round.worker_id is not None, (
        "worker 在等待窗口内没有从自己的日志里报出 worker_id——"
        f"`{worker_round_module.WORKER_ENTRY_MODULE}` 的启动行不存在或格式变了：\n{worker_round.log_tail()}"
    )
    assert worker_round.worker_id.startswith("worker-")


def test_the_health_records_belong_to_the_worker_we_started(worker_round: WorkerRound) -> None:
    """两个收敛健康键的值必须精确等于**本轮子进程**报出的 worker_id。"""

    assert worker_round.worker_id is not None, "worker 没有报出 worker_id，归属无从判定"
    values = worker_round_module.health_record_values(worker_round_module.redis_test_url())

    for key, value in values.items():
        assert value is not None, f"健康键 {key} 不存在——worker 没有发布收敛能力"
        assert value == worker_round.worker_id, (
            f"健康键 {key} 的值是 {value!r}，不是本轮 worker 的 {worker_round.worker_id}——它来自别的进程"
        )


def test_the_worker_does_not_write_outside_the_round_root(worker_round: WorkerRound) -> None:
    """worker 与迁移器一样：五个根显式改道，诱饵 cwd 必须保持为空。

    只声明**文件根**：沙箱端点被指向拒绝外联的测试端点，但 Neo4j / Milvus / MinIO 未隔离，
    因此这条不作那些方面的声明（本轮也不投递任何 job）。
    """

    leftovers = sorted(path.name for path in worker_round.child_cwd.iterdir())
    assert leftovers == [], f"worker 在 cwd 里留下了内容，说明还有文件根没被隔离：{leftovers}"
    for name in ROOT_ENV_VARS:
        assert Path(worker_round.env[name]).is_relative_to(worker_round.root)
    assert worker_round.env["SANDBOX_PROVISIONER_URL"] == worker_round_module.SANDBOX_ISOLATION_OVERRIDES[
        "SANDBOX_PROVISIONER_URL"
    ], "沙箱端点没有改道到拒绝外联的测试端点"


def test_the_round_mutex_refuses_a_second_worker(worker_round: WorkerRound) -> None:
    """健康键是全局的：`worker_round` 正持有租约，第二个 worker 必须拿不到锁。"""

    with pytest.raises(RuntimeError, match="另一个测试 worker 正在使用"):
        with WorkerRoundMutex(worker_round_module.redis_test_url()):
            pass
