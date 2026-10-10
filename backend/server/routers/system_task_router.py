from fastapi import APIRouter, Depends, HTTPException, Query

from yuxi.storage.postgres.models_business import User
from yuxi.services.task_service import tasker
from server.utils.auth_middleware import get_required_user

tasks = APIRouter(prefix="/tasks", tags=["tasks"])


def _can_access_task(task: dict, current_user: User) -> bool:
    """管理员可查看全部任务，普通用户只能操作自己发起的任务。"""

    if current_user.role in {"admin", "superadmin"}:
        return True
    payload = task.get("payload") or {}
    return str(payload.get("operator_id") or "") == str(current_user.uid)


async def _get_accessible_task(task_id: str, current_user: User) -> dict:
    """读取任务并以 404 隐藏不属于当前用户的资源。"""

    task = await tasker.get_task(task_id)
    if not task or not _can_access_task(task, current_user):
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@tasks.get("")
async def list_tasks(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
    current_user: User = Depends(get_required_user),
):
    """List tasks, optionally filtered by status."""
    operator_id = None if current_user.role in {"admin", "superadmin"} else str(current_user.uid)
    return await tasker.list_tasks(status=status, limit=limit, operator_id=operator_id)


@tasks.get("/{task_id}")
async def get_task(task_id: str, current_user: User = Depends(get_required_user)):
    """Retrieve a single task by id."""
    task = await _get_accessible_task(task_id, current_user)
    return {"task": task}


@tasks.post("/{task_id}/cancel")
async def cancel_task(task_id: str, current_user: User = Depends(get_required_user)):
    """Request cancellation of a task."""
    await _get_accessible_task(task_id, current_user)
    task = await tasker.cancel_task(task_id)
    if task is None:
        raise HTTPException(status_code=400, detail="Task cannot be cancelled")
    return {
        "task_id": task_id,
        "status": task.status,
        "cancel_requested": task.cancel_requested,
    }


@tasks.delete("/{task_id}")
async def delete_task(task_id: str, current_user: User = Depends(get_required_user)):
    """Delete a task by id."""
    await _get_accessible_task(task_id, current_user)
    success = await tasker.delete_task(task_id)
    if not success:
        raise HTTPException(status_code=409, detail="Task must exist and be terminal before deletion")
    return {"task_id": task_id, "status": "deleted"}
