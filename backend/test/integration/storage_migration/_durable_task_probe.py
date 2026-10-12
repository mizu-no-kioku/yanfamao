"""在独立测试实例上放一个「已提交但**未发布**」的 pending 意图，并读回它的终态。

## 为什么证据必须是"读库"

补发的证据不能只看日志：日志只能说明"收敛循环跑了"。要证明补发**发生过**，必须看到
`pending → running → 终态` 这条**由 worker 写下的**状态链——发布是消息进队列，消费是
`TaskRepository.claim()` 把 `status` 改成 `running` 并记下 `worker_id`/`attempt_count`，
终态由 handler 与领域 success hook 一起提交。这三步都落在库里。

## 为什么这条意图不碰外部依赖

payload 里 `count=0`，`_generate_dataset_task` 因此在
`existing_count (0) >= total_count (0)` 处**短路返回**，走到 `finish_dataset_generation_task`
就结束——不会去查 Milvus、不会调 LLM。本轮**没有**隔离 Milvus / Neo4j / MinIO（见 README
「已知限制」），所以只有走短路路径的意图才能在这里安全投递。

## 归属：写库用的是 owner 连接

探针走 `tenant_schema_url()`（owner），worker 走受限角色 `yuxi_app`。这是**有意**的：
探针扮演"提交任务的应用"，权限比 worker 宽；RLS 尚未启用（版本 13），两侧都能看到同一批行。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.storage.postgres.models_business import TaskRecord
from yuxi.storage.postgres.models_knowledge import EvaluationDataset, KnowledgeBase

from _tenant_schema_test_db import tenant_schema_url

TERMINAL_TASK_STATUSES = {"success", "failed", "cancelled"}

# 补发证据链的两端：Task 的终态，与领域 hook 写下的数据集状态。
EXPECTED_TERMINAL_STATUS = "success"
EXPECTED_DATASET_STATUS = "completed"

SESSION_SUFFIX = os.environ.get("DURABLE_TASK_GATE_ID", "").strip().replace("-", "_")[:10] or uuid.uuid4().hex[:10]


@dataclass(frozen=True)
class ProbeTask:
    """一条探针意图的三个标识。`task_id` 受 `tasks.id` 的 String(32) 限制，别加长。"""

    kb_id: str
    dataset_id: str
    task_id: str


def new_probe_task(kind: str) -> ProbeTask:
    """每次**运行**唯一（`SESSION_SUFFIX` 是进程级的，同一次运行内多个场景靠 `kind` 区分）。

    上一版用固定的 `local` 作缺省标识，跨运行复用同一个 kb_id，于是"上一轮留下的行"会参与
    本轮判定。唯一后缀把这种污染从"要小心"变成"不可能"。
    """

    suffix = f"{SESSION_SUFFIX}_{kind}"
    return ProbeTask(
        kb_id=f"kb-rs-{suffix}",
        dataset_id=f"dataset-rs-{suffix}",
        task_id=f"task-rs-{suffix}",
    )


def _pending_payload(probe: ProbeTask) -> dict:
    return {
        "dataset_id": probe.dataset_id,
        "kb_id": probe.kb_id,
        "created_by": "part2-probe",
        "name": probe.dataset_id,
        "description": "pending intent probe",
        "count": 0,  # 短路：不查 Milvus、不调 LLM
        "neighbors_count": 1,
        "concurrency_count": 1,
        "llm_model_spec": "unused:model",
        "generation_mode": "vector",
        "graph_expand_top_k": 1,
    }


async def _session_factory():
    """每次调用一个新引擎：探针与 worker 的 event loop 不同，池不能跨 loop 复用。"""

    engine = create_async_engine(tenant_schema_url(), pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def insert_pending_intent(probe: ProbeTask) -> None:
    """写知识库 + 数据集 + **pending** Task 行，**不发布**——这就是"worker 挂了留下的意图"。"""

    engine, factory = await _session_factory()
    try:
        async with factory() as session:
            # 分三次 flush：这三个 mapper 之间**没有** `relationship()`，unit of work 因此
            # 没有依赖边可依，一次 flush 的 INSERT 顺序不保证满足外键（实测会撞
            # `evaluation_datasets_kb_id_fkey`）。依赖顺序在这里是显式的，不靠 SQLAlchemy 猜。
            session.add(KnowledgeBase(kb_id=probe.kb_id, name=probe.dataset_id, kb_type="milvus"))
            await session.flush()
            session.add(
                EvaluationDataset(
                    dataset_id=probe.dataset_id,
                    kb_id=probe.kb_id,
                    name=probe.dataset_id,
                    description="pending intent probe",
                    item_count=0,
                    has_gold_chunks=True,
                    has_gold_answers=True,
                    build_metadata={
                        "source": "generated",
                        "status": "pending",
                        "progress": 0,
                        "task_id": probe.task_id,
                        "params": {"count": 0},
                    },
                    created_by="part2-probe",
                )
            )
            await session.flush()
            session.add(
                TaskRecord(
                    id=probe.task_id,
                    name="生成评估数据集",
                    type="dataset_generation",
                    status="pending",
                    message="任务等待 worker 执行",
                    payload=_pending_payload(probe),
                    handler_version=1,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


async def read_task_state(task_id: str) -> dict | None:
    """读回 Task 的补发/消费证据；不存在返回 None。"""

    engine, factory = await _session_factory()
    try:
        async with factory() as session:
            record = await session.scalar(select(TaskRecord).where(TaskRecord.id == task_id))
            if record is None:
                return None
            return {
                "status": record.status,
                "attempt_count": int(record.attempt_count or 0),
                "worker_id": record.worker_id,
                "message": record.message,
                "error": record.error,
            }
    finally:
        await engine.dispose()


async def read_dataset_metadata(dataset_id: str) -> dict | None:
    """读回领域 success hook 写下的数据集状态（终态的另一半证据）。"""

    engine, factory = await _session_factory()
    try:
        async with factory() as session:
            dataset = await session.scalar(
                select(EvaluationDataset).where(EvaluationDataset.dataset_id == dataset_id)
            )
            return None if dataset is None else dict(dataset.build_metadata or {})
    finally:
        await engine.dispose()


async def delete_probe_rows(probe: ProbeTask) -> None:
    """清掉本轮的探针行。顺序与 FK 一致：先 Task，再数据集，最后知识库。"""

    engine, factory = await _session_factory()
    try:
        async with factory() as session:
            await session.execute(delete(TaskRecord).where(TaskRecord.id == probe.task_id))
            await session.execute(delete(EvaluationDataset).where(EvaluationDataset.dataset_id == probe.dataset_id))
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == probe.kb_id))
            await session.commit()
    finally:
        await engine.dispose()
