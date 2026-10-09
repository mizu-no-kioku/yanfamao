import pytest

from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.knowledge.implementations.milvus import MilvusKB, _retrieval_config_options


def test_milvus_retrieval_config_exposes_graph_and_dependencies():
    options = _retrieval_config_options()
    by_key = {option["key"]: option for option in options}

    assert by_key["use_graph_retrieval"]["default"] is False
    assert by_key["graph_max_nodes"]["default"] == 10000
    assert by_key["graph_max_nodes"]["depend_on"] == ("use_graph_retrieval", True)
    assert by_key["graph_top_k"]["depend_on"] == ("use_graph_retrieval", True)
    assert by_key["reranker_model"]["depend_on"] == ("use_reranker", True)


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"final_top_k": 101}, "不能大于"),
        ({"graph_max_nodes": 10**9}, "不能大于"),
        ({"ppr_damping": float("nan")}, "有限数值"),
        ({"unknown": 1}, "不支持的查询参数"),
    ],
)
def test_milvus_query_params_reject_unbounded_or_unknown_values(params, message):
    kb = object.__new__(MilvusKB)

    with pytest.raises(ValueError, match=message):
        kb.validate_query_params("db", params)


def test_milvus_query_params_allow_valid_file_filter_at_runtime():
    kb = object.__new__(MilvusKB)

    assert kb.validate_query_params(
        "db",
        {"final_top_k": 20, "file_name": "manual.pdf"},
        allowed_extra_keys={"file_name"},
    ) == {"final_top_k": 20, "file_name": "manual.pdf"}


def test_milvus_query_params_allow_declared_default_for_dynamic_select(monkeypatch):
    """动态 select（如可用重排序模型）的默认空值不能被判成非法取值。

    回归背景：`reranker_model` 的默认值是 ``""``，可选值来自当前部署的模型目录。
    若把默认空值也送进枚举校验，任何使用默认查询参数的 Milvus 知识库都会检索失败。
    """
    kb = object.__new__(MilvusKB)
    monkeypatch.setattr(
        kb,
        "get_query_params_config",
        lambda _kb_id, **_kwargs: {
            "type": "milvus",
            "options": [
                {
                    "key": "reranker_model",
                    "type": "select",
                    "default": "",
                    "options": [{"value": "provider:reranker"}],
                }
            ],
        },
    )

    assert kb.validate_query_params("db", {"reranker_model": ""}) == {"reranker_model": ""}


def test_milvus_query_params_reject_select_value_outside_choices(monkeypatch):
    """显式选择了目录外的具体值时仍然拒绝。"""
    kb = object.__new__(MilvusKB)
    monkeypatch.setattr(
        kb,
        "get_query_params_config",
        lambda _kb_id, **_kwargs: {
            "type": "milvus",
            "options": [
                {
                    "key": "search_mode",
                    "type": "select",
                    "default": "vector",
                    "options": [{"value": "vector"}, {"value": "hybrid"}],
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="取值无效"):
        kb.validate_query_params("db", {"search_mode": "unknown-mode"})


def test_graph_ppr_ranks_chunk_nodes_from_seed_entities():
    subgraph = {
        "nodes": [
            {"id": "e1", "type": "Entity", "properties": {"entity_id": "seed"}},
            {"id": "c1", "type": "Chunk", "properties": {"chunk_id": "chunk_a"}},
            {"id": "e2", "type": "Entity", "properties": {"entity_id": "other"}},
            {"id": "c2", "type": "Chunk", "properties": {"chunk_id": "chunk_b"}},
        ],
        "edges": [
            {"source_id": "e1", "target_id": "c1"},
            {"source_id": "e1", "target_id": "e2"},
            {"source_id": "e2", "target_id": "c2"},
        ],
    }

    ranked = MilvusGraphService.rank_chunks_by_ppr(subgraph, {"seed": 1.0}, top_k=2, damping=0.85)

    assert [chunk_id for chunk_id, _ in ranked] == ["chunk_a", "chunk_b"]


def test_rrf_fusion_merges_chunk_and_graph_rankings():
    kb = object.__new__(MilvusKB)
    base_chunks = [
        {"content": "base a", "metadata": {"chunk_id": "a"}, "score": 0.9},
        {"content": "base b", "metadata": {"chunk_id": "b"}, "score": 0.8},
    ]
    graph_chunks = [
        {"content": "graph b", "metadata": {"chunk_id": "b"}, "score": 0.7, "graph_score": 0.7},
        {"content": "graph c", "metadata": {"chunk_id": "c"}, "score": 0.6, "graph_score": 0.6},
    ]

    fused = kb._fuse_chunk_rankings(base_chunks, graph_chunks, graph_weight=1.0)

    assert [chunk["metadata"]["chunk_id"] for chunk in fused] == ["b", "a", "c"]
    assert fused[0]["graph_score"] == 0.7
    assert fused[0]["fusion_sources"] == ["chunk", "graph"]
