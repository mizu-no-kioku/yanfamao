from __future__ import annotations

import asyncio
import json
import time
import weakref
from typing import Any

from yuxi.knowledge.graphs.extractors import GraphExtractor, GraphExtractorFactory, normalize_extraction_result
from yuxi.knowledge.graphs.graph_utils import (
    build_graph_payload,
    compute_entity_id,
    compute_triple_id,
    cypher_merge_chunk,
    cypher_merge_entity_mention,
    cypher_merge_relation,
    normalize_entity_name,
)
from yuxi.knowledge.graphs.milvus_graph_vector_store import MilvusGraphVectorStore
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from yuxi.storage.neo4j import (
    Neo4jConnectionManager,
    get_shared_neo4j_connection,
    neo4j_read,
    neo4j_write,
    safe_neo4j_label,
)
from yuxi.utils import logger
from yuxi.utils.datetime_utils import utc_isoformat

GRAPH_CONFIG_KEY = "graph_build_config"
GRAPH_TASK_TYPE = "knowledge_graph_index"
NEO4J_QUERY_OFFLOAD_LIMIT = 8
# 数据库游标每次预取的 chunk 数量边界，只控制查询频率和单页内存，不限制 LLM 并发。
# 实际 LLM 并发只由 extractor_options.concurrency_count 决定；预取量会按其两倍动态计算后落在此范围内。
GRAPH_BUILD_FETCH_MIN_SIZE = 100
GRAPH_BUILD_FETCH_MAX_SIZE = 1000
# 构建任务 INFO 汇总日志与前端进度更新间隔；单个 chunk 的耗时明细使用 DEBUG 日志。
GRAPH_BUILD_LOG_INTERVAL_SECONDS = 5.0
GRAPH_VECTOR_BATCH_SIZE = 100
GRAPH_VECTOR_LEASE_SECONDS = 300
GRAPH_VECTOR_FLUSH_INTERVAL_SECONDS = 0.2
GRAPH_EXTRACTION_MAX_ATTEMPTS = 3
GRAPH_EXTRACTION_RETRY_DELAYS_SECONDS = (2.0, 10.0)
# 子图路径查询的最大跳数上限：变量长度路径的开销随节点度数增长很快，前后端共用这一个上限。
MAX_SUBGRAPH_DEPTH = 3
# 关系属性中允许筛选的属性名；text 是自由描述，extractor_type 是枚举。
RELATION_ATTRIBUTE_NAMES = ("text", "extractor_type")
_neo4j_query_offload_semaphore_refs: dict[
    int,
    tuple[weakref.ReferenceType[asyncio.AbstractEventLoop], weakref.ReferenceType[asyncio.Semaphore]],
] = {}


def _get_neo4j_query_offload_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    entry = _neo4j_query_offload_semaphore_refs.get(loop_id)
    if entry is not None:
        loop_ref, semaphore_ref = entry
        semaphore = semaphore_ref()
        if loop_ref() is loop and semaphore is not None:
            return semaphore

    semaphore = asyncio.Semaphore(NEO4J_QUERY_OFFLOAD_LIMIT)

    def cleanup(ref, stale_loop_id=loop_id):
        current_entry = _neo4j_query_offload_semaphore_refs.get(stale_loop_id)
        if current_entry is not None and current_entry[1] is ref:
            _neo4j_query_offload_semaphore_refs.pop(stale_loop_id, None)

    _neo4j_query_offload_semaphore_refs[loop_id] = (weakref.ref(loop), weakref.ref(semaphore, cleanup))
    return semaphore


async def _run_neo4j_query_io(func, /, *args, **kwargs):
    semaphore = _get_neo4j_query_offload_semaphore()
    await semaphore.acquire()
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))

    def release_capacity(completed_task: asyncio.Task):
        semaphore.release()
        if completed_task.cancelled():
            return
        completed_task.exception()

    task.add_done_callback(release_capacity)
    return await asyncio.shield(task)


class GraphAttributesNotReadyError(ValueError):
    """图谱实体尚未回填筛选属性投影，属性筛选无法给出可信结果。"""


class MilvusGraphService:
    def __init__(
        self,
        *,
        kb_id: str | None = None,
        kb_repo: KnowledgeBaseRepository | None = None,
        chunk_repo: KnowledgeChunkRepository | None = None,
        graph_repo: KnowledgeGraphRepository | None = None,
        graph_vector_store: MilvusGraphVectorStore | None = None,
        neo4j_connection: Neo4jConnectionManager | None = None,
    ):
        self.kb_id = kb_id
        self.kb_repo = kb_repo or KnowledgeBaseRepository()
        self.chunk_repo = chunk_repo or KnowledgeChunkRepository()
        self.graph_repo = graph_repo or KnowledgeGraphRepository()
        self._graph_vector_store = graph_vector_store
        self._graph_vector_store_lock = asyncio.Lock()
        self._connection = neo4j_connection

    @property
    def connection(self) -> Neo4jConnectionManager:
        if self._connection is None:
            self._connection = get_shared_neo4j_connection()
        return self._connection

    @property
    def graph_vector_store(self) -> MilvusGraphVectorStore:
        if self._graph_vector_store is None:
            self._graph_vector_store = MilvusGraphVectorStore()
        return self._graph_vector_store

    async def get_graph_vector_store(self) -> MilvusGraphVectorStore:
        if self._graph_vector_store is not None:
            return self._graph_vector_store
        async with self._graph_vector_store_lock:
            if self._graph_vector_store is None:
                self._graph_vector_store = await asyncio.to_thread(MilvusGraphVectorStore)
        return self._graph_vector_store

    @property
    def driver(self):
        return self.connection.driver

    async def get_status(self, kb_id: str, *, tasker: Any = None) -> dict[str, Any]:
        kb = await self._get_milvus_kb(kb_id)
        params = dict(kb.additional_params or {})
        config = params.get(GRAPH_CONFIG_KEY) or {}
        status_values = await asyncio.gather(
            self.chunk_repo.count_by_kb_id(kb_id),
            self.chunk_repo.count_graph_pending_by_kb_id(kb_id),
            self.chunk_repo.count_graph_indexed_by_kb_id(kb_id),
            self.chunk_repo.count_graph_structure_indexed_by_kb_id(kb_id),
            self.chunk_repo.count_graph_extraction_statuses_by_kb_id(kb_id),
            self.graph_repo.count_by_kb_id(kb_id),
            self.graph_repo.count_vector_statuses_by_kb_id(kb_id),
        )
        (
            total_chunks,
            pending_chunks,
            indexed_chunks,
            structured_chunks,
            extraction_counts,
            graph_counts,
            vector_counts,
        ) = status_values
        entity_count, relationship_count = graph_counts

        build_task_status = None
        build_task_progress = 0
        if tasker is not None:
            latest_task = await tasker.find_task_by_payload(
                task_type=GRAPH_TASK_TYPE,
                payload_match={"kb_id": kb_id},
                statuses=None,
            )
            if latest_task and latest_task.status in {"pending", "running"}:
                build_task_status = latest_task.status
                build_task_progress = round(latest_task.progress)
            elif latest_task and latest_task.status == "success":
                build_task_status = "completed"
                build_task_progress = 100
            elif latest_task and latest_task.status in {"failed", "cancelled"}:
                build_task_status = "failed"

        return {
            "kb_id": kb_id,
            "kb_type": kb.kb_type,
            "configured": bool(config),
            "locked": bool(config.get("locked")),
            "config": self._public_config(config),
            "total_chunks": total_chunks,
            "pending_chunks": pending_chunks,
            "indexed_chunks": indexed_chunks,
            "structured_chunks": structured_chunks,
            "extraction_counts": extraction_counts,
            "vector_counts": vector_counts,
            "entity_count": entity_count,
            "relationship_count": relationship_count,
            "build_task_status": build_task_status,
            "build_task_progress": build_task_progress,
        }

    async def configure(
        self,
        kb_id: str,
        extractor_type: str,
        extractor_options: dict[str, Any],
        created_by: str,
    ) -> dict:
        kb = await self._get_milvus_kb(kb_id)
        additional_params = dict(kb.additional_params or {})
        existing_config = additional_params.get(GRAPH_CONFIG_KEY) or {}
        normalized_extractor_type = (extractor_type or "").lower()
        if existing_config.get("locked"):
            existing_extractor_type = (existing_config.get("extractor_type") or "").lower()
            if normalized_extractor_type != existing_extractor_type:
                raise ValueError("图谱抽取器类型已锁定，只能修改模型、Schema 等抽取参数")

        extractor_options = extractor_options or {}
        if normalized_extractor_type == "llm" and extractor_options.get("prompt"):
            raise ValueError("LLM 图谱抽取器不支持自定义完整 Prompt，请使用 schema 配置抽取约束")
        GraphExtractorFactory.create(normalized_extractor_type, extractor_options)
        config = {
            "locked": True,
            "extractor_type": normalized_extractor_type,
            "extractor_options": extractor_options or {},
            "created_at": existing_config.get("created_at") or utc_isoformat(),
            "created_by": existing_config.get("created_by") or created_by,
        }
        if existing_config.get("locked"):
            config["updated_at"] = utc_isoformat()
            config["updated_by"] = created_by
        additional_params[GRAPH_CONFIG_KEY] = config
        await self.kb_repo.update(kb_id, {"additional_params": additional_params})
        return config

    async def get_failed_chunk_samples(self, kb_id: str, limit: int = 10) -> dict[str, Any]:
        await self._get_milvus_kb(kb_id)
        samples = await self.chunk_repo.list_graph_extraction_failed_samples(kb_id, limit)
        return {"kb_id": kb_id, "samples": samples}

    async def build_pending_chunks(self, kb_id: str, *, context=None) -> dict[str, Any]:
        kb = await self._get_milvus_kb(kb_id)
        config = self._get_locked_config(kb.additional_params or {})
        extractor_options = self._runtime_extractor_options(config)
        extractor = GraphExtractorFactory.create(config["extractor_type"], extractor_options)
        worker_count = self._get_worker_count(config)
        total_pending = await self.chunk_repo.count_graph_pending_by_kb_id(kb_id)
        initially_indexed = await self.chunk_repo.count_graph_indexed_by_kb_id(kb_id)
        processed = 0
        extraction_failed = 0
        write_failed = 0
        extraction_completed = 0
        write_completed = 0
        active_extractions = 0
        fetch_size = max(GRAPH_BUILD_FETCH_MIN_SIZE, min(worker_count * 2, GRAPH_BUILD_FETCH_MAX_SIZE))
        extraction_queue: asyncio.Queue[Any | None] = asyncio.Queue(maxsize=max(worker_count * 2, 1))
        write_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=max(worker_count * 2, 1))
        reporter_stop = asyncio.Event()
        structure_done = asyncio.Event()
        vector_wakeup = asyncio.Event()
        started_at = time.monotonic()

        logger.info(
            f"图谱构建开始 kb_id={kb_id} pending={total_pending} "
            f"extraction_concurrency={worker_count} fetch_size={fetch_size}"
        )

        async def put_queue_item(queue: asyncio.Queue, item) -> None:
            while True:
                if context is not None:
                    await context.raise_if_cancelled()
                try:
                    await asyncio.wait_for(queue.put(item), timeout=0.5)
                    return
                except TimeoutError:
                    continue

        async def extraction_worker(worker_index: int) -> None:
            nonlocal active_extractions, extraction_completed, extraction_failed
            while True:
                chunk = await extraction_queue.get()
                try:
                    if chunk is None:
                        return
                    if context is not None:
                        await context.raise_if_cancelled()
                    active_extractions += 1
                    extraction_started_at = time.monotonic()
                    try:
                        await self._get_chunk_extraction_result(kb_id, chunk, extractor)
                        await put_queue_item(write_queue, chunk.chunk_id)
                    except Exception as exc:
                        extraction_failed += 1
                        logger.error(
                            f"Chunk 图谱抽取失败 kb_id={kb_id} chunk_id={chunk.chunk_id} worker={worker_index}: {exc}"
                        )
                    finally:
                        active_extractions -= 1
                        extraction_completed += 1
                        logger.debug(
                            f"Chunk 图谱抽取结束 kb_id={kb_id} chunk_id={chunk.chunk_id} "
                            f"worker={worker_index} duration={time.monotonic() - extraction_started_at:.2f}s"
                        )
                finally:
                    extraction_queue.task_done()

        async def write_worker() -> None:
            nonlocal processed, write_failed, write_completed
            while True:
                chunk_id = await write_queue.get()
                try:
                    if chunk_id is None:
                        return
                    if context is not None:
                        await context.raise_if_cancelled()
                    chunk = await self.chunk_repo.get_by_chunk_id(chunk_id)
                    if chunk is None:
                        raise ValueError(f"图谱写入找不到 chunk: {chunk_id}")
                    extraction_result = await self._get_chunk_extraction_result(kb_id, chunk, extractor)
                    write_started_at = time.monotonic()
                    entities, triples = await asyncio.to_thread(
                        self.write_chunk_graph,
                        kb_id,
                        chunk,
                        extraction_result,
                    )
                    await self.graph_repo.upsert_chunk_graph(
                        kb_id=kb_id,
                        file_id=chunk.file_id,
                        chunk_id=chunk.chunk_id,
                        entities=entities,
                        triples=triples,
                    )
                    await self.chunk_repo.mark_graph_structure_indexed(
                        chunk.chunk_id,
                        ent_ids=[entity["entity_id"] for entity in entities],
                    )
                    vector_wakeup.set()
                    processed += 1
                    logger.debug(
                        f"Chunk 图谱写入结束 kb_id={kb_id} chunk_id={chunk.chunk_id} "
                        f"entities={len(entities)} triples={len(triples)} "
                        f"duration={time.monotonic() - write_started_at:.2f}s"
                    )
                except Exception as exc:
                    write_failed += 1
                    logger.error(f"Chunk 图谱写入失败 kb_id={kb_id} chunk_id={chunk_id}: {exc}")
                finally:
                    if chunk_id is not None:
                        write_completed += 1
                    write_queue.task_done()

        async def index_vector_batch(record_type: str) -> int:
            lock_token, records = await self.graph_repo.claim_vector_records(
                kb_id=kb_id,
                record_type=record_type,
                limit=GRAPH_VECTOR_BATCH_SIZE,
                lease_seconds=GRAPH_VECTOR_LEASE_SECONDS,
            )
            if not records:
                return 0
            record_ids = [record["id"] for record in records]
            try:
                graph_vector_store = await self.get_graph_vector_store()
                await graph_vector_store.upsert_graph_records(
                    kb_id=kb_id,
                    embedding_model_spec=kb.embedding_model_spec,
                    record_type=record_type,
                    records=records,
                )
                await self.graph_repo.mark_vector_records_indexed(
                    record_type=record_type,
                    record_ids=record_ids,
                    lock_token=lock_token,
                )
            except asyncio.CancelledError as exc:
                await self.graph_repo.mark_vector_records_failed(
                    record_type=record_type,
                    record_ids=record_ids,
                    lock_token=lock_token,
                    error=str(exc) or "cancelled",
                )
                raise
            except Exception as exc:
                await self.graph_repo.mark_vector_records_failed(
                    record_type=record_type,
                    record_ids=record_ids,
                    lock_token=lock_token,
                    error=str(exc),
                )
                logger.error(f"图谱向量索引失败 kb_id={kb_id} type={record_type} count={len(records)}: {exc}")
            return len(records)

        async def vector_worker() -> None:
            while True:
                if context is not None:
                    await context.raise_if_cancelled()
                entity_count, triple_count = await asyncio.gather(
                    index_vector_batch("entity"),
                    index_vector_batch("triple"),
                )
                if entity_count or triple_count:
                    await self.graph_repo.finalize_graph_indexed_chunks(kb_id)
                    continue

                vector_counts = await self.graph_repo.count_vector_statuses_by_kb_id(kb_id)
                if structure_done.is_set() and not vector_counts["pending"] and not vector_counts["processing"]:
                    await self.graph_repo.finalize_graph_indexed_chunks(kb_id)
                    return
                vector_wakeup.clear()
                try:
                    await asyncio.wait_for(vector_wakeup.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                if vector_wakeup.is_set():
                    await asyncio.sleep(GRAPH_VECTOR_FLUSH_INTERVAL_SECONDS)

        async def report_progress() -> None:
            while True:
                try:
                    await asyncio.wait_for(reporter_stop.wait(), timeout=GRAPH_BUILD_LOG_INTERVAL_SECONDS)
                except TimeoutError:
                    pass

                elapsed = max(time.monotonic() - started_at, 0.001)
                extraction_rate = extraction_completed / elapsed
                vector_counts = await self.graph_repo.count_vector_statuses_by_kb_id(kb_id)
                message = (
                    f"图谱构建：抽取 {extraction_completed}/{total_pending} "
                    f"(活跃 {active_extractions}/{worker_count})，写入 {write_completed}/{total_pending}，"
                    f"向量待处理 {vector_counts['pending'] + vector_counts['processing']}，"
                    f"向量失败 {vector_counts['failed']}，抽取失败 {extraction_failed}，"
                    f"写入失败 {write_failed}，抽取吞吐 {extraction_rate:.2f} chunk/s"
                )
                logger.info(f"kb_id={kb_id} {message}")
                if context is not None:
                    extraction_progress = extraction_completed / max(total_pending, 1) * 40.0
                    structure_progress = write_completed / max(total_pending, 1) * 20.0
                    indexed_chunks = await self.chunk_repo.count_graph_indexed_by_kb_id(kb_id)
                    newly_indexed = max(indexed_chunks - initially_indexed, 0)
                    vector_progress = newly_indexed / max(total_pending, 1) * 35.0
                    await context.set_progress(
                        5.0 + min(extraction_progress + structure_progress + vector_progress, 95.0), message
                    )
                if reporter_stop.is_set():
                    return

        extraction_workers = [
            asyncio.create_task(extraction_worker(index + 1), name=f"graph-extractor-{index + 1}")
            for index in range(worker_count)
        ]
        writer_task = asyncio.create_task(write_worker(), name="graph-writer")
        vector_task = asyncio.create_task(vector_worker(), name="graph-vector-indexer")
        reporter_task = asyncio.create_task(report_progress(), name="graph-progress-reporter")

        try:
            after_id = 0
            while True:
                if context is not None:
                    await context.raise_if_cancelled()
                chunks = await self.chunk_repo.list_graph_pending_by_kb_id(
                    kb_id,
                    fetch_size,
                    after_id=after_id,
                )
                if not chunks:
                    break
                for chunk in chunks:
                    after_id = chunk.id
                    if getattr(chunk, "graph_structure_indexed", False):
                        extraction_completed += 1
                        write_completed += 1
                        vector_wakeup.set()
                    elif chunk.extraction_result:
                        extraction_completed += 1
                        await put_queue_item(write_queue, chunk.chunk_id)
                    else:
                        await put_queue_item(extraction_queue, chunk)

            for _ in extraction_workers:
                await put_queue_item(extraction_queue, None)
            await asyncio.gather(*extraction_workers)
            await put_queue_item(write_queue, None)
            await writer_task
            structure_done.set()
            vector_wakeup.set()
            await vector_task
        except BaseException:
            for task in [*extraction_workers, writer_task, vector_task]:
                task.cancel()
            await asyncio.gather(*extraction_workers, writer_task, vector_task, return_exceptions=True)
            raise
        finally:
            reporter_stop.set()
            await asyncio.gather(reporter_task, return_exceptions=True)

        remaining = await self.chunk_repo.count_graph_pending_by_kb_id(kb_id)
        extraction_counts = await self.chunk_repo.count_graph_extraction_statuses_by_kb_id(kb_id)
        vector_counts = await self.graph_repo.count_vector_statuses_by_kb_id(kb_id)
        logger.info(
            f"图谱构建结束 kb_id={kb_id} success={processed} extraction_failed={extraction_failed} "
            f"write_failed={write_failed} remaining={remaining} "
            f"duration={time.monotonic() - started_at:.2f}s"
        )
        incomplete = max(remaining - extraction_counts["failed"], 0)
        result = {
            "kb_id": kb_id,
            "success": max(total_pending - remaining, 0),
            "failed": extraction_failed + write_failed,
            "extraction_failed": extraction_failed,
            "write_failed": write_failed,
            "remaining": remaining,
            "vector_failed": vector_counts["failed"],
        }
        if incomplete or write_failed or vector_counts["failed"]:
            raise RuntimeError(
                f"图谱构建执行异常：chunk_incomplete={incomplete}, "
                f"write_failed={write_failed}, vector_failed={vector_counts['failed']}"
            )
        return result

    @staticmethod
    def _get_worker_count(config: dict[str, Any]) -> int:
        if (config.get("extractor_type") or "").lower() != "llm":
            return 1
        try:
            worker_count = int((config.get("extractor_options") or {}).get("concurrency_count") or 1)
        except (TypeError, ValueError):
            return 1
        return max(1, min(worker_count, 1000))

    @staticmethod
    def _runtime_extractor_options(config: dict[str, Any]) -> dict[str, Any]:
        options = dict(config.get("extractor_options") or {})
        options.pop("prompt", None)
        return options

    async def _get_chunk_extraction_result(self, kb_id: str, chunk, extractor: GraphExtractor) -> dict[str, Any]:
        extractor_type = extractor.extractor_type
        if chunk.extraction_result:
            return normalize_extraction_result(chunk.extraction_result, extractor_type)

        details = getattr(chunk, "graph_extraction_details", None) or {}
        if details.get("status") == "failed":
            await self.chunk_repo.mark_graph_extraction_pending(chunk.chunk_id)

        metadata = {
            "kb_id": kb_id,
            "chunk_id": chunk.chunk_id,
            "file_id": chunk.file_id,
            "chunk_index": chunk.chunk_index,
        }
        for attempt in range(1, GRAPH_EXTRACTION_MAX_ATTEMPTS + 1):
            try:
                extraction_result = await extractor.extract(chunk.content, chunk_metadata=metadata)
                normalized_result = normalize_extraction_result(extraction_result, extractor_type)
                await self.chunk_repo.update_extraction_result(chunk.chunk_id, normalized_result, attempt)
                return normalized_result
            except Exception as exc:
                if attempt >= GRAPH_EXTRACTION_MAX_ATTEMPTS:
                    await self.chunk_repo.mark_graph_extraction_failed(chunk.chunk_id, attempt, str(exc))
                    raise
                delay = GRAPH_EXTRACTION_RETRY_DELAYS_SECONDS[attempt - 1]
                logger.warning(
                    f"Chunk 图谱抽取重试 kb_id={kb_id} chunk_id={chunk.chunk_id} "
                    f"attempt={attempt}/{GRAPH_EXTRACTION_MAX_ATTEMPTS} delay={delay:.1f}s: {exc}"
                )
                await asyncio.sleep(delay)

        raise RuntimeError(f"Chunk 图谱抽取未返回结果: {chunk.chunk_id}")

    def write_chunk_graph(
        self,
        kb_id: str,
        chunk,
        normalized_result: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """将单个 chunk 的抽取结果写入 Neo4j。"""
        label = safe_neo4j_label(kb_id)
        graph_payload = build_graph_payload(normalized_result)
        relation_extractor_type = graph_payload["metadata"].get("extractor_type", "unknown")
        entities = graph_payload["entities"]
        relations = graph_payload["relations"]
        entity_by_id = {entity["id"]: entity for entity in entities}
        entity_records = self._build_entity_records(kb_id, entities)
        entity_record_by_local_id = {
            entity["id"]: record for entity, record in zip(entities, entity_records, strict=True)
        }
        triple_records = self._build_triple_records(kb_id, relations, entity_record_by_local_id, graph_payload)
        content_preview = (chunk.content or "")[:300]

        # 预构建 Cypher 模板（同一 chunk 内复用）
        merge_chunk_cypher = cypher_merge_chunk(label)
        merge_entity_cypher = cypher_merge_entity_mention(label)
        merge_relation_cypher = cypher_merge_relation(label)

        def query(tx):
            # 1. MERGE Chunk 节点
            tx.run(
                merge_chunk_cypher,
                chunk_id=chunk.chunk_id,
                file_id=chunk.file_id,
                kb_id=kb_id,
                chunk_index=chunk.chunk_index,
                content_preview=content_preview,
                start_char_pos=chunk.start_char_pos,
                end_char_pos=chunk.end_char_pos,
            )

            # 2. MERGE Entity 节点 + Chunk→Entity (MENTIONS)
            for entity in entities:
                entity_record = entity_record_by_local_id[entity["id"]]
                tx.run(
                    merge_entity_cypher,
                    chunk_id=chunk.chunk_id,
                    file_id=chunk.file_id,
                    kb_id=kb_id,
                    entity_id=entity_record["entity_id"],
                    normalized_name=normalize_entity_name(entity["text"]),
                    entity_label=entity.get("label") or "Entity",
                    name=entity["text"],
                    attributes=json.dumps(entity.get("attributes") or [], ensure_ascii=False),
                    attribute_names=entity_record["attribute_names"],
                    attribute_values=entity_record["attribute_values"],
                )

            # 3. MERGE Entity→Entity (RELATION) 边
            for relation in relations:
                source = entity_by_id[relation["source"]]
                target = entity_by_id[relation["target"]]
                source_record = entity_record_by_local_id[relation["source"]]
                target_record = entity_record_by_local_id[relation["target"]]
                relation_type = relation.get("label") or "RELATED_TO"
                triple_id = compute_triple_id(
                    kb_id,
                    source_record["normalized_name"],
                    source_record["label"],
                    relation_type,
                    target_record["normalized_name"],
                    target_record["label"],
                )
                tx.run(
                    merge_relation_cypher,
                    kb_id=kb_id,
                    chunk_id=chunk.chunk_id,
                    file_id=chunk.file_id,
                    source_name=normalize_entity_name(source["text"]),
                    source_label=source.get("label") or "Entity",
                    target_name=normalize_entity_name(target["text"]),
                    target_label=target.get("label") or "Entity",
                    relation_type=relation_type,
                    triple_id=triple_id,
                    text=relation["text"],
                    extractor_type=relation_extractor_type,
                )

        neo4j_write(self.driver, query)
        return entity_records, triple_records

    def _build_entity_records(self, kb_id: str, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把抽取实体转成 Neo4j 写入记录，并投影可精确匹配的属性平行数组。"""
        records = []
        for entity in entities:
            label = entity.get("label") or "Entity"
            normalized_name = normalize_entity_name(entity["text"])
            entity_id = compute_entity_id(kb_id, normalized_name, label)
            attributes = entity.get("attributes") or []
            records.append(
                {
                    "entity_id": entity_id,
                    "kb_id": kb_id,
                    "normalized_name": normalized_name,
                    "label": label,
                    "name": entity["text"],
                    "attributes": attributes,
                    # Neo4j 属性只支持标量与标量数组，不能存 map 数组；
                    # 平行数组按索引对应，避免用分隔符拼串时属性值含分隔符导致错配。
                    "attribute_names": [attribute["label"] for attribute in attributes],
                    "attribute_values": [attribute["text"] for attribute in attributes],
                    "content": normalized_name,
                }
            )
        return records

    def _build_triple_records(
        self,
        kb_id: str,
        relations: list[dict[str, Any]],
        entity_record_by_local_id: dict[str, dict[str, Any]],
        graph_payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        records = []
        seen_triple_ids: set[str] = set()
        extractor_type = graph_payload["metadata"].get("extractor_type", "unknown")
        for relation in relations:
            source_record = entity_record_by_local_id[relation["source"]]
            target_record = entity_record_by_local_id[relation["target"]]
            relation_type = relation.get("label") or "RELATED_TO"
            triple_id = compute_triple_id(
                kb_id,
                source_record["normalized_name"],
                source_record["label"],
                relation_type,
                target_record["normalized_name"],
                target_record["label"],
            )
            if triple_id in seen_triple_ids:
                continue
            seen_triple_ids.add(triple_id)
            content = f"{source_record['normalized_name']} → {relation_type} → {target_record['normalized_name']}"
            records.append(
                {
                    "triple_id": triple_id,
                    "kb_id": kb_id,
                    "source_entity_id": source_record["entity_id"],
                    "target_entity_id": target_record["entity_id"],
                    "relation_type": relation_type,
                    "content": content,
                    "text": relation["text"],
                    "extractor_type": extractor_type,
                }
            )
        return records

    async def reset(self, kb_id: str, *, clear_extraction_result: bool, clear_config: bool) -> dict[str, Any]:
        kb = await self._get_milvus_kb(kb_id)
        await asyncio.to_thread(self.delete_graph, kb_id)
        await self.graph_repo.delete_by_kb_id(kb_id)
        reset_chunks = await self.chunk_repo.reset_graph_state_by_kb_id(kb_id, clear_extraction_result)
        if clear_config:
            additional_params = dict(kb.additional_params or {})
            additional_params.pop(GRAPH_CONFIG_KEY, None)
            await self.kb_repo.update(kb_id, {"additional_params": additional_params})
        return {
            "message": "图谱构建状态已重置",
            "status": "success",
            "reset_chunks": reset_chunks,
            "clear_extraction_result": clear_extraction_result,
            "clear_config": clear_config,
        }

    async def reconcile_vectors(self, kb_id: str, *, all_vectors: bool) -> dict[str, Any]:
        await self._get_milvus_kb(kb_id)
        reset_records = await self.graph_repo.reconcile_vector_records(kb_id, all_vectors=all_vectors)
        if all_vectors:
            graph_vector_store = await self.get_graph_vector_store()
            await asyncio.to_thread(graph_vector_store.drop_graph_collections, kb_id)
        return {
            "kb_id": kb_id,
            "mode": "all_vectors" if all_vectors else "failed",
            "reset_records": reset_records,
        }

    def delete_graph(self, kb_id: str) -> None:
        label = safe_neo4j_label(kb_id)

        def query(tx):
            tx.run(f"MATCH (n:MilvusKB:`{label}`) DETACH DELETE n")

        neo4j_write(self.driver, query)
        self.graph_vector_store.drop_graph_collections(kb_id)

    async def delete_file_graph(self, kb_id: str, file_id: str) -> None:
        orphan_entity_ids, orphan_triple_ids = await self.graph_repo.delete_file_references(file_id)
        await self.graph_vector_store.delete_graph_records(
            kb_id,
            entity_ids=orphan_entity_ids,
            triple_ids=orphan_triple_ids,
        )
        await asyncio.to_thread(self._delete_file_graph_from_neo4j, kb_id, file_id)

    def _delete_file_graph_from_neo4j(self, kb_id: str, file_id: str) -> None:
        label = safe_neo4j_label(kb_id)

        def query(tx):
            tx.run(
                f"""
                MATCH (:Entity:MilvusKB:`{label}`)-[r:RELATION {{kb_id: $kb_id, file_id: $file_id}}]->
                    (:Entity:MilvusKB:`{label}`)
                DELETE r
                """,
                kb_id=kb_id,
                file_id=file_id,
            )
            tx.run(
                f"""
                MATCH (:Chunk:MilvusKB:`{label}` {{kb_id: $kb_id, file_id: $file_id}})-[m:MENTIONS]->
                    (e:Entity:MilvusKB:`{label}`)
                DELETE m
                WITH DISTINCT e
                WHERE NOT ()-[:MENTIONS]->(e)
                DETACH DELETE e
                """,
                kb_id=kb_id,
                file_id=file_id,
            )
            tx.run(
                f"""
                MATCH (c:Chunk:MilvusKB:`{label}` {{kb_id: $kb_id, file_id: $file_id}})
                DETACH DELETE c
                """,
                kb_id=kb_id,
                file_id=file_id,
            )

        neo4j_write(self.driver, query)

    async def query_subgraph(
        self,
        kb_id: str | None = None,
        *,
        start_id: str | None = None,
        end_id: str | None = None,
        max_depth: int = 2,
        max_nodes: int = 100,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按起点（可选终点）与四类筛选查询子图。

        查询失败向上抛出，不返回形状正确的空结果。
        """
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return {"nodes": [], "edges": [], "truncated": False}
        if start_id and end_id and start_id == end_id:
            raise ValueError("起点与终点不能是同一个实体")
        if end_id and not start_id:
            raise ValueError("只有填写起点后才能指定终点")

        effective_filters = filters or {}
        if effective_filters.get("entity_attributes") and not await self.attributes_ready(effective_kb_id):
            raise GraphAttributesNotReadyError(
                "该图谱尚未回填筛选属性，请先执行图谱属性回填后再按实体属性筛选"
            )

        label = safe_neo4j_label(effective_kb_id)
        bounded_depth = max(1, min(max_depth, MAX_SUBGRAPH_DEPTH))
        effective_max_nodes = max(1, max_nodes)
        cypher = self._build_subgraph_cypher(
            label,
            start_id=start_id,
            end_id=end_id,
            max_depth=bounded_depth,
            filters=effective_filters,
        )
        params: dict[str, Any] = {
            "kb_id": effective_kb_id,
            "max_nodes": effective_max_nodes,
            "path_limit": effective_max_nodes * 10,
        }
        if start_id:
            params["start_id"] = start_id
        if end_id:
            params["end_id"] = end_id
        # 参数名必须与 _build_subgraph_cypher 生成引用时的判断条件逐条对齐，否则 Neo4j 会报缺参。
        if effective_filters.get("intermediate_entities"):
            params["entity_types"] = list(effective_filters["intermediate_entities"])
        if effective_filters.get("entity_attributes"):
            params["entity_attributes"] = [
                {"name": condition["name"], "value": condition["value"]}
                for condition in effective_filters["entity_attributes"]
            ]
        if effective_filters.get("relation_types"):
            params["relation_types"] = list(effective_filters["relation_types"])
        text_values = [
            condition["value"]
            for condition in effective_filters.get("relation_attributes") or []
            if condition["name"] == "text"
        ]
        if text_values:
            params["relation_attr_text"] = text_values
        extractor_values = [
            condition["value"]
            for condition in effective_filters.get("relation_attributes") or []
            if condition["name"] == "extractor_type"
        ]
        if extractor_values:
            params["relation_attr_extractor_type"] = extractor_values

        return await _run_neo4j_query_io(
            self._query_subgraph_sync,
            effective_kb_id,
            cypher,
            params,
            effective_max_nodes,
        )

    def _query_subgraph_sync(
        self,
        kb_id: str,
        cypher: str,
        params: dict[str, Any],
        max_nodes: int,
    ) -> dict[str, Any]:
        with self.driver.session() as session:
            record = session.run(cypher, **params).single()
            if not record:
                return {"nodes": [], "edges": [], "truncated": False}
            result = self._process_subgraph_record(record, max_nodes, kb_id)
            result["truncated"] = bool(record.get("truncated"))
            return result

    async def attributes_ready(self, kb_id: str) -> bool:
        """判断图谱中是否存在「有属性但未投影」的实体，用于属性筛选的就绪门。"""
        label = safe_neo4j_label(kb_id)
        cypher = f"""
        MATCH (e:Entity:MilvusKB:`{label}`)
        WHERE coalesce(e.attributes, '[]') <> '[]'
          AND (e.attribute_names IS NULL OR size(e.attribute_names) = 0)
        RETURN count(e) AS unbackfilled
        """
        record = await _run_neo4j_query_io(self._attributes_ready_sync, cypher)
        return int((record or {}).get("unbackfilled") or 0) == 0

    def _attributes_ready_sync(self, cypher: str) -> dict[str, Any]:
        with self.driver.session() as session:
            row = session.run(cypher).single()
            return dict(row) if row else {}

    # 关系描述是自由文本，只作为筛选建议返回并限制条数，不构成可选值的闭集。
    RELATION_TEXT_SUGGESTION_LIMIT = 500

    async def get_filter_options(self, kb_id: str | None = None) -> dict[str, Any]:
        """返回该知识库可用于筛选的类型与属性枚举，以及属性投影就绪状态。"""
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return {
                "entity_types": [],
                "entity_attributes": [],
                "relation_types": [],
                "relation_attributes": [],
                "text_values_truncated": False,
                "attributes_ready": True,
            }
        label = safe_neo4j_label(effective_kb_id)
        (
            entity_types,
            entity_attributes,
            relation_types,
            relation_attributes,
        ) = await _run_neo4j_query_io(
            self._filter_options_sync,
            label,
            self.RELATION_TEXT_SUGGESTION_LIMIT,
        )
        values_by_name: dict[str, list[str]] = {}
        for row in entity_attributes:
            values_by_name.setdefault(row["name"], [])
            if row["value"] not in values_by_name[row["name"]]:
                values_by_name[row["name"]].append(row["value"])
        text_values = [row["text"] for row in relation_attributes if row["text"] is not None]
        extractor_values = sorted(
            {row["extractor_type"] for row in relation_attributes if row["extractor_type"]}
        )
        return {
            "entity_types": [row["entity_type"] for row in entity_types],
            "entity_attributes": [
                {"name": name, "values": values} for name, values in sorted(values_by_name.items())
            ],
            "relation_types": [row["relation_type"] for row in relation_types if row["relation_type"]],
            "relation_attributes": [
                {"name": "text", "values": text_values},
                {"name": "extractor_type", "values": extractor_values},
            ],
            "text_values_truncated": len(text_values) >= self.RELATION_TEXT_SUGGESTION_LIMIT,
            "attributes_ready": await self.attributes_ready(effective_kb_id),
        }

    def _filter_options_sync(self, label: str, text_limit: int) -> tuple[list, list, list, list]:
        """在一次会话内取齐四类枚举，避免为每组枚举各开一次事务。"""
        with self.driver.session() as session:
            entity_types = list(
                session.run(
                    f"MATCH (e:Entity:MilvusKB:`{label}`) "
                    "RETURN DISTINCT e.label AS entity_type ORDER BY entity_type"
                )
            )
            entity_attributes = list(
                session.run(
                    f"MATCH (e:Entity:MilvusKB:`{label}`) "
                    "WHERE e.attribute_names IS NOT NULL "
                    "UNWIND range(0, size(e.attribute_names) - 1) AS i "
                    "RETURN DISTINCT e.attribute_names[i] AS name, e.attribute_values[i] AS value "
                    "ORDER BY name, value"
                )
            )
            relation_types = list(
                session.run(
                    f"MATCH (:Entity:MilvusKB:`{label}`)-[r:RELATION]->(:Entity:MilvusKB:`{label}`) "
                    "RETURN DISTINCT r.type AS relation_type ORDER BY relation_type"
                )
            )
            relation_attributes = list(
                session.run(
                    f"MATCH (:Entity:MilvusKB:`{label}`)-[r:RELATION]->(:Entity:MilvusKB:`{label}`) "
                    "RETURN DISTINCT r.text AS text, r.extractor_type AS extractor_type "
                    "ORDER BY text LIMIT $text_limit",
                    text_limit=text_limit,
                )
            )
        return entity_types, entity_attributes, relation_types, relation_attributes

    async def search_entities(
        self, kb_id: str | None = None, *, query: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        """按名称子串搜索实体，供起点/终点选择器使用。"""
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return []
        label = safe_neo4j_label(effective_kb_id)
        cypher = f"""
        MATCH (e:Entity:MilvusKB:`{label}`)
        WHERE e.entity_id IS NOT NULL
          AND ($q = '' OR toLower(coalesce(e.name, '')) CONTAINS toLower($q))
        RETURN e.entity_id AS entity_id, e.name AS name, e.label AS label
        ORDER BY e.name
        LIMIT $limit
        """
        rows = await _run_neo4j_query_io(
            self._search_entities_sync, cypher, {"q": query or "", "limit": max(1, min(limit, 100))}
        )
        return [
            {
                "entity_id": row["entity_id"],
                "name": row["name"] or row["entity_id"],
                "label": row["label"] or "Entity",
            }
            for row in rows
        ]

    def _search_entities_sync(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self.driver.session() as session:
            return [dict(row) for row in session.run(cypher, **params)]

    # 回填按批读写；批次只控制单页内存与事务长度，不影响最终结果。
    ATTRIBUTE_BACKFILL_BATCH_SIZE = 500

    async def backfill_attribute_projection(self, kb_id: str, *, context: Any = None) -> dict[str, Any]:
        """把 PostgreSQL 镜像里的实体属性投影回填到 Neo4j，幂等可重复执行。

        回填不解析 Neo4j 里的 JSON 字符串（Cypher 无法解析），而是以 PostgreSQL 镜像为输入。
        """
        await self._get_milvus_kb(kb_id)
        label = safe_neo4j_label(kb_id)
        graph_counts = await self.graph_repo.count_by_kb_id(kb_id)
        total_entities = graph_counts[0] if graph_counts else 0
        scanned = 0
        updated = 0
        after_id = 0
        while True:
            if context is not None:
                await context.raise_if_cancelled()
            entities = await self.graph_repo.list_entities_with_attributes_by_kb_id(
                kb_id, self.ATTRIBUTE_BACKFILL_BATCH_SIZE, after_id=after_id
            )
            if not entities:
                break
            rows = []
            for entity in entities:
                after_id = entity.id
                attributes = entity.attributes or []
                rows.append(
                    {
                        "entity_id": entity.entity_id,
                        "names": [attribute.get("label") or "Attribute" for attribute in attributes],
                        "values": [attribute.get("text") or "" for attribute in attributes],
                    }
                )
            updated += await asyncio.to_thread(self._write_attribute_projection, label, rows)
            scanned += len(entities)
            if context is not None:
                await context.set_progress(
                    min(95.0, scanned / max(total_entities, 1) * 95.0),
                    f"属性投影回填 {scanned}/{total_entities}",
                )
        return {"kb_id": kb_id, "scanned": scanned, "updated": updated}

    def _write_attribute_projection(self, label: str, rows: list[dict[str, Any]]) -> int:
        """按 entity_id 批量写回投影属性；只更新已存在的实体，不创建新节点。"""
        cypher = f"""
        UNWIND $rows AS row
        MATCH (e:Entity:MilvusKB:`{label}` {{entity_id: row.entity_id}})
        SET e.attribute_names = row.names,
            e.attribute_values = row.values
        RETURN count(e) AS updated
        """

        def query(tx):
            record = tx.run(cypher, rows=rows).single()
            return int(record["updated"]) if record else 0

        return neo4j_write(self.driver, query)

    async def query_seed_subgraph(
        self,
        kb_id: str,
        *,
        entity_ids: list[str],
        max_nodes: int,
    ) -> dict[str, Any]:
        if not entity_ids:
            return {"nodes": [], "edges": []}
        seed_entity_ids = list(dict.fromkeys(entity_ids))
        label = safe_neo4j_label(kb_id)
        cypher = f"""
        MATCH (seed:Entity:MilvusKB:`{label}`)
        WHERE seed.entity_id IN $entity_ids
        MATCH p = (seed)-[*1..2]-(n:MilvusKB:`{label}`)
        WITH p LIMIT $path_limit
        WITH collect(p) AS paths
        UNWIND paths AS node_path
        UNWIND nodes(node_path) AS node
        WITH paths, collect(DISTINCT node) AS graph_nodes
        UNWIND paths AS rel_path
        UNWIND relationships(rel_path) AS rel
        RETURN graph_nodes AS nodes, collect(DISTINCT rel) AS edges
        """
        try:
            return await _run_neo4j_query_io(
                self._query_seed_subgraph_sync,
                kb_id,
                cypher,
                seed_entity_ids,
                max_nodes,
            )
        except Exception as e:
            logger.error(f"Milvus seed subgraph query failed: {e}")
            return {"nodes": [], "edges": []}

    def _query_seed_subgraph_sync(
        self,
        kb_id: str,
        cypher: str,
        entity_ids: list[str],
        max_nodes: int,
    ) -> dict[str, Any]:
        with self.driver.session() as session:
            record = session.run(
                cypher,
                entity_ids=entity_ids,
                path_limit=max(max_nodes, 1) * 4,
            ).single()
            if not record:
                return {"nodes": [], "edges": []}
            return self._process_subgraph_record(record, max_nodes, kb_id)

    async def query_and_rank_chunks_by_ppr(
        self,
        kb_id: str,
        seed_weights: dict[str, float],
        *,
        max_nodes: int,
        top_k: int,
        damping: float,
    ) -> list[tuple[str, float]]:
        if not seed_weights:
            return []
        subgraph = await self.query_seed_subgraph(
            kb_id,
            entity_ids=list(seed_weights.keys()),
            max_nodes=max_nodes,
        )
        return self.rank_chunks_by_ppr(subgraph, seed_weights, top_k=top_k, damping=damping)

    @staticmethod
    def rank_chunks_by_ppr(
        subgraph: dict[str, Any],
        seed_weights: dict[str, float],
        *,
        top_k: int,
        damping: float,
    ) -> list[tuple[str, float]]:
        nodes = subgraph.get("nodes") or []
        edges = subgraph.get("edges") or []
        if not nodes:
            return []

        import networkx as nx

        node_ids = [node["id"] for node in nodes]
        index_by_id = {node_id: index for index, node_id in enumerate(node_ids)}
        edge_indices = [
            (index_by_id[edge["source_id"]], index_by_id[edge["target_id"]])
            for edge in edges
            if edge.get("source_id") in index_by_id and edge.get("target_id") in index_by_id
        ]
        if not edge_indices:
            return []

        # networkx 节点 id 直接用节点索引（与 igraph 的 reset 向量语义对齐）。
        graph = nx.Graph()
        graph.add_nodes_from(range(len(nodes)))
        graph.add_edges_from(edge_indices)
        reset = [0.0] * len(nodes)
        chunk_node_indexes: list[tuple[int, str]] = []
        for index, node in enumerate(nodes):
            properties = node.get("properties") or {}
            if node.get("type") == "Chunk" and properties.get("chunk_id"):
                chunk_node_indexes.append((index, properties["chunk_id"]))
                continue
            entity_id = properties.get("entity_id")
            if entity_id in seed_weights:
                reset[index] = seed_weights[entity_id]

        reset_total = sum(reset)
        if reset_total <= 0 or not chunk_node_indexes:
            return []
        reset = [value / reset_total for value in reset]
        # networkx 的 personalization 必须覆盖全部节点（缺节点会抛错），0 值节点贡献为 0；
        # 默认 dangling=None 时跳转分布使用 personalization，与 igraph 的 reset 语义一致。
        personalization = {index: value for index, value in enumerate(reset)}
        scores = nx.pagerank(graph, alpha=min(max(damping, 0.1), 0.99), personalization=personalization)
        ranked = sorted(
            ((chunk_id, float(scores[index])) for index, chunk_id in chunk_node_indexes),
            key=lambda item: item[1],
            reverse=True,
        )
        return ranked[:top_k]

    async def get_labels(self, kb_id: str | None = None) -> list[str]:
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return []
        label = safe_neo4j_label(effective_kb_id)

        cypher = f"""
        MATCH (n:MilvusKB:`{label}`)
        UNWIND labels(n) AS node_label
        WITH DISTINCT node_label
        WHERE node_label <> 'MilvusKB' AND node_label <> $kb_id
        RETURN node_label
        ORDER BY node_label
        """
        try:
            records = await _run_neo4j_query_io(self._get_labels_sync, cypher, effective_kb_id)
            return [record["node_label"] for record in records]
        except Exception as e:
            logger.error(f"Failed to get Milvus graph labels: {e}")
            return []

    def _get_labels_sync(self, cypher: str, kb_id: str) -> list[Any]:
        return neo4j_read(self.driver, cypher, kb_id=kb_id)

    async def get_stats(self, kb_id: str | None = None) -> dict[str, Any]:
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return {"total_nodes": 0, "total_edges": 0, "entity_types": []}
        label = safe_neo4j_label(effective_kb_id)

        stats_cypher = f"""
        MATCH (n:MilvusKB:`{label}`)
        WITH count(n) AS node_count
        OPTIONAL MATCH (:MilvusKB:`{label}`)-[r]->(:MilvusKB:`{label}`)
        RETURN node_count, count(r) AS edge_count
        """
        label_cypher = f"""
        MATCH (n:Entity:MilvusKB:`{label}`)
        WITH n.label AS entity_label, count(*) AS count
        RETURN entity_label, count
        ORDER BY count DESC
        """
        try:
            return await _run_neo4j_query_io(self._get_stats_sync, stats_cypher, label_cypher)
        except Exception as e:
            logger.error(f"Failed to get Milvus graph stats: {e}")
            return {"total_nodes": 0, "total_edges": 0, "entity_types": []}

    def _get_stats_sync(self, stats_cypher: str, label_cypher: str) -> dict[str, Any]:
        with self.driver.session() as session:
            stats = session.run(stats_cypher).single()
            label_stats = session.run(label_cypher)
            return {
                "total_nodes": stats["node_count"] if stats else 0,
                "total_edges": stats["edge_count"] if stats else 0,
                "entity_types": [{"type": row["entity_label"], "count": row["count"]} for row in label_stats],
            }

    async def _get_milvus_kb(self, kb_id: str):
        kb = await self.kb_repo.get_by_kb_id(kb_id)
        if kb is None:
            raise ValueError(f"知识库 {kb_id} 不存在")
        if (kb.kb_type or "").lower() != "milvus":
            raise ValueError("仅 Milvus 知识库支持独立图谱构建")
        return kb

    def _get_locked_config(self, additional_params: dict[str, Any]) -> dict[str, Any]:
        config = additional_params.get(GRAPH_CONFIG_KEY) or {}
        if not config.get("locked"):
            raise ValueError("请先确认并锁定图谱抽取配置")
        if not config.get("extractor_type"):
            raise ValueError("图谱抽取配置缺少 extractor_type")
        return config

    def _public_config(self, config: dict[str, Any]) -> dict[str, Any] | None:
        if not config:
            return None
        return {
            "locked": bool(config.get("locked")),
            "extractor_type": config.get("extractor_type"),
            "extractor_options": self._runtime_extractor_options(config),
            "created_at": config.get("created_at"),
            "created_by": config.get("created_by"),
            "updated_at": config.get("updated_at"),
            "updated_by": config.get("updated_by"),
        }

    @staticmethod
    def _build_relation_predicates(filters: dict[str, Any]) -> list[str]:
        """把关系类筛选条件转成作用于单条 RELATION 边的完整布尔表达式。

        每条表达式自带 all(v IN $list ...) 包装：同类条件之间是 AND——每条关系都必须满足全部条件。
        """
        predicates: list[str] = []
        if filters.get("relation_types"):
            predicates.append("r.type IN $relation_types")
        relation_attributes = filters.get("relation_attributes") or []
        # 未处理的名字必须显式失败：静默丢掉条件会返回「看起来成功但没有筛选」的结果。
        unhandled_names = sorted(
            {condition["name"] for condition in relation_attributes} - set(RELATION_ATTRIBUTE_NAMES)
        )
        if unhandled_names:
            raise ValueError(f"不支持的关系属性名：{'、'.join(unhandled_names)}")
        if any(condition["name"] == "text" for condition in relation_attributes):
            predicates.append(
                "all(v IN $relation_attr_text WHERE toLower(coalesce(r.text, '')) CONTAINS toLower(v))"
            )
        if any(condition["name"] == "extractor_type" for condition in relation_attributes):
            predicates.append("all(v IN $relation_attr_extractor_type WHERE r.extractor_type = v)")
        return predicates

    @staticmethod
    def _build_intermediate_predicates(filters: dict[str, Any]) -> list[str]:
        """把实体类筛选条件转成作用于单个中间实体的完整布尔表达式。"""
        predicates: list[str] = []
        if filters.get("intermediate_entities"):
            predicates.append("n.label IN $entity_types")
        if filters.get("entity_attributes"):
            # 平行数组按索引对应；无属性或未回填的实体 size 为 0（或为 null），any 为假，路径被排除。
            predicates.append(
                "all(a IN $entity_attributes WHERE any(i IN range(0, size(n.attribute_names) - 1)"
                " WHERE n.attribute_names[i] = a.name AND n.attribute_values[i] = a.value))"
            )
        return predicates

    @staticmethod
    def _build_subgraph_cypher(
        label: str,
        *,
        start_id: str | None,
        end_id: str | None,
        max_depth: int,
        filters: dict[str, Any],
    ) -> str:
        """拼装子图查询 Cypher：起点锚定（可选终点）加四类路径筛选。

        `label` 必须已由 safe_neo4j_label 处理；`max_depth` 由调用方保证在 1..MAX_SUBGRAPH_DEPTH。
        路径只沿 :RELATION 在 :Entity 之间走，Chunk 节点不会进入路径。
        """
        relation_predicates = MilvusGraphService._build_relation_predicates(filters)
        intermediate_predicates = MilvusGraphService._build_intermediate_predicates(filters)

        clauses: list[str] = []
        if relation_predicates:
            joined = " AND ".join(relation_predicates)
            clauses.append(f"all(r IN relationships(p) WHERE {joined})")
        if intermediate_predicates:
            # 起点豁免；填了终点时终点同样豁免，只约束真正的中间实体。
            exempt = "n = s OR n = e OR " if end_id else "n = s OR "
            joined = " AND ".join(intermediate_predicates)
            clauses.append(f"all(n IN nodes(p) WHERE {exempt}{joined})")

        where = f"\nWHERE {' AND '.join(clauses)}" if clauses else ""
        rel_pattern = f"[:RELATION*1..{max_depth} {{kb_id: $kb_id}}]"
        path_tail = """WITH p LIMIT $path_limit
WITH collect(p) AS paths
UNWIND paths AS node_path
UNWIND nodes(node_path) AS node
WITH paths, collect(DISTINCT node) AS graph_nodes
UNWIND paths AS rel_path
UNWIND relationships(rel_path) AS rel
WITH paths, graph_nodes, collect(DISTINCT rel) AS edges
RETURN [x IN graph_nodes WHERE x IS NOT NULL] AS nodes,
       edges,
       size(graph_nodes) > $max_nodes
         OR size(paths) >= $path_limit
         OR size(edges) > $max_nodes * 2 AS truncated"""

        if start_id and end_id:
            return f"""MATCH (s:Entity:MilvusKB:`{label}` {{entity_id: $start_id}})
MATCH (e:Entity:MilvusKB:`{label}` {{entity_id: $end_id}})
MATCH p = (s)-{rel_pattern}-(e){where}
{path_tail}"""

        if start_id:
            return f"""MATCH (s:Entity:MilvusKB:`{label}` {{entity_id: $start_id}})
MATCH p = (s)-{rel_pattern}-(m){where}
{path_tail}"""

        # 概览模式：种子是全部实体。`seeds` 必须逐段带下去并并回节点集合，
        # 否则没有任何 RELATION 的孤立实体会从结果里消失。
        overview_tail = """WITH seeds, collect(p) AS paths
UNWIND paths AS node_path
UNWIND nodes(node_path) AS node
WITH seeds, paths, collect(DISTINCT node) AS graph_nodes
UNWIND paths AS rel_path
UNWIND relationships(rel_path) AS rel
WITH seeds, paths, graph_nodes, collect(DISTINCT rel) AS edges
RETURN [x IN graph_nodes WHERE x IS NOT NULL] + [x IN seeds WHERE x IS NOT NULL] AS nodes,
       edges,
       size(graph_nodes) > $max_nodes
         OR size(paths) >= $path_limit
         OR size(edges) > $max_nodes * 2 AS truncated"""

        return f"""MATCH (seed:Entity:MilvusKB:`{label}`)
WITH seed LIMIT $max_nodes
WITH collect(seed) AS seeds
UNWIND seeds AS s
MATCH p = (s)-{rel_pattern}-(m){where}
{overview_tail}"""

    def _process_subgraph_record(self, record: Any, limit: int, kb_id: str) -> dict[str, Any]:
        nodes = []
        edges = []
        node_ids = set()
        edge_ids = set()

        for raw_node in record.get("nodes") or []:
            node = self._normalize_node(raw_node, kb_id)
            if not node or node["id"] in node_ids:
                continue
            nodes.append(node)
            node_ids.add(node["id"])
            if len(nodes) >= limit:
                break

        for raw_edge in record.get("edges") or []:
            edge = self._normalize_edge(raw_edge)
            if not edge or edge["id"] in edge_ids:
                continue
            if edge["source_id"] not in node_ids or edge["target_id"] not in node_ids:
                continue
            edges.append(edge)
            edge_ids.add(edge["id"])

        return self._finalize_subgraph_result(nodes, edges, limit)

    @staticmethod
    def _finalize_subgraph_result(
        nodes: list[dict[str, Any]], edges: list[dict[str, Any]], limit: int
    ) -> dict[str, Any]:
        limit = max(0, limit)
        final_nodes = nodes[:limit]
        node_ids = {node["id"] for node in final_nodes}
        final_edges = [
            edge for edge in edges if edge.get("source_id") in node_ids and edge.get("target_id") in node_ids
        ]
        return {"nodes": final_nodes, "edges": final_edges[: limit * 2]}

    def _normalize_node(self, raw_node: Any, kb_id: str | None = None) -> dict[str, Any]:
        if hasattr(raw_node, "element_id"):
            node_id = raw_node.element_id
            labels = list(raw_node.labels)
            properties = dict(raw_node.items())
        elif isinstance(raw_node, dict):
            node_id = raw_node.get("id") or raw_node.get("element_id")
            labels = raw_node.get("labels", [])
            properties = raw_node.get("properties") or {k: v for k, v in raw_node.items() if k not in {"id", "labels"}}
        else:
            return {}

        effective_kb_id = kb_id or self.kb_id
        db_label = properties.get("kb_id") or effective_kb_id
        filtered_labels = [label for label in labels if label not in {"MilvusKB", db_label}]
        entity_type = "Chunk" if "Chunk" in labels else properties.get("label", "Entity")
        name = properties.get("name") or properties.get("content_preview") or properties.get("chunk_id") or "Unknown"
        return {
            "id": node_id,
            "name": name,
            "original_id": node_id,
            "type": entity_type,
            "labels": filtered_labels,
            "properties": properties,
            "normalized": {
                "name": name,
                "type": entity_type,
                "source": "milvus",
            },
            "graph_type": "milvus",
        }

    def _normalize_edge(self, raw_edge: Any) -> dict[str, Any]:
        if hasattr(raw_edge, "element_id"):
            edge_id = raw_edge.element_id
            edge_type = raw_edge.type
            source_id = raw_edge.start_node.element_id
            target_id = raw_edge.end_node.element_id
            properties = dict(raw_edge.items())
            edge_type = properties.get("type") or edge_type
        elif isinstance(raw_edge, dict):
            edge_id = raw_edge.get("id")
            edge_type = raw_edge.get("type")
            source_id = raw_edge.get("source_id")
            target_id = raw_edge.get("target_id")
            properties = raw_edge.get("properties", {})
        else:
            return {}

        return {
            "id": edge_id,
            "source_id": source_id,
            "target_id": target_id,
            "type": edge_type,
            "properties": properties,
            "normalized": {
                "type": edge_type,
                "direction": "directed",
            },
        }
