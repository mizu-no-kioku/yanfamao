from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from server.utils.auth_middleware import get_admin_user, get_required_user
from server.utils.knowledge_permissions import (
    ensure_knowledge_base_permission,
    require_knowledge_base_read,
)
from server.utils.knowledge_response import serialize_knowledge_base
from yuxi.knowledge.graphs.milvus_graph_service import (
    MAX_SUBGRAPH_DEPTH,
    RELATION_ATTRIBUTE_NAMES,
    GraphAttributesNotReadyError,
    MilvusGraphService,
)
from yuxi.permissions import ResourcePermission
from yuxi.knowledge.runtime import knowledge_base
from yuxi.storage.postgres.models_business import User
from yuxi.utils.logging_config import logger

graph = APIRouter(prefix="/graph", tags=["graph"])


async def _get_graph_service(kb_id: str) -> MilvusGraphService:
    db_info = await knowledge_base.get_database_info(kb_id)
    if not db_info:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    kb_type = db_info.kb_type.lower()
    if kb_type != "milvus":
        raise HTTPException(status_code=404, detail="Graph API only supports Milvus knowledge bases")

    return MilvusGraphService(kb_id=kb_id)


@graph.get("/list")
async def get_graphs(current_user: User = Depends(get_admin_user)):
    """获取支持图谱能力的 Milvus 知识库列表"""
    try:
        databases = await knowledge_base.get_databases_by_uid(current_user.uid)
        graphs = []
        for db in databases:
            if db.kb_type.lower() != "milvus":
                continue
            serialized = serialize_knowledge_base(db)
            graphs.append(
                {
                    "id": db.kb_id,
                    "name": db.name,
                    "type": "milvus",
                    "description": db.description,
                    "status": "已连接",
                    "created_at": serialized["created_at"],
                    "metadata": serialized,
                }
            )
        return {"success": True, "data": graphs}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to list graphs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list graphs: {str(e)}")


class GraphEntityRef(BaseModel):
    """起点或终点的实体引用。"""

    entity_id: str = Field(..., min_length=1, description="实体 entity_id")


class GraphFilterCondition(BaseModel):
    """属性类筛选条件：属性名与属性值必须同时命中。"""

    name: str = Field(..., min_length=1, description="属性名")
    value: str = Field(..., min_length=1, description="属性值")


class GraphSubgraphFilters(BaseModel):
    """四类路径筛选条件；同类多行 AND，类型集合内部 OR。"""

    intermediate_entities: list[str] = Field(default_factory=list, description="中间实体类型集合")
    entity_attributes: list[GraphFilterCondition] = Field(default_factory=list)
    relation_types: list[str] = Field(default_factory=list, description="实体间关系类型集合")
    relation_attributes: list[GraphFilterCondition] = Field(default_factory=list)


class GraphSubgraphQuery(BaseModel):
    """子图查询请求体。"""

    kb_id: str = Field(..., min_length=1)
    start: GraphEntityRef | None = None
    end: GraphEntityRef | None = None
    max_depth: int = Field(default=2, ge=1, le=MAX_SUBGRAPH_DEPTH, description="路径最大跳数")
    max_nodes: int = Field(default=100, ge=1, le=1000, description="最大节点数")
    filters: GraphSubgraphFilters = Field(default_factory=GraphSubgraphFilters)


@graph.post("/subgraph")
async def post_subgraph(
    query: GraphSubgraphQuery,
    current_user: User = Depends(get_required_user),
):
    """按起点（可选终点）与四类筛选查询 Milvus 知识库图谱子图"""
    # kb_id 只来自请求体，因此不能依赖 require_knowledge_base_read：它自身声明了 kb_id 参数，
    # 对请求体形态的路由会被 FastAPI 解析成必填 query 参数，导致只发请求体的调用方拿到 422，
    # 且授权校验读到的 kb_id 与实际执行的 kb_id 不是同一个。这里显式用请求体的 kb_id 校验。
    # 底座是任意登录用户：个人库创建者要能查自己库的图谱，权限仍由下面这一行按范围判定。
    await ensure_knowledge_base_permission(query.kb_id, current_user, ResourcePermission.READ)
    invalid_names = sorted(
        {
            condition.name
            for condition in query.filters.relation_attributes
            if condition.name not in RELATION_ATTRIBUTE_NAMES
        }
    )
    if invalid_names:
        raise HTTPException(
            status_code=422,
            detail=(
                f"关系属性名只支持 {'、'.join(RELATION_ATTRIBUTE_NAMES)}，"
                f"收到：{'、'.join(invalid_names)}"
            ),
        )
    try:
        logger.info(f"Querying subgraph - kb_id: {query.kb_id}, start: {query.start}, end: {query.end}")
        service = await _get_graph_service(query.kb_id)
        result_data = await service.query_subgraph(
            query.kb_id,
            start_id=query.start.entity_id if query.start else None,
            end_id=query.end.entity_id if query.end else None,
            max_depth=query.max_depth,
            max_nodes=query.max_nodes,
            filters=query.filters.model_dump(),
        )
        return {"success": True, "data": result_data}
    except HTTPException:
        raise
    except GraphAttributesNotReadyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Failed to query subgraph: {e}")
        raise HTTPException(status_code=503, detail=f"图谱存储不可用：{e}")


@graph.get("/filter-options")
async def get_filter_options(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(require_knowledge_base_read),
):
    """获取图谱筛选可用的类型与属性枚举"""
    try:
        service = await _get_graph_service(kb_id)
        return {"success": True, "data": await service.get_filter_options(kb_id)}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to get filter options: {e}")
        raise HTTPException(status_code=503, detail=f"图谱存储不可用：{e}")


@graph.get("/entities")
async def search_graph_entities(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    q: str = Query("", description="实体名称子串"),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(require_knowledge_base_read),
):
    """按名称搜索实体，供起点/终点选择器使用"""
    try:
        service = await _get_graph_service(kb_id)
        entities = await service.search_entities(kb_id, query=q, limit=limit)
        return {"success": True, "data": {"entities": entities}}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to search entities: {e}")
        raise HTTPException(status_code=503, detail=f"图谱存储不可用：{e}")


@graph.get("/labels")
async def get_graph_labels(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(require_knowledge_base_read),
):
    """获取 Milvus 知识库图谱的所有标签"""
    try:
        service = await _get_graph_service(kb_id)
        labels = await service.get_labels()
        return {"success": True, "data": {"labels": labels}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get labels: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get labels: {str(e)}")


@graph.get("/stats")
async def get_graph_stats(
    kb_id: str = Query(..., description="Milvus 知识库ID"),
    current_user: User = Depends(require_knowledge_base_read),
):
    """获取 Milvus 知识库图谱统计信息"""
    try:
        service = await _get_graph_service(kb_id)
        stats_data = await service.get_stats()
        return {"success": True, "data": stats_data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {str(e)}")
