from __future__ import annotations

from uuid import uuid4

import pytest

from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.storage.neo4j import get_shared_neo4j_connection, safe_neo4j_label

pytestmark = pytest.mark.integration


def _entity_ids(result: dict) -> set[str]:
    """取结果里的业务实体 id。节点 id 是 Neo4j 内部 element_id，不能用于业务断言。"""
    return {node["properties"].get("entity_id") for node in result["nodes"]}


async def _build_fixture_graph(kb_id: str) -> None:
    """建一张已知拓扑的图：企业 -属于-> 项目 -投资-> 人员，另有 企业 -属于-> 旧项目。"""
    label = safe_neo4j_label(kb_id)
    connection = get_shared_neo4j_connection()
    with connection.driver.session() as session:
        session.run(
            f"""
            CREATE (org:Entity:MilvusKB:`{label}` {{
                       kb_id: $kb_id, entity_id: 'e_org', name: '待接入企业', label: '组织',
                       attribute_names: ['城市'], attribute_values: ['上海']}}),
                   (proj:Entity:MilvusKB:`{label}` {{
                       kb_id: $kb_id, entity_id: 'e_proj', name: '研发项目', label: '项目',
                       attribute_names: ['状态'], attribute_values: ['进行中']}}),
                   (done:Entity:MilvusKB:`{label}` {{
                       kb_id: $kb_id, entity_id: 'e_done', name: '旧项目', label: '项目',
                       attribute_names: ['状态'], attribute_values: ['已完成']}}),
                   (person:Entity:MilvusKB:`{label}` {{
                       kb_id: $kb_id, entity_id: 'e_person', name: '张三', label: '人员',
                       attribute_names: [], attribute_values: []}}),
                   (org)-[:RELATION {{kb_id: $kb_id, type: '属于', text: '企业属于集团'}}]->(proj),
                   (proj)-[:RELATION {{kb_id: $kb_id, type: '投资', text: '研发投资'}}]->(person),
                   (org)-[:RELATION {{kb_id: $kb_id, type: '属于', text: '企业属于集团'}}]->(done)
            """,
            kb_id=kb_id,
        ).consume()


@pytest.fixture
def graph_kb_id():
    kb_id = f"pytest_filter_graph_{uuid4().hex}"
    yield kb_id
    label = safe_neo4j_label(kb_id)
    connection = get_shared_neo4j_connection()
    with connection.driver.session() as session:
        session.run(f"MATCH (n:MilvusKB:`{label}`) DETACH DELETE n").consume()


async def test_query_subgraph_returns_path_between_start_and_end(graph_kb_id):
    await _build_fixture_graph(graph_kb_id)
    service = MilvusGraphService()

    result = await service.query_subgraph(graph_kb_id, start_id="e_org", end_id="e_person", max_depth=2)

    assert _entity_ids(result) == {"e_org", "e_proj", "e_person"}


async def test_query_subgraph_start_only_expands_neighbourhood(graph_kb_id):
    """只给起点（界面上的主路径）时按层数向外扩展，起点本身不受实体类条件约束。"""
    await _build_fixture_graph(graph_kb_id)
    service = MilvusGraphService()

    depth_one = await service.query_subgraph(graph_kb_id, start_id="e_org", max_depth=1)
    assert _entity_ids(depth_one) == {"e_org", "e_proj", "e_done"}

    depth_two = await service.query_subgraph(graph_kb_id, start_id="e_org", max_depth=2)
    assert _entity_ids(depth_two) == {"e_org", "e_proj", "e_done", "e_person"}

    filtered = await service.query_subgraph(
        graph_kb_id,
        start_id="e_org",
        max_depth=2,
        filters={"intermediate_entities": ["项目"]},
    )
    assert _entity_ids(filtered) == {"e_org", "e_proj", "e_done"}


async def test_query_subgraph_reports_truncation_when_edges_hit_the_cap(graph_kb_id):
    """边被上限砍掉时必须报告 truncated，否则用户会以为看到的是全部关系。"""
    label = safe_neo4j_label(graph_kb_id)
    connection = get_shared_neo4j_connection()
    with connection.driver.session() as session:
        session.run(
            f"""
            CREATE (a:Entity:MilvusKB:`{label}` {{
                       kb_id: $kb_id, entity_id: 'e_a', name: 'A', label: '项目',
                       attribute_names: [], attribute_values: []}}),
                   (b:Entity:MilvusKB:`{label}` {{
                       kb_id: $kb_id, entity_id: 'e_b', name: 'B', label: '项目',
                       attribute_names: [], attribute_values: []}}),
                   (c:Entity:MilvusKB:`{label}` {{
                       kb_id: $kb_id, entity_id: 'e_c', name: 'C', label: '项目',
                       attribute_names: [], attribute_values: []}})
            """,
            kb_id=graph_kb_id,
        ).consume()
        for index in range(10):
            session.run(
                f"""
                MATCH (src:Entity:MilvusKB:`{label}` {{entity_id: 'e_a'}}),
                      (dst:Entity:MilvusKB:`{label}` {{entity_id: $target}})
                CREATE (src)-[:RELATION {{kb_id: $kb_id, type: $relation_type, text: '边'}}]->(dst)
                """,
                kb_id=graph_kb_id,
                target="e_b" if index < 5 else "e_c",
                relation_type=f"关系{index}",
            ).consume()

    service = MilvusGraphService()

    # 节点数 3 不超过 max_nodes=4，但边数 10 超过上限 4 * 2 = 8。
    result = await service.query_subgraph(graph_kb_id, start_id="e_a", max_depth=1, max_nodes=4)

    assert _entity_ids(result) == {"e_a", "e_b", "e_c"}
    assert result["truncated"] is True
    assert len(result["edges"]) == 8


async def test_query_subgraph_overview_without_start_returns_reachable_entities(graph_kb_id):
    """不填起点时以全部实体为种子，返回可达子图而不是空结果。"""
    await _build_fixture_graph(graph_kb_id)
    service = MilvusGraphService()

    result = await service.query_subgraph(graph_kb_id, start_id=None, end_id=None, max_depth=1)

    assert _entity_ids(result) == {"e_org", "e_proj", "e_done", "e_person"}
    assert result["truncated"] is False


async def test_query_subgraph_depth_limit_excludes_longer_paths(graph_kb_id):
    await _build_fixture_graph(graph_kb_id)
    service = MilvusGraphService()

    result = await service.query_subgraph(graph_kb_id, start_id="e_org", end_id="e_person", max_depth=1)

    assert result["nodes"] == []


async def test_query_subgraph_relation_type_condition_filters_whole_path(graph_kb_id):
    await _build_fixture_graph(graph_kb_id)
    service = MilvusGraphService()

    # 路径上有一条 属于 边不满足条件，整条路径必须不返回。
    applied = await service.query_subgraph(
        graph_kb_id,
        start_id="e_org",
        end_id="e_person",
        max_depth=2,
        filters={"relation_types": ["属于"]},
    )
    assert applied["nodes"] == []

    matched = await service.query_subgraph(
        graph_kb_id,
        start_id="e_org",
        end_id="e_person",
        max_depth=2,
        filters={"relation_types": ["属于", "投资"]},
    )
    assert _entity_ids(matched) == {"e_org", "e_proj", "e_person"}


async def test_query_subgraph_intermediate_entity_condition_exempts_start(graph_kb_id):
    await _build_fixture_graph(graph_kb_id)
    service = MilvusGraphService()

    result = await service.query_subgraph(
        graph_kb_id,
        start_id="e_org",
        end_id="e_person",
        max_depth=2,
        filters={"intermediate_entities": ["项目"]},
    )

    # 起点是「组织」而不是「项目」，豁免后仍然返回。
    assert _entity_ids(result) == {"e_org", "e_proj", "e_person"}


async def test_query_subgraph_entity_attribute_condition_matches_name_and_value(graph_kb_id):
    await _build_fixture_graph(graph_kb_id)
    service = MilvusGraphService()

    matched = await service.query_subgraph(
        graph_kb_id,
        start_id="e_org",
        end_id="e_person",
        max_depth=2,
        filters={"entity_attributes": [{"name": "状态", "value": "进行中"}]},
    )
    assert _entity_ids(matched) == {"e_org", "e_proj", "e_person"}

    # 负向：属性名相同但值不同，必须整条路径不返回。
    unmatched = await service.query_subgraph(
        graph_kb_id,
        start_id="e_org",
        end_id="e_person",
        max_depth=2,
        filters={"entity_attributes": [{"name": "状态", "value": "已完成"}]},
    )
    assert unmatched["nodes"] == []


async def test_query_subgraph_relation_attribute_text_uses_substring_match(graph_kb_id):
    await _build_fixture_graph(graph_kb_id)
    service = MilvusGraphService()

    # 企业→项目 那条边的 text 是「企业属于集团」，不含「投资」，整条路径被排除。
    excluded = await service.query_subgraph(
        graph_kb_id,
        start_id="e_org",
        end_id="e_person",
        max_depth=2,
        filters={"relation_attributes": [{"name": "text", "value": "投资"}]},
    )
    assert excluded["nodes"] == []

    # 只走 项目→人员 这一跳时，唯一的边含「投资」，路径成立。
    matched = await service.query_subgraph(
        graph_kb_id,
        start_id="e_proj",
        end_id="e_person",
        max_depth=1,
        filters={"relation_attributes": [{"name": "text", "value": "投资"}]},
    )
    assert _entity_ids(matched) == {"e_proj", "e_person"}


async def test_attributes_ready_detects_unprojected_attributes(graph_kb_id):
    await _build_fixture_graph(graph_kb_id)
    service = MilvusGraphService()

    assert await service.attributes_ready(graph_kb_id) is True

    connection = get_shared_neo4j_connection()
    label = safe_neo4j_label(graph_kb_id)
    with connection.driver.session() as session:
        session.run(
            f"MATCH (e:Entity:MilvusKB:`{label}` {{entity_id: 'e_person'}}) "
            "SET e.attributes = $attributes "
            "REMOVE e.attribute_names, e.attribute_values",
            attributes='[{"text": "工程师", "label": "职务"}]',
        ).consume()

    assert await service.attributes_ready(graph_kb_id) is False


async def test_get_filter_options_reports_graph_enums(graph_kb_id):
    await _build_fixture_graph(graph_kb_id)
    service = MilvusGraphService()

    options = await service.get_filter_options(graph_kb_id)

    assert set(options["entity_types"]) == {"人员", "组织", "项目"}
    assert {"name": "状态", "values": ["已完成", "进行中"]} in options["entity_attributes"]
    assert set(options["relation_types"]) == {"投资", "属于"}
    assert options["text_values_truncated"] is False
    assert options["attributes_ready"] is True


async def test_search_entities_matches_name_substring(graph_kb_id):
    await _build_fixture_graph(graph_kb_id)
    service = MilvusGraphService()

    matched = await service.search_entities(graph_kb_id, query="项目", limit=10)

    assert {entity["name"] for entity in matched} == {"研发项目", "旧项目"}
    assert await service.search_entities(graph_kb_id, query="不存在的名字", limit=10) == []
