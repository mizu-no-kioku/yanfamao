from __future__ import annotations

import inspect
import os
from types import SimpleNamespace

import pytest

if os.name == "nt":
    pytest.skip("Yuxi Agent runtime uses Linux filesystem flags", allow_module_level=True)

from yuxi.agents.toolkits.business_database import tools

pytestmark = pytest.mark.unit


def _tool_callable(tool):
    callback = getattr(tool, "coroutine", None) or getattr(tool, "func", None)
    if callback is None:
        raise AssertionError(f"{tool.name} tool has no callable entry")
    return callback


async def _run_tool(tool, **kwargs):
    result = _tool_callable(tool)(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def test_database_tools_do_not_expose_identity_arguments() -> None:
    """模型只能提交表名或 SQL，不能伪造租户和员工身份。"""
    assert set(tools.list_business_database_tables.args) == {"dummy"}
    assert set(tools.describe_business_database_table.args) == {"table_name"}
    assert set(tools.query_business_database.args) == {"sql"}
    for database_tool in (
        tools.list_business_database_tables,
        tools.describe_business_database_table,
        tools.query_business_database,
    ):
        assert "tenant_id" not in database_tool.args
        assert "staff_id" not in database_tool.args
        assert "uid" not in database_tool.args


@pytest.mark.asyncio
async def test_query_tool_uses_runtime_uid(monkeypatch) -> None:
    """工具从 ToolRuntime 读取可信 uid 并传给服务。"""
    calls = []

    class FakeService:
        def execute_authorized_query(self, uid: str, sql: str):
            calls.append((uid, sql))
            return {"row_count": 0, "fields": [], "rows": []}

    monkeypatch.setattr(tools, "BusinessDatabaseService", FakeService)
    runtime = SimpleNamespace(context=SimpleNamespace(uid="trusted-uid"))
    result = await _run_tool(tools.query_business_database, sql="SELECT ID FROM t_project", runtime=runtime)

    assert calls == [("trusted-uid", "SELECT ID FROM t_project")]
    assert result["row_count"] == 0
