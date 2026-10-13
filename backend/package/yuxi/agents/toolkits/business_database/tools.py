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

# 返回给模型的拒绝文案：只给稳定错误码与通用说明。
# 详细的表名、列名、菜单 ID、配置项只写服务端日志——把内部结构回灌给模型，
# 既会泄漏授权边界，也会诱导模型针对内部标识反复试探。
_SAFE_DENIAL_TEXT: dict[str, str] = {
    # 解析器身份链与功能门
    "IDENTITY_USER_MISSING": "当前用户没有可用的业务数据库身份",
    "IDENTITY_USER_INACTIVE": "当前用户的业务数据库身份已失效",
    "IDENTITY_USER_NO_TENANT": "当前用户没有任何业务租户归属",
    "IDENTITY_TENANT_NOT_OWNED": "指定的租户与用户归属不符",
    "IDENTITY_STAFF_MISSING": "该租户下没有有效员工记录",
    "IDENTITY_STAFF_AMBIGUOUS": "该租户下的员工关联存在歧义",
    "TENANT_AMBIGUOUS": "用户归属多个租户，需要管理员明确指定租户",
    "ROLE_MISSING": "该租户下没有有效角色关系",
    "ROLE_INACTIVE": "当前角色已失效",
    "ROLE_CARDINALITY_INVALID": "当前角色关系异常，请联系管理员",
    "FUNCTION_GATE_DENIED": "当前角色没有 AI 问数功能权限，或没有可用的数据资源",
    "NO_PROJECT_ACCESS": "当前员工没有可访问的业务项目",
    # SQL 守卫
    "EMPTY_SQL": "未提供可执行的查询语句",
    "PARSE_ERROR": "查询语句无法解析",
    "MULTI_STATEMENT": "每次只允许一条查询语句",
    "READ_ONLY": "只允许只读查询",
    "FORBIDDEN_CLAUSE": "查询包含禁止的子句",
    "OUTER_JOIN_NOT_ALLOWED": "当前不支持外连接",
    "WILDCARD_COLUMN": "必须明确列名，不能使用 SELECT *",
    "FUNCTION_NOT_ALLOWED": "查询使用了不允许的函数",
    "QUALIFIED_TABLE": "不允许跨库查询",
    "DENIED_TABLE": "该表不允许查询",
    "TABLE_NOT_ALLOWED": "当前身份无权查询该表",
    "MISSING_SCHEMA": "该表缺少可信字段清单",
    "NO_TENANT_COLUMN": "该表无法实施租户隔离",
    "UNKNOWN_ALIAS": "查询引用了未知的表别名",
    "COLUMN_NOT_ALLOWED": "查询引用了不允许的字段",
    "DENIED_COLUMN": "该字段不允许查询",
    "SESSION_VARIABLE": "不允许读取或修改数据库会话变量",
    "RECURSIVE_CTE": "不允许递归 CTE",
    "NO_TABLE": "查询必须访问授权业务表",
    "OFFSET_NOT_ALLOWED": "不允许使用 OFFSET 分页",
    "TENANT_REWRITE_FAILED": "租户过滤条件注入失败",
    "STATUS_REWRITE_FAILED": "有效数据过滤条件注入失败",
    "STAFF_REWRITE_FAILED": "员工过滤条件注入失败",
    "PROJECT_REWRITE_FAILED": "项目过滤条件注入失败",
}

_GENERIC_DENIAL_TEXT = "业务数据库查询被拒绝：当前身份没有所需的数据访问权限"


def _safe_denial_message(exc: Exception) -> str:
    """把拒绝异常映射为可安全返回给模型的文案。

    `PolicyViolation.code` 与解析器拒绝消息前缀都是稳定错误码，取得到就带码 + 通用说明；
    取不到（例如服务层自带的「无权查看表 X」）一律退化为通用文案，绝不回显原文。
    """
    code = ""
    if isinstance(exc, PolicyViolation):
        code = exc.code
    elif isinstance(exc, BusinessDatabaseAuthorizationError):
        prefix = str(exc).split(":", 1)[0].strip()
        if prefix in _SAFE_DENIAL_TEXT:
            code = prefix
    if code:
        return f"{code}: {_SAFE_DENIAL_TEXT[code]}"
    return _GENERIC_DENIAL_TEXT


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
        # 详情只进服务端日志；返回给模型的只有稳定错误码与通用说明。
        logger.warning(
            "business database tool denied uid={} method={} type={} detail={}",
            uid,
            method_name,
            type(exc).__name__,
            exc,
        )
        raise ToolException(_safe_denial_message(exc)) from exc
    except Exception as exc:
        logger.exception("business database tool failed uid={} method={}", uid, method_name)
        raise ToolException("业务数据库查询失败，请联系管理员检查服务配置和数据库连接") from exc
