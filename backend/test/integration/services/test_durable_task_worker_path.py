"""真实 ARQ worker 恢复未发布 PostgreSQL Durable Task 的 assembled path。"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete

from yuxi.services.task_queue_service import TASK_RECONCILIATION_SECONDS

from yuxi.knowledge.eval.service import EvaluationService
from yuxi.repositories.evaluation_repository import EvaluationRepository
from yuxi.repositories.task_repository import TaskRepository
from yuxi.services import task_service
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import TaskRecord
from yuxi.storage.postgres.models_knowledge import KnowledgeBase

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件直接使用 shipping PostgreSQL 与 worker，不依赖 HTTP API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    yield


@pytest_asyncio.fixture(autouse=True)
async def _fresh_pg_manager_per_test():
    """每个用例结束后关掉全局池。

    pytest 的 event loop 是 function 级的，而 asyncpg 连接绑在**创建它的 loop** 上。
    共享池被上一个用例归还的连接会在这个用例的另一个 loop 里被取出来，触发
    `Future attached to a different loop`，清理时再报 `Event loop is closed`。
    关掉池（并把 manager 复位）让下一个用例在自己的 loop 里重建连接。
    """

    yield
    await pg_manager.close()


# 每次**运行**唯一的标识。缺省值原先写死成 `local`，于是跨运行复用同一个 kb_id，
# 本轮的判定会受上一轮残留行的影响。CI 用 `DURABLE_TASK_GATE_ID` 显式给每轮唯一值。
_RUN_SUFFIX = os.getenv("DURABLE_TASK_GATE_ID", "").replace("-", "_")[:24] or uuid.uuid4().hex[:12]

# 等待窗口**由周期推导**，不写死。这条用例依赖周期补发（见下面那条的 docstring），而
# `_reconcile_durable_tasks_forever()` 是「先睡后做」，所以窗口必须覆盖至少一个完整周期；
# 原先的 150 × 0.1s = 15s 只有周期（30s）的一半，恰好落在 50% 的通过率上。
# 这不是"延长超时取绿灯"：窗口跟着周期走，周期改大窗口自动跟上。
_WAIT_WINDOW_SECONDS = TASK_RECONCILIATION_SECONDS * 2 + 15.0
_POLL_SECONDS = 0.1


def _gate_identity(kind: str = "success") -> tuple[str, str]:
    return f"kb_task_worker_{_RUN_SUFFIX}_{kind}", f"durable-task-worker-{_RUN_SUFFIX}-{kind}"


async def _delete_gate_facts(kb_id: str) -> None:
    datasets = await EvaluationRepository().list_datasets(kb_id)
    task_ids = [str((dataset.build_metadata or {}).get("task_id") or "") for dataset in datasets]
    async with pg_manager.get_async_session_context() as session:
        if task_ids:
            await session.execute(delete(TaskRecord).where(TaskRecord.id.in_(task_ids)))
        await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))


async def test_prepare_task_with_failed_initial_arq_publication(monkeypatch) -> None:
    """在 worker 停止时留下已提交但尚未发布的 Task intent。"""
    kb_id, dataset_name = _gate_identity()
    await _delete_gate_facts(kb_id)
    async with pg_manager.get_async_session_context() as session:
        session.add(KnowledgeBase(kb_id=kb_id, name=dataset_name, kb_type="milvus"))

    async def fail_initial_publication(_task_id: str, _tenant_id: int) -> None:
        raise ConnectionError("simulated Redis publication failure")

    monkeypatch.setattr(task_service, "publish_task", fail_initial_publication)
    submitted = await EvaluationService().generate_dataset(
        kb_id=kb_id,
        name=dataset_name,
        description="deterministic worker path",
        count=0,
        neighbors_count=1,
        concurrency_count=1,
        llm_model_spec="unused:model",
        created_by="integration",
    )

    task = await TaskRepository().get_by_id(submitted["task_id"])
    dataset = await EvaluationRepository().get_dataset(submitted["dataset_id"])
    assert task.status == "pending"
    assert (dataset.build_metadata or {}).get("status") == "pending"


async def test_shipping_worker_periodic_reconciliation_recovers_pending_publication() -> None:
    """真实 worker 的**周期**收敛恢复前一用例遗留的 pending intent。

    名字里的 `periodic` 是这条用例的实际语义，不是修饰：意图是在 worker（容器）**早已启动
    之后**才落库的，`_worker_startup` 的那次收敛早已发生，唯一能救它的是
    `_reconcile_durable_tasks_forever()` 的那次 tick。旧名字写成 `startup_recovers`，
    与它真正依赖的路径相反——那个名字现在留给
    `storage_migration/test_durable_task_republish.py` 里真正"启动前落库"的场景。
    """
    kb_id, _dataset_name = _gate_identity()
    datasets = await EvaluationRepository().list_datasets(kb_id)
    assert len(datasets) == 1
    dataset = datasets[0]
    task_id = str((dataset.build_metadata or {}).get("task_id") or "")
    assert task_id

    try:
        for _attempt in range(int(_WAIT_WINDOW_SECONDS / _POLL_SECONDS)):
            task = await TaskRepository().get_by_id(task_id)
            dataset = await EvaluationRepository().get_dataset(dataset.dataset_id)
            if task and task.status == "success" and (dataset.build_metadata or {}).get("status") == "completed":
                break
            await asyncio.sleep(_POLL_SECONDS)
        else:
            raise AssertionError(
                f"Durable Task 没有通过**周期**补发收敛：等了 {_WAIT_WINDOW_SECONDS:g} 秒"
                f"（= {TASK_RECONCILIATION_SECONDS:g} 秒周期 × 2 + 余量）"
            )

        assert task.attempt_count == 1
        assert task.worker_id is None
        assert (dataset.build_metadata or {}).get("task_id") == task_id
    finally:
        await _delete_gate_facts(kb_id)


async def test_shipping_worker_failure_runs_domain_hook() -> None:
    """真实 worker 失败必须在同一终态事务中收敛数据集领域状态。"""
    kb_id, dataset_name = _gate_identity("failure")
    await _delete_gate_facts(kb_id)
    async with pg_manager.get_async_session_context() as session:
        session.add(KnowledgeBase(kb_id=kb_id, name=dataset_name, kb_type="dify"))

    submitted = await EvaluationService().generate_dataset(
        kb_id=kb_id,
        name=dataset_name,
        description="deterministic failure-hook path",
        count=1,
        neighbors_count=1,
        concurrency_count=1,
        llm_model_spec="unused:model",
        created_by="integration",
    )

    try:
        for _attempt in range(150):
            task = await TaskRepository().get_by_id(submitted["task_id"])
            dataset = await EvaluationRepository().get_dataset(submitted["dataset_id"])
            metadata = dataset.build_metadata or {}
            if task and task.status == "failed" and metadata.get("status") == "failed":
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Durable Task failure hook did not converge through the shipping worker")

        assert task.attempt_count == 1
        assert task.worker_id is None
        assert metadata.get("task_id") == task.id
        assert metadata.get("progress") == 100
        assert metadata.get("error_message")
    finally:
        await _delete_gate_facts(kb_id)
