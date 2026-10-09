"""供统一 Agent 使用的受控业务数据库工具。"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.tools import ToolException
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, Field

from yuxi.agents.toolkits.registry import tool
from yuxi.services.business_database_security import PolicyViolation
from yuxi.services.business_database_service import (
    BusinessDatabaseAuthorizationError,
    BusinessDatabaseConfigurationError,
    BusinessDatabaseService,
)
from yuxi.utils import logger


class ListBusinessDatabaseTablesInput(BaseModel):
    """列出授权业务表的工具输入。"""

    dummy: str = Field(default="", description="占位参数，请忽略")


class DescribeBusinessDatabaseTableInput(BaseModel):
    """读取授权业务表 Schema 的工具输入。"""

    table_name: str = Field(description="先由 list_business_database_tables 返回的完整表名")


class QueryBusinessDatabaseInput(BaseModel):
    """执行受控业务数据库查询的工具输入。"""

    sql: str = Field(
        description=(
            "一条 MySQL SELECT 查询；必须明确列名，不能使用 SELECT *。"
            "不要自行拼接租户、员工或项目身份，执行器会强制注入可信权限条件。"
        )
    )


@tool(
    category="buildin",
    tags=["数据库", "问数", "权限"],
    display_name="列出授权业务表",
    args_schema=ListBusinessDatabaseTablesInput,
)
async def list_business_database_tables(dummy: str, runtime: ToolRuntime) -> list[dict[str, str]]:
    """列出当前用户可查询的原业务系统表；生成 SQL 前先调用此工具。"""
    del dummy
    return await _call_service(runtime, "list_authorized_tables")


@tool(
    category="buildin",
    tags=["数据库", "问数", "Schema"],
    display_name="查看授权业务表结构",
    args_schema=DescribeBusinessDatabaseTableInput,
)
async def describe_business_database_table(table_name: str, runtime: ToolRuntime) -> dict[str, Any]:
    """读取一张已授权业务表的可见字段、类型和含义。"""
    return await _call_service(runtime, "describe_authorized_table", table_name)


@tool(
    category="buildin",
    tags=["数据库", "问数", "权限"],
    display_name="查询业务数据库",
    args_schema=QueryBusinessDatabaseInput,
)
async def query_business_database(sql: str, runtime: ToolRuntime) -> dict[str, Any]:
    """在租户、项目、字段和只读限制下执行一条业务数据库查询。"""
    return await _call_service(runtime, "execute_authorized_query", sql)


async def _call_service(runtime: ToolRuntime, method_name: str, *args: Any) -> Any:
    uid = str(getattr(runtime.context, "uid", "") or "").strip()
    if not uid:
        raise ToolException("无法获取当前 Yuxi 用户身份")
    service = BusinessDatabaseService()
    method = getattr(service, method_name)
    try:
        return await asyncio.to_thread(method, uid, *args)
    except (
        BusinessDatabaseAuthorizationError,
        BusinessDatabaseConfigurationError,
        PolicyViolation,
        ValueError,
    ) as exc:
        raise ToolException(str(exc)) from exc
    except Exception as exc:
        logger.exception("business database tool failed uid={} method={}", uid, method_name)
        raise ToolException("业务数据库查询失败，请联系管理员检查服务配置和数据库连接") from exc
