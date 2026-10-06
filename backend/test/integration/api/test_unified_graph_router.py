from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _create_dify_database(test_client, admin_headers) -> str:
    response = await test_client.post(
        "/api/knowledge/databases",
        json={
            "database_name": f"pytest_graph_dify_{uuid.uuid4().hex[:8]}",
            "description": "Graph router Dify negative test",
            "kb_type": "dify",
            "additional_params": {
                "dify_api_url": "https://api.dify.ai/v1",
                "dify_token": "test-token",
                "dify_dataset_id": f"dataset-{uuid.uuid4().hex[:8]}",
            },
        },
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["kb_id"]


async def _delete_database(test_client, admin_headers, kb_id: str) -> None:
    await test_client.delete(f"/api/knowledge/databases/{kb_id}", headers=admin_headers)


async def test_graph_routes_require_auth(test_client):
    response = await test_client.get("/api/graph/list")
    assert response.status_code == 401


async def test_standard_user_cannot_access_graph_endpoints(test_client, standard_user):
    response = await test_client.get("/api/graph/list", headers=standard_user["headers"])
    assert response.status_code == 403


async def test_get_graphs_list_only_returns_milvus(test_client, admin_headers, knowledge_database):
    response = await test_client.get("/api/graph/list", headers=admin_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert isinstance(payload["data"], list)
    assert payload["data"]
    assert all(graph["type"] == "milvus" for graph in payload["data"])
    assert any(graph["id"] == knowledge_database["kb_id"] for graph in payload["data"])


@pytest.mark.parametrize("path", ["/api/graph/stats", "/api/graph/labels"])
async def test_graph_endpoints_reject_non_milvus_types(test_client, admin_headers, path):
    kb_id = await _create_dify_database(test_client, admin_headers)
    try:
        response = await test_client.get(path, params={"kb_id": kb_id}, headers=admin_headers)
    finally:
        await _delete_database(test_client, admin_headers, kb_id)

    assert response.status_code == 404
    assert "only supports Milvus" in response.text


async def test_subgraph_endpoint_rejects_non_milvus_types(test_client, admin_headers):
    kb_id = await _create_dify_database(test_client, admin_headers)
    try:
        response = await test_client.post(
            "/api/graph/subgraph", json={"kb_id": kb_id}, headers=admin_headers
        )
    finally:
        await _delete_database(test_client, admin_headers, kb_id)

    assert response.status_code == 404
    assert "only supports Milvus" in response.text


@pytest.mark.parametrize(
    ("path", "params", "expected_keys"),
    [
        ("/api/graph/stats", {}, ("total_nodes", "total_edges", "entity_types")),
        ("/api/graph/labels", {}, ("labels",)),
    ],
)
async def test_milvus_graph_endpoints(test_client, admin_headers, knowledge_database, path, params, expected_keys):
    response = await test_client.get(
        path,
        params={"kb_id": knowledge_database["kb_id"], **params},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["success"] is True
    for key in expected_keys:
        assert key in payload["data"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/graph/neo4j/nodes",
        "/api/graph/neo4j/node",
        "/api/graph/neo4j/info",
        "/api/graph/neo4j/index-entities",
        "/api/graph/neo4j/add-entities",
    ],
)
async def test_neo4j_upload_routes_are_removed(test_client, admin_headers, path):
    method = test_client.post if path.endswith(("index-entities", "add-entities")) else test_client.get
    response = await method(path, headers=admin_headers)
    assert response.status_code == 404


async def test_subgraph_endpoint_returns_truncation_flag(test_client, admin_headers, knowledge_database):
    response = await test_client.post(
        "/api/graph/subgraph",
        json={"kb_id": knowledge_database["kb_id"], "max_nodes": 10},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == {"nodes", "edges", "truncated"}
    assert data["nodes"] == []
    assert data["truncated"] is False


async def test_subgraph_endpoint_rejects_max_depth_above_limit(test_client, admin_headers, knowledge_database):
    response = await test_client.post(
        "/api/graph/subgraph",
        json={"kb_id": knowledge_database["kb_id"], "max_depth": 5},
        headers=admin_headers,
    )

    assert response.status_code == 422


async def test_subgraph_endpoint_rejects_end_without_start(test_client, admin_headers, knowledge_database):
    response = await test_client.post(
        "/api/graph/subgraph",
        json={"kb_id": knowledge_database["kb_id"], "end": {"entity_id": "e1"}},
        headers=admin_headers,
    )

    assert response.status_code == 400
    assert "只有填写起点后才能指定终点" in response.text


async def test_subgraph_endpoint_rejects_same_start_and_end(test_client, admin_headers, knowledge_database):
    response = await test_client.post(
        "/api/graph/subgraph",
        json={
            "kb_id": knowledge_database["kb_id"],
            "start": {"entity_id": "e1"},
            "end": {"entity_id": "e1"},
        },
        headers=admin_headers,
    )

    assert response.status_code == 400
    assert "起点与终点不能是同一个实体" in response.text


async def test_subgraph_endpoint_rejects_unknown_relation_attribute_name(
    test_client, admin_headers, knowledge_database
):
    response = await test_client.post(
        "/api/graph/subgraph",
        json={
            "kb_id": knowledge_database["kb_id"],
            "filters": {"relation_attributes": [{"name": "金额", "value": "100"}]},
        },
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert "关系属性名只支持" in response.text


async def test_subgraph_get_is_gone(test_client, admin_headers):
    response = await test_client.get("/api/graph/subgraph", headers=admin_headers)

    assert response.status_code == 405


@pytest.mark.parametrize(
    ("path", "params", "expected_keys"),
    [
        (
            "/api/graph/filter-options",
            {},
            (
                "entity_types",
                "entity_attributes",
                "relation_types",
                "relation_attributes",
                "text_values_truncated",
                "attributes_ready",
            ),
        ),
        ("/api/graph/entities", {"q": "任意"}, ("entities",)),
    ],
)
async def test_graph_enumeration_endpoints_return_expected_shape(
    test_client, admin_headers, knowledge_database, path, params, expected_keys
):
    response = await test_client.get(
        path, params={"kb_id": knowledge_database["kb_id"], **params}, headers=admin_headers
    )

    assert response.status_code == 200, response.text
    for key in expected_keys:
        assert key in response.json()["data"]
