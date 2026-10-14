"""退休业务数据库旧工具名，从智能体配置中移除。

旧实现把 `business_database_schema` / `business_database_query` 写进
`agents.config_json.context.tools`；这两个能力已由 `list_business_database_tables` /
`describe_business_database_table` / `query_business_database` 三工具取代。

在新部署里旧工具**根本没有注册**，因此无法通过应用接口删除：配置合并会
「保留写者无权访问的旧引用」（见 `merge_agent_config_json`），那是有意的安全行为，
本迁移不修改它，只在数据层做一次清理。

幂等：只摘掉这两个明确退休的名字，其余配置原样保留；没有可摘的一律不改动，
因此重复执行不产生副作用。
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import Agent
from yuxi.utils.logging_config import logger

# 仅这两个名字退休。不要在这里追加"顺便清理"的工具名——
# 每多一个名字，误删无关配置的风险就多一分。
RETIRED_TOOL_SLUGS: frozenset[str] = frozenset({"business_database_schema", "business_database_query"})


def strip_retired_tools(context: Any) -> tuple[Any, int]:
    """从一份 context 中摘掉退休工具名。

    返回（新的或原样的 context，摘掉的数量）。非字典、无 tools、或 tools 不是列表时
    一律原样返回，不做任何推断式修补。
    """
    if not isinstance(context, dict):
        return context, 0
    tools = context.get("tools")
    if not isinstance(tools, list):
        return context, 0

    kept = [tool for tool in tools if tool not in RETIRED_TOOL_SLUGS]
    removed = len(tools) - len(kept)
    if not removed:
        return context, 0

    updated = dict(context)
    updated["tools"] = kept
    return updated, removed


async def retire_business_database_tools(db: AsyncSession) -> int:
    """扫描全部智能体并移除退休工具名，返回摘掉的条目总数。

    重复执行：第二次扫描时这两个名字已不存在，摘掉数为 0，不写库。
    """
    result = await db.execute(select(Agent))
    removed_total = 0
    touched: list[str] = []
    for agent in result.scalars().all():
        config = agent.config_json
        if not isinstance(config, dict):
            continue
        context, removed = strip_retired_tools(config.get("context"))
        if not removed:
            continue
        updated_config = dict(config)
        updated_config["context"] = context
        agent.config_json = updated_config
        removed_total += removed
        touched.append(agent.slug)

    if removed_total:
        await db.flush()
        logger.info(f"已从 {len(touched)} 个智能体移除退休业务数据库工具名：{', '.join(touched)}")
    return removed_total
