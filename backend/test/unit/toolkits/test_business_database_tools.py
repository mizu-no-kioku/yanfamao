from __future__ import annotations

import inspect
import os
from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

if os.name == "nt":
    pytest.skip("Yuxi Agent runtime uses Linux filesystem flags", allow_module_level=True)

from yuxi.agents.toolkits.business_database import tools
from yuxi.services.business_database_security import PolicyViolation
from yuxi.services.business_database_service import (
    BusinessDatabaseAuthorizationError,
    BusinessDatabaseConfigurationError,
)

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


class RecordingLogger:
    """记录服务端日志，用于断言「详情只进日志」。"""

    def __init__(self) -> None:
        self.entries: list[str] = []

    def _render(self, message, args) -> str:
        return message.format(*args) if args else message

    def warning(self, message, *args) -> None:
        self.entries.append(self._render(message, args))

    def error(self, message, *args) -> None:
        self.entries.append(self._render(message, args))

    def exception(self, message, *args) -> None:
        self.entries.append(self._render(message, args))

    def info(self, message, *args) -> None:  # pragma: no cover - 本测试不关心
        pass


def _failing_service(monkeypatch, exc: Exception, method: str = "execute_authorized_query") -> None:
    class FakeService:
        def __getattr__(self, name):
            def call(*_args, **_kwargs):
                raise exc

            return call

    monkeypatch.setattr(tools, "BusinessDatabaseService", FakeService)
    del method


async def _call_and_capture(monkeypatch, exc: Exception, *, sql: str = "SELECT ID FROM t_project"):
    logger = RecordingLogger()
    monkeypatch.setattr(tools, "logger", logger)
    _failing_service(monkeypatch, exc)
    runtime = SimpleNamespace(context=SimpleNamespace(uid="trusted-uid"))
    with pytest.raises(ToolException) as caught:
        await _run_tool(tools.query_business_database, sql=sql, runtime=runtime)
    return str(caught.value), logger.entries


@pytest.mark.asyncio
async def test_denial_to_model_carries_code_but_no_identifiers(monkeypatch) -> None:
    """拒绝文案只给稳定错误码与通用说明；表名/列名不得返回给模型。"""
    message, _logged = await _call_and_capture(
        monkeypatch,
        PolicyViolation("COLUMN_NOT_ALLOWED", "字段 t_project.OPERATER_STATUS 不在可信字段清单中"),
    )

    assert "COLUMN_NOT_ALLOWED" in message
    assert "OPERATER_STATUS" not in message
    assert "t_project" not in message


@pytest.mark.asyncio
async def test_denial_detail_is_logged_server_side(monkeypatch) -> None:
    """详细原因（含表名/列名）必须落到服务端日志，便于排障。"""
    _message, logged = await _call_and_capture(
        monkeypatch,
        PolicyViolation("COLUMN_NOT_ALLOWED", "字段 t_project.OPERATER_STATUS 不在可信字段清单中"),
    )

    assert any("t_project.OPERATER_STATUS" in entry for entry in logged), logged


@pytest.mark.asyncio
async def test_authorization_denial_exposes_code_but_not_menu_ids(monkeypatch) -> None:
    """功能门拒绝返回错误码，但不泄漏内部菜单 ID。"""
    message, logged = await _call_and_capture(
        monkeypatch,
        BusinessDatabaseAuthorizationError(
            "FUNCTION_GATE_DENIED: 未命中 AI 功能菜单 ID（[238, 253, 254]），当前可见菜单 ID [4, 7, 37]"
        ),
    )

    assert "FUNCTION_GATE_DENIED" in message
    assert "253" not in message
    assert "4, 7, 37" not in message
    assert any("253" in entry for entry in logged), logged


@pytest.mark.asyncio
async def test_configuration_error_message_is_generic(monkeypatch) -> None:
    """配置类错误不得把内部表名或配置项返回给模型。"""
    message, _logged = await _call_and_capture(
        monkeypatch,
        BusinessDatabaseConfigurationError("授权表不存在：t_project, t_project_user"),
    )

    assert "t_project" not in message


@pytest.mark.asyncio
async def test_unmapped_authorization_message_falls_back_to_generic(monkeypatch) -> None:
    """没有稳定错误码的授权消息（例如带表名的 describe 拒绝）只返回通用文案。"""
    message, logged = await _call_and_capture(
        monkeypatch,
        BusinessDatabaseAuthorizationError("当前身份无权查看表 t_project_user"),
    )

    assert "t_project_user" not in message
    assert any("t_project_user" in entry for entry in logged), logged
