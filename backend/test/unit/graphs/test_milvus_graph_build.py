from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from yuxi.knowledge.graphs.extractors import (
    GraphExtractorFactory,
    LLMGraphExtractor,
    normalize_extraction_result,
)
from yuxi.knowledge.graphs.extractors.base import (
    MAX_ENTITY_LABEL_LENGTH,
    MAX_ENTITY_NAME_LENGTH,
    MAX_RELATION_TYPE_LENGTH,
)
from yuxi.knowledge.graphs.milvus_graph_service import (
    GraphAttributesNotReadyError,
    MilvusGraphService,
)
from yuxi.knowledge.graphs.milvus_graph_vector_store import MilvusGraphVectorStore
from yuxi.storage.postgres.models_knowledge import KnowledgeGraphEntity, KnowledgeGraphTriple


def _raw_graph_node(node_id: str, *, labels: list[str] | None = None, name: str | None = None) -> dict:
    return {
        "id": node_id,
        "labels": labels or ["MilvusKB", "Entity"],
        "properties": {"name": name or node_id, "kb_id": "kb_test"},
    }


def _raw_graph_edge(edge_id: str, source_id: str, target_id: str) -> dict:
    return {
        "id": edge_id,
        "type": "RELATED_TO",
        "source_id": source_id,
        "target_id": target_id,
        "properties": {},
    }


def test_normalize_extraction_result_defaults_and_validates_refs():
    result = normalize_extraction_result(
        {
            "entities": [{"text": "张三"}, {"text": "公司"}],
            "relations": [{"source": "张三", "target": "公司", "text": "任职于"}],
        },
        "llm",
    )

    assert result["entities"][0]["label"] == "Entity"
    assert result["relations"][0]["label"] == "RELATED_TO"
    assert result["relations"][0]["source"] == {"text": "张三", "label": "Entity", "attributes": []}
    assert result["metadata"] == {"extractor_type": "llm", "schema_version": 1}


def test_normalize_extraction_result_accepts_llm_nested_relation_entities():
    result = normalize_extraction_result(
        {
            "relations": [
                {
                    "source": {
                        "text": "张三",
                        "label": "Person",
                        "attributes": [{"text": "工程师", "label": "Occupation"}],
                    },
                    "target": {"text": "公司", "label": "Organization"},
                    "text": "任职于",
                    "label": "WORKS_AT",
                }
            ]
        },
        "llm",
    )

    assert result["entities"] == [
        {"text": "张三", "label": "Person", "attributes": [{"text": "工程师", "label": "Occupation"}]},
        {"text": "公司", "label": "Organization", "attributes": []},
    ]
    assert result["relations"][0]["source"]["attributes"] == [{"text": "工程师", "label": "Occupation"}]
    assert result["relations"][0]["target"] == {"text": "公司", "label": "Organization", "attributes": []}


# 真实案例的形状：模型把一整段摘要正文当成了实体。原文 1672 字符，而列是 varchar(512)。
ABSTRACT_PARAGRAPH = (
    "Reliable engine-weight estimation at the conceptual design stage is critical to the "
    "development of new aircraft engines. It helps to identify the best engine concept "
    "amongst several candidates. "
) * 6


def test_normalize_extraction_result_drops_over_long_entity_and_its_relations():
    """超长实体必须被丢弃，而不是让同一批的好实体一起陪葬。

    原来做不到这一点：upsert_chunk_graph 把一个分块的所有实体放进一条多行 INSERT，
    一个值超过列长度，整批被 PostgreSQL 拒绝，该分块的图谱全部丢失且永远停在 pending。
    """
    result = normalize_extraction_result(
        {
            "entities": [
                {"text": "WATE", "label": "Tool"},
                {"text": ABSTRACT_PARAGRAPH, "label": "Abstract"},
                {"text": "NASA", "label": "Organization"},
            ],
            "relations": [
                {"source": "WATE", "target": "NASA", "text": "由...开发", "label": "DEVELOPED_BY"},
                {"source": "WATE", "target": ABSTRACT_PARAGRAPH, "text": "has abstract"},
            ],
        },
        "llm",
    )

    assert [entity["text"] for entity in result["entities"]] == ["WATE", "NASA"]
    assert [(relation["source"]["text"], relation["target"]["text"]) for relation in result["relations"]] == [
        ("WATE", "NASA")
    ]
    assert result["metadata"]["dropped_entities"] == 1
    assert result["metadata"]["dropped_relations"] == 1


def test_normalize_extraction_result_drops_over_long_inline_endpoint():
    """关系以内联对象给出实体时，只丢这一条关系，其余照常。"""
    result = normalize_extraction_result(
        {
            "relations": [
                {
                    "source": {"text": "WATE", "label": "Tool"},
                    "target": {"text": ABSTRACT_PARAGRAPH, "label": "Abstract"},
                    "text": "has abstract",
                },
                {
                    "source": {"text": "WATE", "label": "Tool"},
                    "target": {"text": "NASA", "label": "Organization"},
                    "text": "由...开发",
                },
            ]
        },
        "llm",
    )

    assert [entity["text"] for entity in result["entities"]] == ["WATE", "NASA"]
    assert len(result["relations"]) == 1
    assert result["metadata"]["dropped_relations"] == 1


def test_normalize_extraction_result_drops_over_long_label_and_relation_type():
    over_long_label = normalize_extraction_result(
        {"entities": [{"text": "WATE", "label": "X" * (MAX_ENTITY_LABEL_LENGTH + 1)}], "relations": []},
        "llm",
    )
    assert over_long_label["entities"] == []

    over_long_relation_type = normalize_extraction_result(
        {
            "entities": [{"text": "A"}, {"text": "B"}],
            "relations": [
                {"source": "A", "target": "B", "text": "r", "label": "T" * (MAX_RELATION_TYPE_LENGTH + 1)}
            ],
        },
        "llm",
    )
    assert [entity["text"] for entity in over_long_relation_type["entities"]] == ["A", "B"]
    assert over_long_relation_type["relations"] == []


def test_normalize_extraction_result_always_fits_entity_columns():
    """不变量：规范化后的实体名不会再超过列长度——写入失败的根因就是这个。"""
    result = normalize_extraction_result(
        {"entities": [{"text": ABSTRACT_PARAGRAPH}, {"text": "长" * 600}, {"text": "ok"}], "relations": []},
        "llm",
    )

    assert [entity["text"] for entity in result["entities"]] == ["ok"]
    assert all(len(entity["text"]) <= MAX_ENTITY_NAME_LENGTH for entity in result["entities"])


def test_normalize_extraction_result_counts_dropped_entities_distinctly():
    """同一个越界实体被多条关系引用时，实体数按去重计，关系数按条计。"""
    result = normalize_extraction_result(
        {
            "entities": [{"text": "A"}, {"text": "B"}, {"text": ABSTRACT_PARAGRAPH}],
            "relations": [
                {"source": "A", "target": ABSTRACT_PARAGRAPH, "text": "r1"},
                {"source": "B", "target": ABSTRACT_PARAGRAPH, "text": "r2"},
            ],
        },
        "llm",
    )

    assert [entity["text"] for entity in result["entities"]] == ["A", "B"]
    assert result["relations"] == []
    assert result["metadata"]["dropped_entities"] == 1
    assert result["metadata"]["dropped_relations"] == 2


def test_normalize_extraction_result_drops_relation_with_over_long_ref():
    """关系只以裸字符串引用一个超长名（未出现在 entities[]）时，丢弃该关系而不是让整块失败。"""
    result = normalize_extraction_result(
        {
            "entities": [{"text": "A"}],
            "relations": [{"source": "A", "target": ABSTRACT_PARAGRAPH, "text": "r"}],
        },
        "llm",
    )

    assert [entity["text"] for entity in result["entities"]] == ["A"]
    assert result["relations"] == []
    assert result["metadata"]["dropped_relations"] == 1


def test_normalize_extraction_result_keeps_metadata_clean_without_drops():
    """没有丢弃时不写这两个键，避免改变既有产物的形状。"""
    result = normalize_extraction_result({"entities": [{"text": "A"}], "relations": []}, "llm")

    assert result["metadata"] == {"extractor_type": "llm", "schema_version": 1}


def test_normalize_extraction_result_still_rejects_unknown_ref():
    """丢弃只针对越界实体；引用一个从未出现过的实体仍然是错误，不能被一并静默吞掉。"""
    with pytest.raises(ValueError, match="未找到"):
        normalize_extraction_result(
            {"entities": [{"text": "A"}], "relations": [{"source": "A", "target": "不存在", "text": "r"}]},
            "llm",
        )


def test_normalizer_limits_match_graph_column_lengths():
    """守卫常量必须与列定义一致：列更短则守卫失效，列更长则白丢数据。"""
    assert MAX_ENTITY_NAME_LENGTH == KnowledgeGraphEntity.name.type.length
    assert MAX_ENTITY_NAME_LENGTH == KnowledgeGraphEntity.normalized_name.type.length
    assert MAX_ENTITY_LABEL_LENGTH == KnowledgeGraphEntity.label.type.length
    assert MAX_RELATION_TYPE_LENGTH == KnowledgeGraphTriple.relation_type.type.length


@pytest.mark.parametrize(
    "payload",
    [
        {"entities": [{"text": "张三"}], "relations": [{"source": "张三", "target": "不存在", "text": "关系"}]},
        {"entities": [{"text": ""}], "relations": []},
    ],
)
def test_normalize_extraction_result_rejects_invalid_payload(payload):
    with pytest.raises(ValueError):
        normalize_extraction_result(payload, "llm")


def test_llm_graph_extractor_rejects_custom_prompt():
    extractor = LLMGraphExtractor({"model_spec": "test/model", "prompt": "custom"})

    with pytest.raises(ValueError, match="不支持自定义完整 Prompt"):
        extractor.validate_options()


def test_llm_graph_extractor_defaults_extraction_timeout():
    """不配置时保持历史默认值，避免改变既有部署的行为。"""
    extractor = LLMGraphExtractor({"model_spec": "test/model"})

    assert extractor._resolve_timeout_seconds() == 60.0


@pytest.mark.asyncio
async def test_llm_graph_extractor_passes_configured_timeout_to_model(monkeypatch):
    """抽取超时必须真的传到模型调用上。

    它不能走 model_params：select_model 会把显式 timeout 覆盖到 model_params 之上，
    所以在 model_params 里写 timeout 是无效的，只能由抽取器显式传入。
    """
    captured = {}

    class FakeModel:
        async def call(self, prompt, stream=False):
            return SimpleNamespace(content='{"relations": []}')

    def fake_select_model(**kwargs):
        captured.update(kwargs)
        return FakeModel()

    monkeypatch.setattr("yuxi.knowledge.graphs.extractors.llm.select_model", fake_select_model)
    extractor = LLMGraphExtractor({"model_spec": "test/model", "timeout_seconds": 300})

    await extractor.extract("某型发动机的涵道比设计值为 9.0")

    assert captured["timeout"] == 300.0
    assert captured["model_spec"] == "test/model"


@pytest.mark.asyncio
async def test_llm_graph_extractor_defaults_timeout_when_absent(monkeypatch):
    """不配置超时时，默认值也必须真的传到模型调用上（与硬编码 60s 的旧行为等价）。"""
    captured = {}

    class FakeModel:
        async def call(self, prompt, stream=False):
            return SimpleNamespace(content='{"relations": []}')

    def fake_select_model(**kwargs):
        captured.update(kwargs)
        return FakeModel()

    monkeypatch.setattr("yuxi.knowledge.graphs.extractors.llm.select_model", fake_select_model)

    await LLMGraphExtractor({"model_spec": "test/model"}).extract("文本")

    assert captured["timeout"] == 60.0


@pytest.mark.parametrize("value", [300, "300", 600, 0.5])
def test_llm_graph_extractor_accepts_valid_timeout(value):
    """上界 600 与字符串数字都应当被接受（字符串数字与既有 concurrency_count 风格一致）。"""
    extractor = LLMGraphExtractor({"model_spec": "test/model", "timeout_seconds": value})

    extractor.validate_options()

    assert extractor._resolve_timeout_seconds() == float(value)


@pytest.mark.parametrize(
    "bad_value",
    [
        0,
        -1,
        600.0001,
        "abc",
        None,
        "",  # 空字符串
        True,  # bool 是 int 子类，float(True)==1.0 会被区间校验放行
        False,
        float("nan"),  # NaN 让区间比较全部为 False
        "NaN",
        float("inf"),
        10**400,  # 超大整数在 float() 上抛 OverflowError
    ],
    ids=[
        "zero",
        "negative",
        "above_max",
        "text",
        "null",
        "empty",
        "true",
        "false",
        "nan",
        "nan_str",
        "inf",
        "huge_int",
    ],
)
def test_llm_graph_extractor_rejects_invalid_timeout(bad_value):
    """越界、非数字、非有限值与布尔要显式失败。

    这几类如果被静默放行，会变成「配置保存成功、构建时整库全挂」：bool 会配出 1 秒超时，
    NaN/inf 会让每次调用直接报错，超大整数则会在路由兜底分支变成 500 而非 400。
    """
    extractor = LLMGraphExtractor({"model_spec": "test/model", "timeout_seconds": bad_value})

    with pytest.raises(ValueError, match="timeout_seconds"):
        extractor.validate_options()


def test_llm_graph_extractor_rejects_top_level_enable_thinking():
    """顶层 enable_thinking 会被当成 create() 的未知参数，保存配置时就要报错。"""
    extractor = LLMGraphExtractor({"model_spec": "test/model", "model_params": {"enable_thinking": False}})

    with pytest.raises(ValueError, match="extra_body"):
        extractor.validate_options()


def test_llm_graph_extractor_accepts_enable_thinking_in_extra_body():
    extractor = LLMGraphExtractor(
        {"model_spec": "test/model", "model_params": {"extra_body": {"enable_thinking": False}}}
    )

    extractor.validate_options()


def test_llm_graph_extractor_appends_schema_to_fixed_prompt():
    extractor = LLMGraphExtractor(
        {
            "model_spec": "test/model",
            "schema": "实体类型只能是 Person 或 Organization",
            "concurrency_count": 5,
            "model_params": {"temperature": 0.1},
        }
    )

    prompt = extractor._build_prompt("张三任职于公司")

    assert "请从下面文本中抽取实体和实体关系" in prompt
    assert "抽取 Schema 约束" in prompt
    assert "实体类型只能是 Person 或 Organization" in prompt
    assert "文本：\n张三任职于公司" in prompt


def test_graph_extractor_factory_supports_only_llm_and_rejects_spacy():
    assert GraphExtractorFactory.supported_types() == ["llm"]

    with pytest.raises(ValueError, match="spacy"):
        GraphExtractorFactory.create("spacy", {"model": "zh_core_web_sm"})


@pytest.mark.asyncio
async def test_graph_extraction_retries_twice_before_third_attempt_succeeds(monkeypatch):
    chunk = SimpleNamespace(
        chunk_id="chunk_1",
        file_id="file_1",
        chunk_index=0,
        content="张三任职于公司",
        extraction_result=None,
        graph_extraction_details={"status": "failed", "attempt_count": 3},
    )
    extractor = SimpleNamespace(
        extractor_type="llm",
        extract=AsyncMock(
            side_effect=[
                RuntimeError("timeout"),
                ValueError("invalid json"),
                {"entities": [], "relations": []},
            ]
        ),
    )
    chunk_repo = SimpleNamespace(
        mark_graph_extraction_pending=AsyncMock(),
        mark_graph_extraction_failed=AsyncMock(),
        update_extraction_result=AsyncMock(),
    )
    service = MilvusGraphService(chunk_repo=chunk_repo)
    monkeypatch.setattr(
        "yuxi.knowledge.graphs.milvus_graph_service.GRAPH_EXTRACTION_RETRY_DELAYS_SECONDS",
        (0, 0),
    )

    result = await service._get_chunk_extraction_result("kb_test", chunk, extractor)

    assert result["metadata"]["extractor_type"] == "llm"
    assert extractor.extract.await_count == 3
    chunk_repo.mark_graph_extraction_pending.assert_awaited_once_with("chunk_1")
    chunk_repo.update_extraction_result.assert_awaited_once_with("chunk_1", result, 3)
    chunk_repo.mark_graph_extraction_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_build_keeps_extraction_concurrency_full_while_writes_are_blocked(monkeypatch):
    chunks = [
        SimpleNamespace(
            id=index,
            chunk_id=f"chunk_{index}",
            file_id="file_1",
            kb_id="kb_test",
            chunk_index=index,
            content=f"content {index}",
            start_char_pos=0,
            end_char_pos=10,
            extraction_result=None,
            graph_extraction_details={"status": "pending", "attempt_count": 0},
            graph_indexed=False,
        )
        for index in range(1, 7)
    ]
    chunks[0].extraction_result = {"entities": [], "relations": []}
    chunks[0].graph_extraction_details = {"status": "succeeded"}
    kb = SimpleNamespace(
        kb_type="milvus",
        embedding_model_spec="test/embedding",
        additional_params={
            "graph_build_config": {
                "locked": True,
                "extractor_type": "llm",
                "extractor_options": {"model_spec": "test/model", "concurrency_count": 2},
            }
        },
    )
    all_extractions_finished = asyncio.Event()
    release_writes = asyncio.Event()

    class Repo:
        async def get_by_kb_id(self, kb_id):
            return kb

    class ChunkRepo:
        async def count_graph_pending_by_kb_id(self, kb_id):
            return sum(not chunk.graph_indexed for chunk in chunks)

        async def count_graph_indexed_by_kb_id(self, kb_id):
            return sum(chunk.graph_indexed for chunk in chunks)

        async def count_graph_extraction_statuses_by_kb_id(self, kb_id):
            counts = {"pending": 0, "succeeded": 0, "failed": 0}
            for chunk in chunks:
                counts[chunk.graph_extraction_details["status"]] += 1
            return counts

        async def list_graph_pending_by_kb_id(self, kb_id, limit, *, after_id=0):
            return [chunk for chunk in chunks if chunk.id > after_id and not chunk.graph_indexed][:limit]

        async def update_extraction_result(self, chunk_id, extraction_result, attempt_count=1):
            chunk = next(chunk for chunk in chunks if chunk.chunk_id == chunk_id)
            chunk.extraction_result = extraction_result
            chunk.graph_extraction_details = {"status": "succeeded", "attempt_count": attempt_count}

        async def mark_graph_extraction_pending(self, chunk_id):
            chunk = next(chunk for chunk in chunks if chunk.chunk_id == chunk_id)
            chunk.graph_extraction_details = {"status": "pending", "attempt_count": 0}

        async def mark_graph_extraction_failed(self, chunk_id, attempt_count, error):
            chunk = next(chunk for chunk in chunks if chunk.chunk_id == chunk_id)
            chunk.graph_extraction_details = {
                "status": "failed",
                "attempt_count": attempt_count,
                "last_error": error,
            }

        async def get_by_chunk_id(self, chunk_id):
            return next(chunk for chunk in chunks if chunk.chunk_id == chunk_id)

        async def mark_graph_indexed(self, chunk_id, ent_ids=None):
            chunk = next(chunk for chunk in chunks if chunk.chunk_id == chunk_id)
            chunk.graph_indexed = True

        async def mark_graph_structure_indexed(self, chunk_id, ent_ids=None):
            chunk = next(chunk for chunk in chunks if chunk.chunk_id == chunk_id)
            chunk.graph_structure_indexed = True

    class Extractor:
        extractor_type = "llm"
        active = 0
        max_active = 0
        calls = 0
        completed = 0

        async def extract(self, text, *, chunk_metadata=None):
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                if text == "content 6":
                    raise RuntimeError("extract failed")
                return {"entities": [], "relations": []}
            finally:
                self.active -= 1
                self.completed += 1
                if self.completed == 7:
                    all_extractions_finished.set()

    extractor = Extractor()
    monkeypatch.setattr(GraphExtractorFactory, "create", lambda extractor_type, options: extractor)
    monkeypatch.setattr(
        "yuxi.knowledge.graphs.milvus_graph_service.GRAPH_EXTRACTION_RETRY_DELAYS_SECONDS",
        (0, 0),
    )

    async def wait_for_writes(**kwargs):
        await release_writes.wait()

    class GraphRepo:
        upsert_chunk_graph = AsyncMock(side_effect=wait_for_writes)

        async def claim_vector_records(self, **kwargs):
            return "token", []

        async def count_vector_statuses_by_kb_id(self, kb_id):
            return {"pending": 0, "processing": 0, "indexed": 0, "failed": 0}

        async def finalize_graph_indexed_chunks(self, kb_id):
            finalized = 0
            for chunk in chunks:
                if getattr(chunk, "graph_structure_indexed", False) and not chunk.graph_indexed:
                    chunk.graph_indexed = True
                    finalized += 1
            return finalized

    graph_repo = GraphRepo()
    graph_vector_store = SimpleNamespace(upsert_graph_records=AsyncMock())
    service = MilvusGraphService(
        kb_repo=Repo(),
        chunk_repo=ChunkRepo(),
        graph_repo=graph_repo,
        graph_vector_store=graph_vector_store,
    )
    monkeypatch.setattr(service, "write_chunk_graph", lambda kb_id, chunk, result: ([], []))

    build_task = asyncio.create_task(service.build_pending_chunks("kb_test"))
    await asyncio.wait_for(all_extractions_finished.wait(), timeout=1)

    assert extractor.max_active == 2
    assert extractor.calls == 7
    assert not build_task.done()

    release_writes.set()
    result = await asyncio.wait_for(build_task, timeout=1)

    assert result["success"] == 5
    assert result["extraction_failed"] == 1
    assert result["remaining"] == 1
    assert chunks[-1].graph_extraction_details["status"] == "failed"
    assert chunks[-1].graph_extraction_details["attempt_count"] == 3


@pytest.mark.asyncio
async def test_graph_build_cancellation_stops_backpressured_extraction_queue(monkeypatch):
    chunks = [
        SimpleNamespace(
            id=index,
            chunk_id=f"chunk_{index}",
            file_id="file_1",
            kb_id="kb_test",
            chunk_index=index,
            content=f"content {index}",
            extraction_result=None,
            graph_indexed=False,
        )
        for index in range(1, 10)
    ]
    kb = SimpleNamespace(
        kb_type="milvus",
        embedding_model_spec="test/embedding",
        additional_params={
            "graph_build_config": {
                "locked": True,
                "extractor_type": "llm",
                "extractor_options": {"model_spec": "test/model", "concurrency_count": 1},
            }
        },
    )
    extraction_started = asyncio.Event()
    never_finish = asyncio.Event()

    class Repo:
        async def get_by_kb_id(self, kb_id):
            return kb

    class ChunkRepo:
        async def count_graph_pending_by_kb_id(self, kb_id):
            return len(chunks)

        async def count_graph_indexed_by_kb_id(self, kb_id):
            return 0

        async def list_graph_pending_by_kb_id(self, kb_id, limit, *, after_id=0):
            return [chunk for chunk in chunks if chunk.id > after_id][:limit]

    class Extractor:
        extractor_type = "llm"

        async def extract(self, text, *, chunk_metadata=None):
            extraction_started.set()
            await never_finish.wait()

    class Context:
        cancel_requested = False

        async def raise_if_cancelled(self):
            if self.cancel_requested:
                raise asyncio.CancelledError

        async def set_progress(self, progress, message):
            return None

    context = Context()
    monkeypatch.setattr(GraphExtractorFactory, "create", lambda extractor_type, options: Extractor())
    service = MilvusGraphService(
        kb_repo=Repo(),
        chunk_repo=ChunkRepo(),
        graph_repo=SimpleNamespace(),
        graph_vector_store=SimpleNamespace(),
    )

    build_task = asyncio.create_task(service.build_pending_chunks("kb_test", context=context))
    await asyncio.wait_for(extraction_started.wait(), timeout=1)
    context.cancel_requested = True

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(build_task, timeout=1.5)


@pytest.mark.asyncio
async def test_graph_build_indexes_vectors_after_structure_write(monkeypatch):
    chunk = SimpleNamespace(
        id=1,
        chunk_id="chunk_1",
        file_id="file_1",
        kb_id="kb_test",
        chunk_index=1,
        content="张三任职于公司",
        start_char_pos=0,
        end_char_pos=8,
        extraction_result=normalize_extraction_result(
            {
                "relations": [
                    {
                        "source": {"text": "张三", "label": "Person"},
                        "target": {"text": "公司", "label": "Organization"},
                        "text": "任职于",
                        "label": "WORKS_AT",
                    }
                ]
            },
            "llm",
        ),
        graph_structure_indexed=False,
        graph_indexed=False,
    )
    kb = SimpleNamespace(
        kb_type="milvus",
        embedding_model_spec="test/embedding",
        additional_params={
            "graph_build_config": {
                "locked": True,
                "extractor_type": "llm",
                "extractor_options": {"model_spec": "test/model", "concurrency_count": 2},
            }
        },
    )

    class ChunkRepo:
        async def count_graph_pending_by_kb_id(self, kb_id):
            return int(not chunk.graph_indexed)

        async def count_graph_indexed_by_kb_id(self, kb_id):
            return int(chunk.graph_indexed)

        async def count_graph_extraction_statuses_by_kb_id(self, kb_id):
            return {"pending": 0, "succeeded": 1, "failed": 0}

        async def list_graph_pending_by_kb_id(self, kb_id, limit, *, after_id=0):
            return [chunk] if chunk.id > after_id and not chunk.graph_indexed else []

        async def get_by_chunk_id(self, chunk_id):
            return chunk

        async def mark_graph_structure_indexed(self, chunk_id, ent_ids):
            chunk.graph_structure_indexed = True

    class GraphRepo:
        def __init__(self):
            self.pending = False
            self.indexed = False

        async def upsert_chunk_graph(self, **kwargs):
            self.pending = True

        async def claim_vector_records(self, *, record_type, **kwargs):
            if record_type == "entity" and self.pending:
                self.pending = False
                return "token", [{"id": "entity_1", "content": "张三"}]
            return "token", []

        async def mark_vector_records_indexed(self, **kwargs):
            self.indexed = True

        async def mark_vector_records_failed(self, **kwargs):
            raise AssertionError("vector indexing should succeed")

        async def count_vector_statuses_by_kb_id(self, kb_id):
            return {
                "pending": int(self.pending),
                "processing": 0,
                "indexed": int(self.indexed),
                "failed": 0,
            }

        async def finalize_graph_indexed_chunks(self, kb_id):
            if chunk.graph_structure_indexed and self.indexed:
                chunk.graph_indexed = True
                return 1
            return 0

    graph_repo = GraphRepo()
    vector_store = SimpleNamespace(upsert_graph_records=AsyncMock())
    service = MilvusGraphService(
        kb_repo=SimpleNamespace(get_by_kb_id=AsyncMock(return_value=kb)),
        chunk_repo=ChunkRepo(),
        graph_repo=graph_repo,
        graph_vector_store=vector_store,
    )
    monkeypatch.setattr(
        service,
        "write_chunk_graph",
        lambda kb_id, chunk, result: ([{"entity_id": "entity_1"}], []),
    )

    result = await service.build_pending_chunks("kb_test")

    assert result == {
        "kb_id": "kb_test",
        "success": 1,
        "failed": 0,
        "extraction_failed": 0,
        "write_failed": 0,
        "remaining": 0,
        "vector_failed": 0,
    }
    vector_store.upsert_graph_records.assert_awaited_once()
    assert chunk.graph_structure_indexed is True
    assert chunk.graph_indexed is True


@pytest.mark.asyncio
async def test_graph_build_fails_after_three_vector_attempts(monkeypatch):
    chunk = SimpleNamespace(
        id=1,
        chunk_id="chunk_1",
        file_id="file_1",
        kb_id="kb_test",
        chunk_index=1,
        content="content",
        start_char_pos=0,
        end_char_pos=7,
        extraction_result={"entities": [], "relations": [], "metadata": {"extractor_type": "llm"}},
        graph_structure_indexed=False,
        graph_indexed=False,
    )
    kb = SimpleNamespace(
        kb_type="milvus",
        embedding_model_spec="test/embedding",
        additional_params={
            "graph_build_config": {
                "locked": True,
                "extractor_type": "llm",
                "extractor_options": {"model_spec": "test/model", "concurrency_count": 1},
            }
        },
    )

    class ChunkRepo:
        async def count_graph_pending_by_kb_id(self, kb_id):
            return 1

        async def count_graph_indexed_by_kb_id(self, kb_id):
            return 0

        async def count_graph_extraction_statuses_by_kb_id(self, kb_id):
            return {"pending": 0, "succeeded": 1, "failed": 0}

        async def list_graph_pending_by_kb_id(self, kb_id, limit, *, after_id=0):
            return [chunk] if chunk.id > after_id else []

        async def get_by_chunk_id(self, chunk_id):
            return chunk

        async def mark_graph_structure_indexed(self, chunk_id, ent_ids):
            chunk.graph_structure_indexed = True

    class GraphRepo:
        attempts = 0
        status = "pending"

        async def upsert_chunk_graph(self, **kwargs):
            return None

        async def claim_vector_records(self, *, record_type, **kwargs):
            if record_type != "entity" or self.status != "pending":
                return "token", []
            self.status = "processing"
            self.attempts += 1
            return "token", [{"id": "entity_1", "content": "entity"}]

        async def mark_vector_records_indexed(self, **kwargs):
            raise AssertionError("vector indexing should fail")

        async def mark_vector_records_failed(self, **kwargs):
            self.status = "failed" if self.attempts >= 3 else "pending"

        async def count_vector_statuses_by_kb_id(self, kb_id):
            return {
                "pending": int(self.status == "pending"),
                "processing": int(self.status == "processing"),
                "indexed": 0,
                "failed": int(self.status == "failed"),
            }

        async def finalize_graph_indexed_chunks(self, kb_id):
            return 0

    graph_repo = GraphRepo()
    service = MilvusGraphService(
        kb_repo=SimpleNamespace(get_by_kb_id=AsyncMock(return_value=kb)),
        chunk_repo=ChunkRepo(),
        graph_repo=graph_repo,
        graph_vector_store=SimpleNamespace(upsert_graph_records=AsyncMock(side_effect=RuntimeError("embed failed"))),
    )
    monkeypatch.setattr(service, "write_chunk_graph", lambda kb_id, chunk, result: ([{"entity_id": "e"}], []))

    with pytest.raises(RuntimeError, match="vector_failed=1"):
        await service.build_pending_chunks("kb_test")

    assert graph_repo.attempts == 3


@pytest.mark.asyncio
async def test_graph_reconcile_skips_completed_structure(monkeypatch):
    chunk = SimpleNamespace(
        id=1,
        chunk_id="chunk_1",
        graph_structure_indexed=True,
        graph_indexed=False,
        extraction_result={"entities": [], "relations": []},
    )
    kb = SimpleNamespace(
        kb_type="milvus",
        embedding_model_spec="test/embedding",
        additional_params={
            "graph_build_config": {
                "locked": True,
                "extractor_type": "llm",
                "extractor_options": {"model_spec": "test/model", "concurrency_count": 1},
            }
        },
    )

    class ChunkRepo:
        async def count_graph_pending_by_kb_id(self, kb_id):
            return int(not chunk.graph_indexed)

        async def count_graph_indexed_by_kb_id(self, kb_id):
            return int(chunk.graph_indexed)

        async def count_graph_extraction_statuses_by_kb_id(self, kb_id):
            return {"pending": 0, "succeeded": 1, "failed": 0}

        async def list_graph_pending_by_kb_id(self, kb_id, limit, *, after_id=0):
            return [chunk] if chunk.id > after_id else []

    class GraphRepo:
        pending = True

        async def upsert_chunk_graph(self, **kwargs):
            raise AssertionError("reconcile must not rewrite graph structure")

        async def claim_vector_records(self, *, record_type, **kwargs):
            if record_type == "entity" and self.pending:
                self.pending = False
                return "token", [{"id": "entity_1", "content": "entity"}]
            return "token", []

        async def mark_vector_records_indexed(self, **kwargs):
            return None

        async def mark_vector_records_failed(self, **kwargs):
            raise AssertionError("vector indexing should succeed")

        async def count_vector_statuses_by_kb_id(self, kb_id):
            return {"pending": int(self.pending), "processing": 0, "indexed": 1, "failed": 0}

        async def finalize_graph_indexed_chunks(self, kb_id):
            chunk.graph_indexed = True
            return 1

    service = MilvusGraphService(
        kb_repo=SimpleNamespace(get_by_kb_id=AsyncMock(return_value=kb)),
        chunk_repo=ChunkRepo(),
        graph_repo=GraphRepo(),
        graph_vector_store=SimpleNamespace(upsert_graph_records=AsyncMock()),
    )
    monkeypatch.setattr(service, "write_chunk_graph", MagicMock(side_effect=AssertionError("must not write")))

    result = await service.build_pending_chunks("kb_test")

    assert result["success"] == 1
    service.write_chunk_graph.assert_not_called()


@pytest.mark.asyncio
async def test_milvus_graph_service_configure_rejects_spacy():
    kb = SimpleNamespace(kb_type="milvus", additional_params={})

    class Repo:
        async def get_by_kb_id(self, kb_id):
            return kb

        async def update(self, kb_id, data):
            raise AssertionError("unsupported extractor should not be persisted")

    service = MilvusGraphService(kb_repo=Repo())

    with pytest.raises(ValueError, match="不支持的图谱抽取器类型"):
        await service.configure(
            "kb_test",
            extractor_type="spacy",
            extractor_options={"model": "zh_core_web_sm"},
            created_by="user_1",
        )


@pytest.mark.asyncio
async def test_milvus_graph_service_configure_persists_updated_concurrency():
    kb = SimpleNamespace(
        kb_type="milvus",
        additional_params={
            "graph_build_config": {
                "locked": True,
                "extractor_type": "llm",
                "extractor_options": {"model_spec": "test/model", "concurrency_count": 5},
            }
        },
    )

    class Repo:
        async def get_by_kb_id(self, kb_id):
            return kb

        async def update(self, kb_id, data):
            kb.additional_params = data["additional_params"]
            return kb

    chunk_repo = SimpleNamespace(
        count_by_kb_id=AsyncMock(return_value=0),
        count_graph_pending_by_kb_id=AsyncMock(return_value=0),
        count_graph_indexed_by_kb_id=AsyncMock(return_value=0),
        count_graph_structure_indexed_by_kb_id=AsyncMock(return_value=0),
        count_graph_extraction_statuses_by_kb_id=AsyncMock(return_value={"pending": 0, "succeeded": 0, "failed": 0}),
    )
    graph_repo = SimpleNamespace(
        count_by_kb_id=AsyncMock(return_value=(3, 2)),
        count_vector_statuses_by_kb_id=AsyncMock(
            return_value={"pending": 0, "processing": 0, "indexed": 5, "failed": 0}
        ),
    )
    service = MilvusGraphService(kb_repo=Repo(), chunk_repo=chunk_repo, graph_repo=graph_repo)

    await service.configure(
        "kb_test",
        extractor_type="llm",
        extractor_options={"model_spec": "test/model", "concurrency_count": 9},
        created_by="user_1",
    )
    status = await service.get_status("kb_test")

    assert status["config"]["extractor_options"]["concurrency_count"] == 9
    assert status["entity_count"] == 3
    assert status["relationship_count"] == 2


@pytest.mark.asyncio
async def test_graph_status_reports_latest_successful_run_as_completed():
    kb = SimpleNamespace(
        kb_type="milvus",
        additional_params={
            "graph_build_config": {
                "locked": True,
                "extractor_type": "llm",
                "extractor_options": {"model_spec": "test/model", "concurrency_count": 5},
            }
        },
    )

    class Repo:
        async def get_by_kb_id(self, kb_id):
            return kb

    class Tasker:
        async def find_task_by_payload(self, *, task_type, payload_match, statuses):
            assert statuses is None
            return SimpleNamespace(status="success", progress=100)

    chunk_repo = SimpleNamespace(
        count_by_kb_id=AsyncMock(return_value=10),
        count_graph_pending_by_kb_id=AsyncMock(return_value=0),
        count_graph_indexed_by_kb_id=AsyncMock(return_value=10),
        count_graph_structure_indexed_by_kb_id=AsyncMock(return_value=10),
        count_graph_extraction_statuses_by_kb_id=AsyncMock(return_value={"pending": 0, "succeeded": 10, "failed": 0}),
    )
    graph_repo = SimpleNamespace(
        count_by_kb_id=AsyncMock(return_value=(3, 2)),
        count_vector_statuses_by_kb_id=AsyncMock(
            return_value={"pending": 0, "processing": 0, "indexed": 5, "failed": 0}
        ),
    )
    service = MilvusGraphService(kb_repo=Repo(), chunk_repo=chunk_repo, graph_repo=graph_repo)

    status = await service.get_status("kb_test", tasker=Tasker())

    assert status["build_task_status"] == "completed"
    assert status["build_task_progress"] == 100


def test_milvus_graph_service_writes_chunk_entity_and_relation():
    tx = MagicMock()
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute_write.side_effect = lambda func: func(tx)
    driver = MagicMock()
    driver.session.return_value = session
    connection = SimpleNamespace(driver=driver)
    service = MilvusGraphService(neo4j_connection=connection)
    chunk = SimpleNamespace(
        chunk_id="chunk_1",
        file_id="file_1",
        kb_id="kb_test",
        chunk_index=1,
        content="张三任职于公司",
        start_char_pos=0,
        end_char_pos=8,
    )

    entities, triples = service.write_chunk_graph(
        "kb_test",
        chunk,
        normalize_extraction_result(
            {
                "relations": [
                    {
                        "source": {
                            "text": "张三",
                            "label": "Person",
                            "attributes": [{"text": "工程师", "label": "Occupation"}],
                        },
                        "target": {"text": "公司", "label": "Organization"},
                        "text": "任职于",
                        "label": "WORKS_AT",
                    }
                ],
            },
            "llm",
        ),
    )

    assert [entity["name"] for entity in entities] == ["张三", "公司"]
    assert {entity["label"] for entity in entities} == {"Person", "Organization"}
    assert triples[0]["relation_type"] == "WORKS_AT"
    queries = [call.args[0] for call in tx.run.call_args_list]
    assert any("MERGE (c:Chunk:MilvusKB:`kb_test`" in query for query in queries)
    assert any("MERGE (e:Entity:MilvusKB:`kb_test`" in query for query in queries)
    assert any("MERGE (source)-[r:RELATION" in query for query in queries)
    entity_call = next(call for call in tx.run.call_args_list if "MERGE (e:Entity" in call.args[0])
    assert entity_call.kwargs["attributes"] == '[{"text": "工程师", "label": "Occupation"}]'
    assert entity_call.kwargs["attribute_names"] == ["Occupation"]
    assert entity_call.kwargs["attribute_values"] == ["工程师"]
    assert any("e.attribute_names = $attribute_names" in query for query in queries)
    assert any("e.attribute_values = $attribute_values" in query for query in queries)


def test_graph_vector_store_uses_idempotent_upsert():
    collection = MagicMock()
    store = MilvusGraphVectorStore.__new__(MilvusGraphVectorStore)

    store._upsert_entities(
        collection,
        [{"id": "entity_1", "content": "entity"}],
        [[0.1, 0.2]],
    )

    collection.upsert.assert_called_once_with([["entity_1"], ["entity"], [[0.1, 0.2]]])
    collection.insert.assert_not_called()


def test_milvus_graph_service_delete_file_graph_uses_scoped_streaming_queries():
    tx = MagicMock()
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute_write.side_effect = lambda func: func(tx)
    driver = MagicMock()
    driver.session.return_value = session
    service = MilvusGraphService(neo4j_connection=SimpleNamespace(driver=driver))

    service._delete_file_graph_from_neo4j("kb_test", "file_1")

    queries = [call.args[0] for call in tx.run.call_args_list]
    assert len(queries) == 3
    cleanup_query = queries[1]
    assert "file_id: $file_id" in cleanup_query
    assert "DELETE m" in cleanup_query
    assert "WITH DISTINCT e" in cleanup_query
    assert "collect(" not in cleanup_query
    assert "MATCH (e:Entity:MilvusKB:`kb_test` {kb_id: $kb_id})" not in cleanup_query
    assert "DETACH DELETE c" in queries[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "kwargs", "expected"),
    [
        ("get_labels", {}, []),
        ("get_stats", {}, {"total_nodes": 0, "total_edges": 0, "entity_types": []}),
    ],
)
async def test_milvus_graph_service_early_returns_empty_for_missing_kb_id(method, kwargs, expected):
    service = MilvusGraphService()
    assert await getattr(service, method)(kb_id=None, **kwargs) == expected


@pytest.mark.parametrize("operation", ["has_collection", "drop_collection"])
def test_drop_graph_collections_propagates_storage_failure(monkeypatch, operation):
    """图集合检查或删除失败必须向上传播。"""
    from yuxi.knowledge.graphs import milvus_graph_vector_store as module

    store = MilvusGraphVectorStore.__new__(MilvusGraphVectorStore)
    store.connection_alias = "test"
    error = RuntimeError("Milvus unavailable")
    monkeypatch.setattr(module.utility, "has_collection", MagicMock(return_value=True))
    monkeypatch.setattr(module.utility, operation, MagicMock(side_effect=error))

    with pytest.raises(RuntimeError) as caught:
        store.drop_graph_collections("kb_test")

    assert caught.value is error


def test_milvus_graph_service_entity_records_project_attributes_as_parallel_arrays():
    service = MilvusGraphService()

    records = service._build_entity_records(
        "kb_test",
        [
            {
                "id": "e1",
                "text": "上海项目",
                "label": "项目",
                "attributes": [
                    {"text": "上海", "label": "城市"},
                    {"text": "进行中", "label": "状态"},
                ],
            }
        ],
    )

    assert records[0]["attribute_names"] == ["城市", "状态"]
    assert records[0]["attribute_values"] == ["上海", "进行中"]


def test_milvus_graph_service_entity_records_project_empty_arrays_without_attributes():
    service = MilvusGraphService()

    records = service._build_entity_records("kb_test", [{"id": "e1", "text": "公司", "label": "组织"}])

    assert records[0]["attribute_names"] == []
    assert records[0]["attribute_values"] == []


def test_milvus_graph_service_entity_records_keep_attribute_values_with_separators():
    """属性值含分隔符时必须原样保留，平行数组不依赖任何拼接格式。"""
    service = MilvusGraphService()

    records = service._build_entity_records(
        "kb_test",
        [
            {
                "id": "e1",
                "text": "项目",
                "label": "项目",
                "attributes": [{"text": "代码=A1", "label": "编号"}],
            }
        ],
    )

    assert records[0]["attribute_names"] == ["编号"]
    assert records[0]["attribute_values"] == ["代码=A1"]


def test_build_subgraph_cypher_start_only_traverses_relations_from_anchor():
    # 起点/终点的豁免子句只在存在中间实体条件时才会生成，所以这里必须带一个实体条件。
    cypher = MilvusGraphService._build_subgraph_cypher(
        "kb_test",
        start_id="e1",
        end_id=None,
        max_depth=2,
        filters={"intermediate_entities": ["项目"]},
    )

    assert "MATCH (s:Entity:MilvusKB:`kb_test` {entity_id: $start_id})" in cypher
    assert "[:RELATION*1..2 {kb_id: $kb_id}]" in cypher
    assert "$end_id" not in cypher
    assert "all(n IN nodes(p) WHERE n = s OR n.label IN $entity_types)" in cypher


def test_build_subgraph_cypher_start_and_end_exempts_both_anchor_entities():
    cypher = MilvusGraphService._build_subgraph_cypher(
        "kb_test",
        start_id="e1",
        end_id="e2",
        max_depth=3,
        filters={"intermediate_entities": ["项目"]},
    )

    assert "MATCH (e:Entity:MilvusKB:`kb_test` {entity_id: $end_id})" in cypher
    assert "[:RELATION*1..3 {kb_id: $kb_id}]" in cypher
    assert "all(n IN nodes(p) WHERE n = s OR n = e OR n.label IN $entity_types)" in cypher


def test_build_subgraph_cypher_without_start_collects_seeds():
    cypher = MilvusGraphService._build_subgraph_cypher(
        "kb_test", start_id=None, end_id=None, max_depth=2, filters={}
    )

    assert "WITH seed LIMIT $max_nodes" in cypher
    assert "UNWIND seeds AS s" in cypher
    assert "+ [x IN seeds WHERE x IS NOT NULL] AS nodes" in cypher


def test_build_subgraph_cypher_maps_types_to_set_membership():
    cypher = MilvusGraphService._build_subgraph_cypher(
        "kb_test",
        start_id="e1",
        end_id=None,
        max_depth=2,
        filters={"intermediate_entities": ["项目", "组织"], "relation_types": ["属于"]},
    )

    assert "n.label IN $entity_types" in cypher
    assert "r.type IN $relation_types" in cypher


def test_build_subgraph_cypher_matches_entity_attributes_by_name_and_value():
    cypher = MilvusGraphService._build_subgraph_cypher(
        "kb_test",
        start_id="e1",
        end_id=None,
        max_depth=2,
        filters={"entity_attributes": [{"name": "城市", "value": "上海"}]},
    )

    assert "all(a IN $entity_attributes WHERE any(i IN range(0, size(n.attribute_names) - 1)" in cypher
    assert "n.attribute_names[i] = a.name AND n.attribute_values[i] = a.value" in cypher


def test_build_subgraph_cypher_splits_relation_attribute_matching_by_name():
    cypher = MilvusGraphService._build_subgraph_cypher(
        "kb_test",
        start_id="e1",
        end_id=None,
        max_depth=2,
        filters={
            "relation_attributes": [
                {"name": "text", "value": "投资"},
                {"name": "extractor_type", "value": "llm"},
            ]
        },
    )

    assert "toLower(coalesce(r.text, '')) CONTAINS toLower(v)" in cypher
    assert "r.extractor_type = v" in cypher
    assert "$relation_attr_text" in cypher
    assert "$relation_attr_extractor_type" in cypher


def test_build_subgraph_cypher_omits_absent_predicates():
    # 返回子句里的列表推导本身含 WHERE，所以这里断言的是「没有生成任何筛选子句」。
    cypher = MilvusGraphService._build_subgraph_cypher(
        "kb_test", start_id="e1", end_id=None, max_depth=2, filters={}
    )

    assert "all(r IN relationships(p)" not in cypher
    assert "all(n IN nodes(p)" not in cypher
    assert "$relation_types" not in cypher
    assert "$entity_attributes" not in cypher


def test_build_subgraph_cypher_rejects_unhandled_relation_attribute_name():
    """未处理的关系属性名必须显式失败，不能静默丢掉条件后返回未筛选的结果。"""
    with pytest.raises(ValueError, match="不支持的关系属性名"):
        MilvusGraphService._build_subgraph_cypher(
            "kb_test",
            start_id="e1",
            end_id=None,
            max_depth=2,
            filters={"relation_attributes": [{"name": "金额", "value": "100"}]},
        )


def test_build_subgraph_cypher_reports_truncation():
    cypher = MilvusGraphService._build_subgraph_cypher(
        "kb_test", start_id="e1", end_id=None, max_depth=2, filters={}
    )

    assert "size(graph_nodes) > $max_nodes" in cypher
    assert "size(paths) >= $path_limit" in cypher
    assert "size(edges) > $max_nodes * 2 AS truncated" in cypher
    assert "LIMIT $path_limit" in cypher


@pytest.mark.asyncio
async def test_query_subgraph_rejects_same_start_and_end():
    service = MilvusGraphService()

    with pytest.raises(ValueError, match="起点与终点不能是同一个实体"):
        await service.query_subgraph("kb_test", start_id="e1", end_id="e1")


@pytest.mark.asyncio
async def test_query_subgraph_rejects_end_without_start():
    service = MilvusGraphService()

    with pytest.raises(ValueError, match="只有填写起点后才能指定终点"):
        await service.query_subgraph("kb_test", start_id=None, end_id="e2")


@pytest.mark.asyncio
async def test_query_subgraph_rejects_entity_attributes_before_backfill(monkeypatch):
    service = MilvusGraphService()
    monkeypatch.setattr(service, "attributes_ready", AsyncMock(return_value=False))

    with pytest.raises(GraphAttributesNotReadyError, match="尚未回填筛选属性"):
        await service.query_subgraph(
            "kb_test",
            start_id="e1",
            filters={"entity_attributes": [{"name": "城市", "value": "上海"}]},
        )


@pytest.mark.asyncio
async def test_query_subgraph_propagates_neo4j_failure():
    session = MagicMock()
    session.__enter__.return_value = session
    session.run.side_effect = RuntimeError("Neo4j unavailable")
    driver = MagicMock()
    driver.session.return_value = session
    service = MilvusGraphService(neo4j_connection=SimpleNamespace(driver=driver))

    with pytest.raises(RuntimeError, match="Neo4j unavailable"):
        await service.query_subgraph("kb_test", start_id="e1", max_depth=1)


@pytest.mark.asyncio
async def test_query_subgraph_returns_nodes_edges_and_truncation():
    query_result = MagicMock()
    query_result.single.return_value = {
        "nodes": [_raw_graph_node("node-a"), _raw_graph_node("node-b")],
        "edges": [_raw_graph_edge("edge-a-b", "node-a", "node-b")],
        "truncated": True,
    }
    session = MagicMock()
    session.__enter__.return_value = session
    session.run.return_value = query_result
    driver = MagicMock()
    driver.session.return_value = session
    service = MilvusGraphService(neo4j_connection=SimpleNamespace(driver=driver))

    result = await service.query_subgraph("kb_test", start_id="node-a", end_id="node-b", max_depth=2)

    assert [node["id"] for node in result["nodes"]] == ["node-a", "node-b"]
    assert [edge["id"] for edge in result["edges"]] == ["edge-a-b"]
    assert result["truncated"] is True
    cypher, params = session.run.call_args
    assert "[:RELATION*1..2 {kb_id: $kb_id}]" in cypher[0]
    assert params["start_id"] == "node-a"
    assert params["end_id"] == "node-b"
    assert params["path_limit"] == 1000


@pytest.mark.asyncio
async def test_query_subgraph_clamps_depth_to_max_subgraph_depth():
    """service 层把超过上限的层数夹到 MAX_SUBGRAPH_DEPTH，而不是生成更大的跳数。"""
    query_result = MagicMock()
    query_result.single.return_value = {"nodes": [], "edges": [], "truncated": False}
    session = MagicMock()
    session.__enter__.return_value = session
    session.run.return_value = query_result
    driver = MagicMock()
    driver.session.return_value = session
    service = MilvusGraphService(neo4j_connection=SimpleNamespace(driver=driver))

    await service.query_subgraph("kb_test", start_id="e1", max_depth=9)

    cypher = session.run.call_args.args[0]
    assert "[:RELATION*1..3 {kb_id: $kb_id}]" in cypher


@pytest.mark.asyncio
async def test_attributes_ready_is_false_when_entities_carry_unprojected_attributes():
    query_result = MagicMock()
    query_result.single.return_value = {"unbackfilled": 3}
    session = MagicMock()
    session.__enter__.return_value = session
    session.run.return_value = query_result
    driver = MagicMock()
    driver.session.return_value = session
    service = MilvusGraphService(neo4j_connection=SimpleNamespace(driver=driver))

    assert await service.attributes_ready("kb_test") is False

    query_result.single.return_value = {"unbackfilled": 0}
    assert await service.attributes_ready("kb_test") is True


@pytest.mark.asyncio
async def test_get_filter_options_merges_attribute_values_by_name(monkeypatch):
    def fake_run(cypher: str, **_kwargs):
        if "DISTINCT e.label AS entity_type" in cypher:
            return [{"entity_type": "项目"}, {"entity_type": "组织"}]
        if "e.attribute_names[i] AS name" in cypher:
            return [
                {"name": "城市", "value": "上海"},
                {"name": "城市", "value": "北京"},
            ]
        if "DISTINCT r.type AS relation_type" in cypher:
            return [{"relation_type": "属于"}]
        if "DISTINCT r.text AS text" in cypher:
            return [{"text": "企业属于集团", "extractor_type": "llm"}]
        raise AssertionError(f"unexpected cypher: {cypher}")

    session = MagicMock()
    session.__enter__.return_value = session
    session.run.side_effect = fake_run
    driver = MagicMock()
    driver.session.return_value = session
    service = MilvusGraphService(neo4j_connection=SimpleNamespace(driver=driver))
    monkeypatch.setattr(service, "attributes_ready", AsyncMock(return_value=True))

    options = await service.get_filter_options("kb_test")

    assert options["entity_types"] == ["项目", "组织"]
    assert options["entity_attributes"] == [{"name": "城市", "values": ["上海", "北京"]}]
    assert options["relation_types"] == ["属于"]
    assert options["relation_attributes"] == [
        {"name": "text", "values": ["企业属于集团"]},
        {"name": "extractor_type", "values": ["llm"]},
    ]
    assert options["text_values_truncated"] is False
    assert options["attributes_ready"] is True


@pytest.mark.asyncio
async def test_search_entities_filters_by_name_substring():
    session = MagicMock()
    session.__enter__.return_value = session
    session.run.return_value = [{"entity_id": "e1", "name": "待接入企业", "label": "组织"}]
    driver = MagicMock()
    driver.session.return_value = session
    service = MilvusGraphService(neo4j_connection=SimpleNamespace(driver=driver))

    entities = await service.search_entities("kb_test", query="企业", limit=5)

    assert entities == [{"entity_id": "e1", "name": "待接入企业", "label": "组织"}]
    cypher = session.run.call_args.args[0]
    assert "toLower(coalesce(e.name, '')) CONTAINS toLower($q)" in cypher
    assert session.run.call_args.kwargs == {"q": "企业", "limit": 5}
