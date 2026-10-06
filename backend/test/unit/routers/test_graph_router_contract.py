from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.main import app
from server.routers import graph_router


def test_subgraph_route_does_not_require_kb_id_as_query_parameter():
    """子图查询的 kb_id 只来自请求体。

    权限依赖 `require_knowledge_base_read` 自身声明了 `kb_id: str`；对 path/query 形态的旧路由它
    能解析到值，但对「kb_id 只在请求体里」的查询路由，FastAPI 会把它当成必填 query 参数，
    于是只发请求体的调用方（前端查询按钮）每次都拿到 422，而且授权校验读的是另一个 kb_id。
    """
    operation = app.openapi()["paths"]["/api/graph/subgraph"]["post"]
    required_query_parameters = {
        parameter["name"]
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "query" and parameter.get("required")
    }

    assert required_query_parameters == set()


@pytest.mark.asyncio
async def test_subgraph_route_rejects_unknown_relation_attribute_name(monkeypatch):
    async def allow_read(kb_id, current_user, required):
        return None

    # 授权在输入校验之前发生，这里只需要放行授权，让用例聚焦在属性名校验上。
    monkeypatch.setattr(graph_router, "ensure_knowledge_base_permission", allow_read)

    query = graph_router.GraphSubgraphQuery(
        kb_id="kb_1",
        filters=graph_router.GraphSubgraphFilters(
            relation_attributes=[graph_router.GraphFilterCondition(name="金额", value="100")]
        ),
    )

    with pytest.raises(HTTPException) as caught:
        await graph_router.post_subgraph(query, current_user=SimpleNamespace(uid="uid-user"))

    assert caught.value.status_code == 422
    assert "关系属性名只支持" in caught.value.detail
