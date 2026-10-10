from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.routers import system_task_router

pytestmark = pytest.mark.asyncio


async def test_standard_user_task_list_is_scoped_to_operator(monkeypatch):
    captured = {}

    async def fake_list_tasks(*, status, limit, operator_id):
        captured.update(status=status, limit=limit, operator_id=operator_id)
        return {"tasks": [], "summary": {}}

    monkeypatch.setattr(system_task_router.tasker, "list_tasks", fake_list_tasks)

    await system_task_router.list_tasks(
        status="running",
        limit=20,
        current_user=SimpleNamespace(uid="user-1", role="user"),
    )

    assert captured == {"status": "running", "limit": 20, "operator_id": "user-1"}


async def test_standard_user_cannot_read_foreign_task(monkeypatch):
    async def fake_get_task(_task_id):
        return {"id": "task-1", "payload": {"operator_id": "user-2"}}

    monkeypatch.setattr(system_task_router.tasker, "get_task", fake_get_task)

    with pytest.raises(HTTPException) as exc_info:
        await system_task_router.get_task(
            "task-1",
            current_user=SimpleNamespace(uid="user-1", role="user"),
        )

    assert exc_info.value.status_code == 404


async def test_admin_task_list_remains_unscoped(monkeypatch):
    captured = {}

    async def fake_list_tasks(*, status, limit, operator_id):
        captured["operator_id"] = operator_id
        return {"tasks": [], "summary": {}}

    monkeypatch.setattr(system_task_router.tasker, "list_tasks", fake_list_tasks)

    await system_task_router.list_tasks(
        status=None,
        limit=100,
        current_user=SimpleNamespace(uid="admin-1", role="admin"),
    )

    assert captured["operator_id"] is None
