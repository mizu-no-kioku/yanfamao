"""`GET /api/auth/users/access-options` 的可见性契约。

底座是 `get_required_user`（任意已登录且**已绑定部门**的用户）。真正挡住无部门调用者的闸门是
`get_required_user` 自身的 `if not user.department_id: raise 400`，不是处理函数内的守卫——
所以本测试覆盖 `get_current_user` 而不是 `get_required_user`，让真实的 400/401 分支被执行。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.auth_router import auth
from server.utils.auth_middleware import get_current_user, get_db
from yuxi.storage.postgres.models_business import Base, Department, User

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        dept_a = Department(name="部门A")
        dept_b = Department(name="部门B")
        db.add_all([dept_a, dept_b])
        await db.commit()
        await db.refresh(dept_a)
        await db.refresh(dept_b)

        def make(uid: str, role: str, department: Department | None) -> User:
            return User(
                username=uid,
                uid=uid,
                password_hash="$argon2id$placeholder",
                role=role,
                department=department,
            )

        superadmin = make("superadmin", "superadmin", dept_a)
        admin_a = make("admin_a", "admin", dept_a)
        user_a = make("user_a", "user", dept_a)
        user_b = make("user_b", "user", dept_b)
        no_dept = make("no_dept", "user", None)
        db.add_all([superadmin, admin_a, user_a, user_b, no_dept])
        await db.commit()
        for obj in (superadmin, admin_a, user_a, user_b, no_dept):
            await db.refresh(obj)

        caller = {"user": user_a}

        app = FastAPI()
        app.include_router(auth, prefix="/api")

        async def override_db():
            yield db

        async def override_current_user():
            return caller["user"]

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_current_user

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield {
                "client": client,
                "caller": caller,
                "users": {
                    "superadmin": superadmin,
                    "admin_a": admin_a,
                    "user_a": user_a,
                    "user_b": user_b,
                    "no_dept": no_dept,
                },
                "dept_a_id": dept_a.id,
            }
    await engine.dispose()


async def test_plain_user_sees_only_own_department(env):
    env["caller"]["user"] = env["users"]["user_a"]
    response = await env["client"].get("/api/auth/users/access-options")

    assert response.status_code == 200, response.text
    options = response.json()
    uids = {option["uid"] for option in options}
    assert uids == {"superadmin", "admin_a", "user_a"}
    assert "user_b" not in uids
    assert all(option["department_id"] == env["dept_a_id"] for option in options)


async def test_admin_sees_only_own_department(env):
    env["caller"]["user"] = env["users"]["admin_a"]
    response = await env["client"].get("/api/auth/users/access-options")

    assert response.status_code == 200, response.text
    options = response.json()
    uids = {option["uid"] for option in options}
    assert uids == {"superadmin", "admin_a", "user_a"}
    assert "user_b" not in uids
    assert all(option["department_id"] == env["dept_a_id"] for option in options)


async def test_superadmin_sees_all_users(env):
    env["caller"]["user"] = env["users"]["superadmin"]
    response = await env["client"].get("/api/auth/users/access-options")

    assert response.status_code == 200, response.text
    uids = {option["uid"] for option in response.json()}
    assert {"superadmin", "admin_a", "user_a", "user_b"} <= uids


async def test_access_options_do_not_expose_role(env):
    env["caller"]["user"] = env["users"]["user_a"]
    response = await env["client"].get("/api/auth/users/access-options")

    assert response.status_code == 200, response.text
    assert response.json()
    assert all("role" not in option for option in response.json())


async def test_caller_without_department_is_rejected(env):
    # 真正的闸门：get_required_user 对无部门调用者抛 400，处理函数根本不会被调用。
    env["caller"]["user"] = env["users"]["no_dept"]
    response = await env["client"].get("/api/auth/users/access-options")

    assert response.status_code == 400, response.text


async def test_unauthenticated_caller_is_rejected(env):
    env["caller"]["user"] = None
    response = await env["client"].get("/api/auth/users/access-options")

    assert response.status_code == 401, response.text
