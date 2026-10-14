"""RLS 兜底、跨租户写入拒绝与引用一致性的直接证据（真实 PostgreSQL）。

分工：**RLS 负责租户可见性，引用一致性触发器负责防止混合父引用**。同租户不同知识库的组合
RLS 抓不到（两边都可见），只有引用一致性约束能抓；跨租户组合两者都会拒绝。

两条纪律：

- 必须**以受限角色 `yuxi_app` 执行**才能证明隔离。连接串是 owner/superuser，它绕过 RLS，
  所以每个断言都发生在 `SET LOCAL ROLE` 之后。
- 预期被 SQL 拒绝的用例会让事务进入失败态，因此每个"应当被拒"的语句都开**自己的事务**，
  不会污染后续断言。

隔离方式照 `test_tenant_backfill.py` 的先例：随机 schema + `search_path`，三个 session 级 autouse
fixture 由本文件覆盖，不需要 TEST_USERNAME/TEST_PASSWORD。

**但连接来源是 `POSTGRES_TEST_URL`（独立实例），不是 `POSTGRES_URL`**：本文件的夹具会执行
实例级全局 DDL（`CREATE ROLE yuxi_app`、`ALTER ROLE`、`GRANT ... ON SCHEMA public`），
跑在共享开发实例上会改到共享角色与公共 schema，隔离 schema 挡不住。起测试实例：
`docker compose --profile test-db up -d postgres-test`。缺配置或目标核对失败**直接报错，不 skip**。
"""

from __future__ import annotations

import asyncio
import os
import uuid
import warnings
from contextlib import asynccontextmanager, suppress

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.evaluation_repository import EvaluationRepository
from yuxi.storage.postgres import manager as manager_module
from yuxi.storage.postgres.manager import (
    TENANT_DISCOVERY_ROLE,
    TENANT_PARENT_OWNERSHIP_RULES,
    TENANT_REFERENCE_GUARDED_TABLES,
    TENANT_REFERENCE_ROW_KB,
    TENANT_REFERENCE_RULES,
    TENANT_ROOT_TABLES,
    TENANT_RUNTIME_ROLE,
    PostgresManager,
)
from yuxi.storage.postgres.tenant_context import DEFAULT_TENANT_ID, clear_tenant_context, use_tenant

from _tenant_schema_test_db import create_test_admin_engine, tenant_schema_url

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

OTHER_TENANT_ID = 2


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件自行创建隔离 Schema，不依赖运行中的 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    yield


def _scoped_manager(engine) -> PostgresManager:
    """创建不触碰进程单例的隔离 manager。"""

    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager._initialized = True
    # 并发用例登记的挂起任务，以及本用例开过的全部后端 PID。夹具收尾要凭它们证明
    # "失败之后没有残留事务"——见 `_cancel_registered_tasks` 与夹具的收尾断言。
    manager._concurrency_tasks = []
    manager._transaction_pids = []
    return manager


# 本文件创建的隔离 schema 登记簿：**只登记，不删除**。会话收尾时统一打出清理语句交人工执行。
#
# 为什么不自动 `DROP SCHEMA`：`DROP ... CASCADE` 会在用例失败时把现场连同数据一起销毁，
# 而失败现场正是要留的东西。代价是测试实例会累积 schema——这是有意接受的取舍。
_CREATED_SCHEMAS: list[str] = []

# 清理清单的落盘位置。容器随时可能退出、stdout 也可能没人留，所以**创建时就追加落盘**。
# 要把清单带出容器，给 `docker compose run` 加一个挂载即可：
#   -v "$PWD/.artifacts:/artifacts:rw" -e YUXI_GUARDRAIL_SCHEMA_LOG=/artifacts/tenant_guardrail_schemas.sql
_SCHEMA_CLEANUP_LOG = os.environ.get(
    "YUXI_GUARDRAIL_SCHEMA_LOG", "/tmp/tenant_guardrail_schemas_to_clean.sql"
)


def _register_schema_for_manual_cleanup(schema: str) -> None:
    """**建 schema 之后立刻**登记：内存列表 + 追加落盘。

    必须在 `CREATE SCHEMA` 之后马上做，**不能**等夹具收尾——夹具的 `try/finally` 是从 `yield`
    才开始的，setup 阶段（建表、装策略、补授权）失败时收尾根本不会执行，那时 schema 已经建出来
    却没人记它，就变成了下一个"说不出从哪来的遗留 schema"。落盘则给"容器退出了、stdout 也没
    留下"这种情况兜底。
    """

    _CREATED_SCHEMAS.append(schema)
    try:
        with open(_SCHEMA_CLEANUP_LOG, "a", encoding="utf-8") as handle:
            handle.write(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE;\n')
    except OSError as exc:
        # 不静默吞掉：写不进去要说出来，否则"登记过"就是假的。
        warnings.warn(f"隔离 schema 清理清单写不进去（{exc}），该 schema 仅在内存登记：{schema}", stacklevel=2)


@pytest.fixture(scope="session", autouse=True)
def report_created_schemas_for_manual_cleanup():
    """会话结束时把所有**未自动删除**的隔离 schema 与清理语句一次性打出来。

    pytest 默认会捕获这里的 `print`，所以**同时**发一条 warning：不加 `-s` 也能从警告摘要里
    看到"有 N 个 schema 待人工清理"；加 `-s`（或任一条用例失败）能看到完整的清理语句。
    """

    yield
    if not _CREATED_SCHEMAS:
        return
    warnings.warn(
        f"本轮创建 {len(_CREATED_SCHEMAS)} 个隔离 schema，均**未**自动删除，需人工清理；"
        f"加 -s 可看到完整清理语句。",
        stacklevel=1,
    )
    print(f"\n[tenant-guardrails] 本轮创建 {len(_CREATED_SCHEMAS)} 个隔离 schema，**均未自动删除**，")
    print("[tenant-guardrails] 人工清理语句（确认无用后再执行）：")
    for _name in _CREATED_SCHEMAS:
        print(f'  DROP SCHEMA IF EXISTS "{_name}" CASCADE;')


@pytest_asyncio.fixture()
async def isolated_tenant_schema():
    """建隔离 schema，建表，跑租户 / RLS / 引用一致性三步，并把运行时角色授权补上。

    `ensure_tenant_schema()` 里的 `GRANT` 硬编码在 `SCHEMA public` 上，隔离 schema 拿不到权限。
    不补授的话，`SET LOCAL ROLE yuxi_app` 之后报的是 permission denied 而不是 RLS 拒绝——
    用例会以错误的原因变绿，那是假证据。
    """

    schema = f"tenant_guardrail_{uuid.uuid4().hex[:16]}"
    admin_engine = await create_test_admin_engine()
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    # **建出来就登记**：setup 后面任何一步失败都不会让它变成无主遗留 schema。
    _register_schema_for_manual_cleanup(schema)
    scoped_engine = create_async_engine(
        tenant_schema_url(),
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "search_path": schema,
                # **会话级**：这条连接上的每个事务都带着有限超时，不依赖"记得在每个事务里设"。
                "statement_timeout": _STATEMENT_TIMEOUT,
                "idle_in_transaction_session_timeout": _IDLE_TRANSACTION_TIMEOUT,
            }
        },
    )
    manager = _scoped_manager(scoped_engine)
    await manager.create_business_tables()
    await manager.create_knowledge_tables()
    await manager.ensure_tenant_schema()
    await manager.ensure_tenant_rls()
    await manager.ensure_tenant_reference_consistency()
    # 归属根护栏**不属于** `ensure_tenant_schema()`（那会改写既有版本语义），单独装。
    await manager.ensure_tenant_root_immutability()
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {TENANT_RUNTIME_ROLE}'))
        await connection.execute(
            text(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" '
                f"TO {TENANT_RUNTIME_ROLE}"
            )
        )
        await connection.execute(
            text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO {TENANT_RUNTIME_ROLE}')
        )
        # 发现角色同样需要隔离 schema 的 USAGE——`ensure_tenant_discovery_boundary()` 里的
        # `GRANT USAGE ON SCHEMA public` 硬编码在 `public` 上，隔离 schema 拿不到。不补授的话
        # 发现角色的失败原因是 permission denied 而不是边界，用例会以错误的理由变绿。
        await connection.execute(
            text(f'GRANT USAGE ON SCHEMA "{schema}" TO {TENANT_DISCOVERY_ROLE}')
        )
    try:
        yield manager
    finally:
        # 收尾顺序是有意的：先让挂起的并发任务消失（它们握着未提交的事务），再断言"没有事务
        # 还开着"。任何一环节漏掉，"断言失败"就会变成"测试挂死"。
        started = asyncio.get_running_loop().time()
        await _cancel_registered_tasks(manager)
        leftover_states = await _open_transaction_states(manager, manager._transaction_pids)
        teardown_seconds = asyncio.get_running_loop().time() - started
        await scoped_engine.dispose()
        await admin_engine.dispose()
        # **本夹具不自动 `DROP SCHEMA`**：破坏性存储操作交人工执行，`DROP ... CASCADE` 会在用例
        # 失败时把现场连同数据一起销毁。schema 原样保留——它**在创建时就已登记**（见夹具开头），
        # 这里不再重复登记。
        # 这两条断言让**本文件每一条用例**都成为"失败后无残留事务"的证据，而不只是并发那几条。
        assert teardown_seconds < 5.0, f"收尾没有很快完成（{teardown_seconds:.1f}s）——取消挂起任务没有生效"
        assert not any(state.startswith("idle in transaction") for state in leftover_states.values()), (
            f"夹具收尾时仍有事务开着：{leftover_states}"
        )


async def _seed(manager: PostgresManager, *statements: str) -> None:
    """以 owner 连接写入夹具数据（owner 绕过 RLS，因此不受策略影响）。"""

    async with manager.async_engine.begin() as connection:
        for statement in statements:
            await connection.execute(text(statement))


@asynccontextmanager
async def _as_tenant(manager: PostgresManager, tenant_id: int):
    """以受限角色、在指定租户上下文里开一个事务。

    `SET LOCAL ROLE` 与 `set_config(..., true)` 都是**事务局部**的。用会话局部会把角色和租户
    上下文泄漏到连接池里的下一条连接上，后续用例会以别人的身份跑。
    """

    async with manager.async_engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL ROLE {TENANT_RUNTIME_ROLE}"))
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield connection


async def _count_as_tenant(manager: PostgresManager, tenant_id: int, statement: str) -> int:
    async with _as_tenant(manager, tenant_id) as connection:
        return (await connection.execute(text(statement))).scalar()


async def _settle_as_tenant(manager: PostgresManager, tenant_id: int, statement: str) -> str:
    """以受限角色 + 指定租户上下文执行一条语句并提交，返回 `'committed'` 或错误描述。

    与 `_settle` 的分工：`_settle` 走 owner 连接（绕过 RLS），这条走 `SET LOCAL ROLE`，用来
    断言**受限角色在真实租户上下文里**到底能不能写成。被 SQL 拒绝时事务进入失败态，这里回滚
    后把原因作为字符串返回——断言看的是**结果**，不是异常类型。
    """

    try:
        async with _as_tenant(manager, tenant_id) as connection:
            await connection.execute(text(statement))
    except Exception as exc:  # noqa: BLE001 - 断言的是"是否被拒"，原因由消息体现
        return f"rejected: {exc}"
    return "committed"


@asynccontextmanager
async def _as_discovery_role(manager: PostgresManager):
    """以**发现角色** `yuxi_worker_discovery` 开一个事务。

    生产里 worker 用**独立连接串**登录这个角色（API 没有该凭据）；隔离用例用 `SET LOCAL ROLE`
    从管理连接降级到它——`SET LOCAL ROLE` 是事务局部的，不会泄漏到连接池的下一条连接。
    """

    async with manager.async_engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL ROLE {TENANT_DISCOVERY_ROLE}"))
        yield connection


async def _settle_as_discovery(manager: PostgresManager, statement: str) -> str:
    """以发现角色执行一条语句，返回 `'committed'` 或错误描述。语义同 `_settle_as_tenant`。"""

    try:
        async with _as_discovery_role(manager) as connection:
            await connection.execute(text(statement))
    except Exception as exc:  # noqa: BLE001 - 断言的是"是否被拒"
        return f"rejected: {exc}"
    return "committed"


async def _count_as_discovery(manager: PostgresManager, statement: str) -> int:
    async with _as_discovery_role(manager) as connection:
        return (await connection.execute(text(statement))).scalar()


async def _tenants_privileges(manager: PostgresManager, role: str) -> set[str]:
    """`tenants` 上某角色的**全部**授权（表级 + 列级），形如 `{'SELECT:id'}`。"""

    async with manager.async_engine.begin() as connection:
        table_level = {
            f"{row[0]}"
            for row in await connection.execute(
                text(
                    "SELECT privilege_type FROM information_schema.table_privileges "
                    "WHERE grantee = :role AND table_name = 'tenants' AND table_schema = current_schema()"
                ),
                {"role": role},
            )
        }
        column_level = {
            f"{row[0]}:{row[1]}"
            for row in await connection.execute(
                text(
                    "SELECT privilege_type, column_name FROM information_schema.column_privileges "
                    "WHERE grantee = :role AND table_name = 'tenants' AND table_schema = current_schema()"
                ),
                {"role": role},
            )
        }
    return table_level | column_level


# --------------------------------------------------------------------------------------
# A. RLS 兜底与角色属性
# --------------------------------------------------------------------------------------


async def test_guarded_tables_return_nothing_without_tenant_context(isolated_tenant_schema):
    """未设上下文必须返回 0 行——这是 RLS 兜底的唯一直接证据。"""

    manager = isolated_tenant_schema
    await _seed(manager, "INSERT INTO departments (id, name, tenant_id) VALUES (1, '默认部门', 1)")

    async with manager.async_engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL ROLE {TENANT_RUNTIME_ROLE}"))
        blind = (await connection.execute(text("SELECT count(*) FROM departments"))).scalar()

    assert blind == 0
    assert await _count_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT count(*) FROM departments") == 1


async def test_with_check_blocks_writing_into_another_tenant(isolated_tenant_schema):
    """写入侧也要拦住：只定义 USING 的话跨租户写入仍能成功。"""

    manager = isolated_tenant_schema
    with pytest.raises(Exception, match="row-level security"):
        async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
            await connection.execute(
                text("INSERT INTO departments (id, name, tenant_id) VALUES (2, '别家部门', 2)")
            )


async def test_existing_rows_cannot_be_moved_across_tenants(isolated_tenant_schema):
    """把已属于自己的行改成别人的租户：**被拒**。

    最先拦下它的是**归属根护栏**（`tenant ownership is immutable`，`23514`）——它比 RLS
    `WITH CHECK` 更早也更严（连 owner 路径都拦）。RLS `WITH CHECK` 对 **INSERT** 的拦截
    仍由 `test_with_check_blocks_writing_into_another_tenant` 证明；这里如实写明机制换了。
    """

    manager = isolated_tenant_schema
    await _seed(manager, "INSERT INTO departments (id, name, tenant_id) VALUES (3, '本家部门', 1)")

    with pytest.raises(Exception, match="tenant ownership is immutable"):
        async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
            await connection.execute(text("UPDATE departments SET tenant_id = 2 WHERE id = 3"))

    assert await _scalar(manager, "SELECT tenant_id FROM departments WHERE id = 3") == 1


async def test_a_reused_connection_without_context_reads_nothing_and_rejects_writes(
    isolated_tenant_schema,
):
    """连接池复用：上一个事务设过 `app.tenant_id`，本次**无上下文** → 读 0 行、写被拒。

    `set_config('app.tenant_id', ..., true)` 的 `true` 是**事务局部**——事务一结束就失效。
    这条直接取证，而不是"我相信它失效了"：在**同一条后端连接**（用 `pg_backend_pid()` 钉住）
    上先跑一个租户 1 的事务，再跑一个无上下文的事务——后者既读不到租户 1 的行，也写不进任何
    行。判据是**读到的结果**，不是"我记得设过"。

    这正是"连接池把上一个请求的连接还回来"时最容易出事的形态：角色与租户上下文都是会话/事务
    局部的，泄漏到下一条连接就会以别人的身份跑。
    """

    manager = isolated_tenant_schema
    await _seed(manager, "INSERT INTO departments (id, name, tenant_id) VALUES (1, '默认部门', 1)")

    async with manager.async_engine.connect() as connection:
        # 事务 1：租户 1 上下文。
        async with connection.begin():
            await connection.execute(text(f"SET LOCAL ROLE {TENANT_RUNTIME_ROLE}"))
            await connection.execute(text("SELECT set_config('app.tenant_id', '1', true)"))
            first_pid = (await connection.execute(text("SELECT pg_backend_pid()"))).scalar()
            visible = (await connection.execute(text("SELECT count(*) FROM departments"))).scalar()
        # 事务 2：**同一条连接**、无上下文（只重新声明角色，租户上下文不再设置）。
        async with connection.begin():
            await connection.execute(text(f"SET LOCAL ROLE {TENANT_RUNTIME_ROLE}"))
            second_pid = (await connection.execute(text("SELECT pg_backend_pid()"))).scalar()
            setting = (
                await connection.execute(text("SELECT current_setting('app.tenant_id', true)"))
            ).scalar()
            blind = (await connection.execute(text("SELECT count(*) FROM departments"))).scalar()
        # 事务 3：无上下文写入必须被 `WITH CHECK` 拒。
        rejected: Exception | None = None
        try:
            async with connection.begin():
                await connection.execute(text(f"SET LOCAL ROLE {TENANT_RUNTIME_ROLE}"))
                await connection.execute(
                    text("INSERT INTO departments (id, name, tenant_id) VALUES (9, '无上下文', 1)")
                )
        except Exception as exc:  # noqa: BLE001 - 断言的是"是否被拒"
            rejected = exc

    assert first_pid == second_pid, "两次事务不在同一条后端连接上，这条用例没有证明复用"
    assert visible == 1, "带上下文的那个事务本应看得见那一行"
    assert setting in (None, ""), f"事务局部的 `app.tenant_id` 泄漏到了下一个事务：{setting!r}"
    assert blind == 0, "无上下文的事务读到了租户 1 的行——RLS 兜底失效"
    assert rejected is not None, "无上下文写入没有被拒——`WITH CHECK` 没生效"
    assert "row-level security" in str(rejected), f"被拒原因不是 RLS：{rejected}"
    assert await _scalar(manager, "SELECT count(*) FROM departments WHERE id = 9") == 0


async def test_runtime_pool_checkout_clears_tenant_after_commit_and_rollback(isolated_tenant_schema):
    """登录受限角色，归还再取同一后端；生产 hook 在缺上下文时必须读零、拒写。"""

    manager = isolated_tenant_schema
    await _seed(manager, "INSERT INTO departments (id, name, tenant_id) VALUES (1, '池复用', 1)")
    async with manager.async_engine.connect() as connection:
        schema = await connection.scalar(text("SELECT current_schema()"))
    url = manager.async_engine.url.set(
        username=TENANT_RUNTIME_ROLE, password=os.environ["POSTGRES_APP_PASSWORD"]
    )
    engine = create_async_engine(
        url, pool_size=1, max_overflow=0,
        connect_args={"server_settings": {"search_path": schema}},
    )
    runtime = _scoped_manager(engine)
    runtime.runtime_engine = engine
    runtime._install_tenant_context_hook()
    try:
        await runtime.require_restricted_runtime_role()
        for rollback in (False, True):
            with use_tenant(1):
                async with runtime.RuntimeSession() as session:
                    first = await session.scalar(text("SELECT pg_backend_pid()"))
                    assert await session.scalar(text("SELECT count(*) FROM departments")) == 1
                    if rollback:
                        await session.rollback()
                    else:
                        await session.commit()
            with clear_tenant_context():
                async with runtime.RuntimeSession() as session:
                    second = await session.scalar(text("SELECT pg_backend_pid()"))
                    assert first == second, "池没有复用同一物理连接"
                    assert await session.scalar(text("SELECT current_user")) == TENANT_RUNTIME_ROLE
                    assert await session.scalar(text("SELECT count(*) FROM departments")) == 0
                    await session.rollback()
                with pytest.raises(Exception, match="row-level security"):
                    async with runtime.get_async_session_context() as session:
                        await session.execute(text("INSERT INTO departments (id, name, tenant_id) "
                                                   "VALUES (9, '池复用无上下文', 1)"))
        assert await _scalar(manager, "SELECT count(*) FROM departments WHERE id = 9") == 0
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------------------
# A2. 跨租户发现的**数据库权限边界**（选项 b：worker 专用发现角色）
#
# worker 必须知道"有哪些租户"才能逐租户进入上下文，而 `tenants` 没有 RLS。此前靠 blanket 的
# `GRANT ... ON ALL TABLES` 整表授权，等于"普通请求角色也能读整张租户表"。只收窄到列级也**不够**：
# API 与 worker 共用运行时角色，普通请求照样能枚举租户 id。所以边界落在**两个角色**上：
#   - 运行时角色 `yuxi_app` 对 `tenants` **完全无权限**（读与写都收回）；
#   - 专用发现角色 `yuxi_worker_discovery` 只有 `SELECT (id) ON tenants`，且不碰任何业务表。
# 生产里只有 worker 配置发现连接串，API 不持有——这才是"区分普通请求与跨租户 worker"。
# --------------------------------------------------------------------------------------


async def test_the_runtime_role_has_no_access_to_tenants_at_all(isolated_tenant_schema):
    """运行时角色对 `tenants` **完全**无权限：连 `id` 都读不到，更不能写。

    这是选项 b 的核心——发现能力被搬去专用角色后，普通请求路径不该再有任何租户表权限。
    """

    manager = isolated_tenant_schema
    await manager.ensure_tenant_discovery_boundary()

    assert (await _settle_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT id FROM tenants")).startswith("rejected")
    assert (await _settle_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT * FROM tenants")).startswith("rejected")
    assert (await _settle_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT count(*) FROM tenants")).startswith(
        "rejected"
    )


async def test_the_runtime_role_has_no_grant_rows_on_tenants(isolated_tenant_schema):
    """授权清单本身：`tenants` 上运行时角色**一条**授权都没有（表级 + 列级）。

    这条同时挡住"表级收回、列级遗留"这种半收窄——`REVOKE ALL PRIVILEGES` 必须把历史列级
    `SELECT (id)` 一并清掉。
    """

    manager = isolated_tenant_schema
    await manager.ensure_tenant_discovery_boundary()

    assert await _tenants_privileges(manager, TENANT_RUNTIME_ROLE) == set()


async def test_the_discovery_role_can_read_tenant_ids(isolated_tenant_schema):
    """正向：发现角色能读到租户 id（`known_tenant_ids()` 用的就是 `SELECT id FROM tenants`）。"""

    manager = isolated_tenant_schema
    await manager.ensure_tenant_discovery_boundary()

    async with _as_discovery_role(manager) as connection:
        ids = (await connection.execute(text("SELECT id FROM tenants ORDER BY id"))).scalars().all()

    assert DEFAULT_TENANT_ID in ids
    assert ids == sorted(ids)


async def test_the_discovery_role_grant_inventory_is_exactly_the_id_column(isolated_tenant_schema):
    """发现角色的授权清单：`tenants` 上**只有** `SELECT:id`，没有表级授权、没有其他列。"""

    manager = isolated_tenant_schema
    await manager.ensure_tenant_discovery_boundary()

    assert await _tenants_privileges(manager, TENANT_DISCOVERY_ROLE) == {"SELECT:id"}


async def test_the_discovery_role_cannot_read_other_columns_or_business_tables(isolated_tenant_schema):
    """越权（发现角色）：读其他列、读业务表、写 `tenants` 全部被拒。

    发现角色只该回答"有哪些租户 id"。给它任何业务表权限都会让它变成"一个可以跨租户读全库的账号"。
    """

    manager = isolated_tenant_schema
    await manager.ensure_tenant_discovery_boundary()
    await _seed(manager, "INSERT INTO departments (id, name, tenant_id) VALUES (7, '部门', 1)")

    assert (await _settle_as_discovery(manager, "SELECT name FROM tenants")).startswith("rejected")
    assert (await _settle_as_discovery(manager, "SELECT * FROM tenants")).startswith("rejected")
    # 业务表：发现角色完全没有授权（不是被 RLS 过滤，是 permission denied）。
    assert (await _settle_as_discovery(manager, "SELECT count(*) FROM departments")).startswith("rejected")
    assert (await _settle_as_discovery(manager, "SELECT count(*) FROM tasks")).startswith("rejected")
    # 写：同样被拒。
    assert (
        await _settle_as_discovery(
            manager,
            "INSERT INTO tenants (id, external_tenant_id, issuer, name, code) VALUES (97, 'x', 'y', 'z', 'x')",
        )
    ).startswith("rejected")
    assert (await _settle_as_discovery(manager, "UPDATE tenants SET name = 'hacked' WHERE id = 1")).startswith(
        "rejected"
    )
    assert (await _settle_as_discovery(manager, "DELETE FROM tenants WHERE id = 1")).startswith("rejected")


async def test_the_discovery_boundary_is_idempotent_and_survives_a_replay(isolated_tenant_schema):
    """重放安全：`REVOKE ALL` + 列级 `GRANT` 都是幂等的，重复执行结论不变。"""

    manager = isolated_tenant_schema

    await manager.ensure_tenant_discovery_boundary()
    await manager.ensure_tenant_discovery_boundary()

    assert (await _settle_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT id FROM tenants")).startswith("rejected")
    assert (await _count_as_discovery(manager, "SELECT count(id) FROM tenants")) >= 1


async def _replay_blanket_grant(manager: PostgresManager) -> None:
    """模拟重放 `GRANT ... ON ALL TABLES`：把 `tenants` 的表级权限重新授出去。"""

    async with manager.async_engine.begin() as connection:
        await connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO {TENANT_RUNTIME_ROLE}")
        )


async def test_the_runtime_role_cannot_create_update_or_delete_tenants(isolated_tenant_schema):
    """`tenants` 的**写**权限必须收回——租户的创建 / 修改 / 删除只应由受控的迁移入口完成。

    与上面"读也收回"的用例互补：这里钉的是写路径，并顺带证明**数据没被改动**。
    """

    manager = isolated_tenant_schema
    await manager.ensure_tenant_discovery_boundary()

    insert = await _settle_as_tenant(
        manager,
        DEFAULT_TENANT_ID,
        "INSERT INTO tenants (id, external_tenant_id, issuer, name, code) "
        "VALUES (99, 'x', 'y', 'z', 'x')",
    )
    update = await _settle_as_tenant(manager, DEFAULT_TENANT_ID, "UPDATE tenants SET name = 'hacked' WHERE id = 1")
    delete = await _settle_as_tenant(manager, DEFAULT_TENANT_ID, "DELETE FROM tenants WHERE id = 1")

    assert insert.startswith("rejected"), insert
    assert update.startswith("rejected"), update
    assert delete.startswith("rejected"), delete
    # 数据没有被改动，且默认租户行仍在。
    assert await _scalar(manager, "SELECT name FROM tenants WHERE id = 1") == "默认租户"
    assert await _scalar(manager, "SELECT count(*) FROM tenants") == 1


async def test_replaying_the_blanket_grant_does_not_leave_tenants_reachable(isolated_tenant_schema):
    """重放 blanket 授权后，**按版本 14 的顺序**再跑边界：运行时角色重新变为**完全不可达**，
    发现角色仍只读得到 id。

    这条证明"重放不会把权限永久放宽"——前提是边界在 blanket 授权**之后**执行。
    """

    manager = isolated_tenant_schema

    # 夹具已跑过 blanket 授权；先建立边界。
    await manager.ensure_tenant_discovery_boundary()
    # 重放 blanket 授权（模拟迁移语句列表再次执行）→ 运行时角色的权限被放回。
    await _replay_blanket_grant(manager)
    # 版本 14 装配里边界是**最后**一批，所以再跑一次。
    await manager.ensure_tenant_discovery_boundary()

    assert (await _settle_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT * FROM tenants")).startswith("rejected")
    assert (await _settle_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT id FROM tenants")).startswith("rejected")
    assert (
        await _settle_as_tenant(
            manager, DEFAULT_TENANT_ID, "INSERT INTO tenants (id, external_tenant_id, issuer, name, code) "
            "VALUES (98, 'x', 'y', 'z', 'x')"
        )
    ).startswith("rejected")
    # 发现能力不受重放影响，仍然只读得到 id。
    assert await _count_as_discovery(manager, "SELECT count(id) FROM tenants") >= 1
    assert (await _settle_as_discovery(manager, "SELECT * FROM tenants")).startswith("rejected")


async def test_the_boundary_must_run_after_the_blanket_grant(isolated_tenant_schema):
    """顺序依赖的**反向对照**：先跑边界、再重放 blanket 授权 → 权限被放宽。

    这条不是要修的行为，而是把"边界必须是最后一批"这条顺序要求钉成可复现的事实——否则
    "重放后仍然收窄"就成了一句无法验证的话。
    """

    manager = isolated_tenant_schema

    await manager.ensure_tenant_discovery_boundary()
    await _replay_blanket_grant(manager)

    # 边界被 blanket 授权覆盖：读整表与写入都重新变成可用。
    assert (await _settle_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT * FROM tenants")) == "committed"


# --------------------------------------------------------------------------------------
# A3. 装约束**之前**的存量预检
#
# 子侧触发器只约束**新写入**，管不到既有行。若库里已存在跨库子行，直接装约束会得到
# "约束装好了、脏数据还在"——边界看着关上了，其实一直开着。所以装约束前必须先把存量失配查出来。
# --------------------------------------------------------------------------------------


async def _seed_ignoring_triggers(manager: PostgresManager, *statements: str) -> None:
    """以 owner + `session_replication_role = replica` 写入——关掉触发器，模拟**历史脏数据**。

    触发器是后装的，管不到装之前就存在的行；要复现"既有失配"，只能绕过它。
    """

    async with manager.async_engine.begin() as connection:
        await connection.execute(text("SET LOCAL session_replication_role = replica"))
        for statement in statements:
            await connection.execute(text(statement))


async def test_the_precheck_passes_on_consistent_data(isolated_tenant_schema):
    """正向：数据自洽时预检**不**报错（否则它就成了"永远失败"的噪声）。"""

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES ('kb-a', 'A', 'milvus', 1)",
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-a', 'kb-a', 'a.txt')",
        "INSERT INTO knowledge_chunks (chunk_id, file_id, kb_id, chunk_index, content, "
        "graph_structure_indexed, graph_extraction_details) "
        "VALUES ('chunk-a', 'file-a', 'kb-a', 0, 'a', FALSE, jsonb_build_object('status', 'pending'))",
    )

    await manager.precheck_tenant_reference_consistency()  # 不抛错即为通过


async def test_the_precheck_flags_existing_cross_kb_children(isolated_tenant_schema):
    """存量失配必须被预检**查出来并显式失败**——这是"先预检、后装约束"能成立的前提。

    注意：这里的失配行是用 `session_replication_role = replica` 绕开触发器写进去的，因为
    触发器**只约束新写入**；"装约束前库里已有脏数据"正是预检要处理的场景。
    """

    manager = isolated_tenant_schema
    await _seed_ignoring_triggers(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        "('kb-a', 'A', 'milvus', 1), ('kb-b', 'B', 'milvus', 1)",
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-b', 'kb-b', 'b.txt')",
        # 子行挂在 kb-b 的父行上，自身却声明 kb-a → 跨库失配。
        "INSERT INTO knowledge_chunks (chunk_id, file_id, kb_id, chunk_index, content, "
        "graph_structure_indexed, graph_extraction_details) "
        "VALUES ('chunk-x', 'file-b', 'kb-a', 0, 'x', FALSE, jsonb_build_object('status', 'pending'))",
    )

    with pytest.raises(RuntimeError, match="既有跨库子行"):
        await manager.precheck_tenant_reference_consistency()


async def test_assertions_run_under_the_restricted_role_not_the_owner(isolated_tenant_schema):
    """角色属性退化会让 RLS 静默失效——不报错、不告警，只是边界没了。

    顺带证明上面的断言确实是**在受限账号下**跑的：owner/superuser 连接根本证明不了隔离。
    """

    manager = isolated_tenant_schema
    async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
        current = (await connection.execute(text("SELECT current_user"))).scalar()
        row = (
            await connection.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :role"),
                {"role": TENANT_RUNTIME_ROLE},
            )
        ).one()
        owned = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = current_schema() AND c.relkind = 'r' "
                    "AND pg_has_role(current_user, c.relowner, 'USAGE')"
                )
            )
        ).scalar()

    assert current == TENANT_RUNTIME_ROLE
    assert row.rolsuper is False
    assert row.rolbypassrls is False
    assert owned == 0, "yuxi_app 成了隔离 schema 里某张基表的 owner，RLS 语义会变"


# --------------------------------------------------------------------------------------
# A3b. 预检 → 装约束的**停写锁契约**（版本 14 完成门禁之一）
#
# 预检只证明"**提交那一刻**库里没有跨库子行"，子侧触发器只约束**新写入**。两步若分处两个
# 事务，中间那段"预检已过、触发器还没装上"的窗口就能被写入挤进一行跨库子行——预检看不到它、
# 触发器（尚未存在）也拦不住它，边界看着关上了、其实一直开着。
# `install_tenant_reference_consistency()` 把两步并进**同一事务**并全程持
# `ACCESS EXCLUSIVE` 停写锁。这一节直接取证：窗口内写入**卡在锁上**；放行后同一写入被刚
# 装上的触发器拒绝；最终库里没有失配行。
# --------------------------------------------------------------------------------------


async def _drop_reference_triggers(manager: PostgresManager) -> None:
    """卸掉引用一致性触发器，复现"装约束之前"的状态（子侧 + 父侧都卸）。"""

    async with manager.async_engine.begin() as connection:
        for table in TENANT_REFERENCE_GUARDED_TABLES:
            await connection.execute(
                text(f"DROP TRIGGER IF EXISTS enforce_{table}_tenant_reference_consistency ON {table}")
            )
            await connection.execute(
                text(f"DROP TRIGGER IF EXISTS enforce_{table}_parent_ownership_stability ON {table}")
            )


async def test_install_failure_rolls_back_trigger_ddl_and_releases_write_locks(
    isolated_tenant_schema, monkeypatch
):
    """真实DDL执行一半失败：整笔事务回滚，锁释放，随后重放成功。"""

    manager = isolated_tenant_schema
    await _drop_reference_triggers(manager)
    original = manager_module.TENANT_REFERENCE_CONSISTENCY_STATEMENTS
    with monkeypatch.context() as patch:
        patch.setattr(manager_module, "TENANT_REFERENCE_CONSISTENCY_STATEMENTS",
                      (*original, "SELECT 1 / 0"))
        with pytest.raises(Exception, match="division by zero"):
            await manager.install_tenant_reference_consistency()
    query = ("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
             "JOIN pg_namespace n ON n.oid=c.relnamespace "
             "WHERE n.nspname=current_schema() AND NOT t.tgisinternal "
             "AND t.tgname LIKE 'enforce_%' AND t.tgname NOT LIKE '%tenant_immutable'")
    assert await _scalar(manager, query) == 0, "失败后留下部分触发器"
    await _assert_lock_is_free(manager, "LOCK TABLE knowledge_chunks IN ROW EXCLUSIVE MODE NOWAIT")
    await manager.install_tenant_reference_consistency()
    assert await _scalar(manager, query) == len(TENANT_REFERENCE_RULES) + len(TENANT_PARENT_OWNERSHIP_RULES)


async def test_the_install_holds_a_stop_write_lock_across_precheck_and_install(
    isolated_tenant_schema, monkeypatch
):
    """预检→装约束窗口内**不可能**写入跨库子行：整个窗口持 `ACCESS EXCLUSIVE` 停写锁。

    做法：把 `_precheck_reference_consistency` 换成"先跑真预检、再记下安装后端 pid、再发信号、
    再等放行"的版本，于是 `install_tenant_reference_consistency()` 会在**取锁之后、装触发器之前**
    停住。此时从另一条连接尝试写入一行跨库子行——它必须**卡在对 `knowledge_chunks` 的
    `RowExclusiveLock` 上、且确实被安装后端挡住**（判据经表名/锁模式/阻塞者三重收窄，任何无关的
    锁等待都不会误判成绿）。放行后安装完成，同一写入被刚装上的触发器以引用一致性错误
    （`23514`）拒绝，最终库里没有失配行。

    反例（变异探针）就是"把 `LOCK TABLE` 去掉"：那时写入不再阻塞、直接提交，于是收窄后的阻塞
    判据与最终失配计数两条断言同时变红——这正是这条用例要钉住的窗口。
    """

    manager = isolated_tenant_schema
    # 复现"装约束之前"：卸触发器、种下两个知识库与 kb-b 的文件。
    await _drop_reference_triggers(manager)
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-b', 'kb-b', 'b.txt')",
    )
    assert await _mismatched_chunk_count(manager) == 0, "起点就不自洽，后面的断言会失去意义"

    reached = asyncio.Event()
    release = asyncio.Event()
    install_pids: list[int] = []
    original = PostgresManager._precheck_reference_consistency

    async def paused_precheck(self, connection):
        await original(self, connection)  # 真预检照跑（窗口内数据自洽 → 不抛）
        install_pids.append((await connection.execute(text("SELECT pg_backend_pid()"))).scalar())
        reached.set()  # 锁已取到、预检已过、触发器还没装
        await release.wait()

    monkeypatch.setattr(PostgresManager, "_precheck_reference_consistency", paused_precheck)

    install = _spawn(manager, manager.install_tenant_reference_consistency())
    blocked = False
    outcome = "<未执行>"
    try:
        await _barrier(reached, "安装停在中途（取锁之后、装触发器之前）")
        async with _transaction(manager) as writer:
            write = _spawn(manager, _settle(writer, _chunk_insert("chunk-x", "file-b", "kb-a")))
            # 三重收窄：等的是 knowledge_chunks 的 RowExclusiveLock，并且确实被安装后端挡住。
            blocked = await _wait_until_blocked(
                manager,
                writer.pid,
                relation="knowledge_chunks",
                mode="RowExclusiveLock",
                blocker_pid=install_pids[0],
            )
            release.set()
            await asyncio.wait_for(install, timeout=10)
            outcome = (await _collect(write, timeout=10))[0]
    finally:
        # 无论如何都放行：否则安装任务会一直挂在锁上，夹具收尾要 `DROP SCHEMA` 时被堵死。
        release.set()

    assert blocked, (
        "预检→装约束窗口内没有对 knowledge_chunks 停写：写入没有被安装后端挡住——"
        "它会在触发器装上之前提交一行失配数据"
    )
    assert "reference consistency violated" in outcome, f"装完触发器后同一写入不是被引用一致性拒绝：{outcome}"
    assert "23514" in outcome or "CheckViolationError" in outcome, f"拒绝的错误码不是 23514：{outcome}"
    assert await _mismatched_chunk_count(manager) == 0, "窗口内挤进了失配行，边界一直开着"


# --------------------------------------------------------------------------------------
# B. 多级归属链（递归 EXISTS 的正确性依赖整条父链都受有效策略保护）
# --------------------------------------------------------------------------------------


async def test_knowledge_chunk_chain_blocks_cross_tenant_reads(isolated_tenant_schema):
    """`knowledge_chunks` 没有 tenant_id，靠 file → kb 两级递归回归属根。"""

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        "('kb-a', 'A 的库', 'milvus', 1), ('kb-b', 'B 的库', 'milvus', 2)",
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES "
        "('file-a', 'kb-a', 'a.txt'), ('file-b', 'kb-b', 'b.txt')",
        "INSERT INTO knowledge_chunks "
        "(chunk_id, file_id, kb_id, chunk_index, content, graph_structure_indexed, graph_extraction_details) "
        "VALUES ('chunk-a', 'file-a', 'kb-a', 0, 'a', FALSE, jsonb_build_object('status', 'pending')), "
        "('chunk-b', 'file-b', 'kb-b', 0, 'b', FALSE, jsonb_build_object('status', 'pending'))",
    )

    assert await _count_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT count(*) FROM knowledge_chunks") == 1
    assert await _count_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT count(*) FROM knowledge_files") == 1

    async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
        visible = (await connection.execute(text("SELECT chunk_id FROM knowledge_chunks"))).scalars().all()
    assert visible == ["chunk-a"]


async def test_derived_table_policy_blocks_cross_tenant_reads(isolated_tenant_schema):
    """派生表策略回父表：租户 1 的上下文读不到租户 2 的子行。"""

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        "('kb-a', 'A 的库', 'milvus', 1), ('kb-b', 'B 的库', 'milvus', 2)",
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES "
        "('file-a', 'kb-a', 'a.txt'), ('file-b', 'kb-b', 'b.txt')",
    )

    async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
        result = await connection.execute(text("SELECT file_id FROM knowledge_files ORDER BY file_id"))
        visible = result.scalars().all()
    assert visible == ["file-a"]


async def test_business_chain_blocks_cross_tenant_reads_four_levels_deep(isolated_tenant_schema):
    """`tool_calls → messages → conversations → users`：四层递归，链上没有一张表有 tenant_id。

    正确性完全依赖**整条链**每一环都受有效策略保护——任何一环漏建，递归就会穿透。
    """

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO users (uid, username, password_hash, role, login_failed_count, is_deleted, tenant_id) VALUES "
        "('u-a', 'user-a', 'x', 'user', 0, 0, 1), ('u-b', 'user-b', 'x', 'user', 0, 0, 2)",
        "INSERT INTO projects (id, uid, selection_status, workdir_path, directory_mode) VALUES "
        "('proj-a', 'u-a', 'selectable', '/tmp/a', 'managed'), "
        "('proj-b', 'u-b', 'selectable', '/tmp/b', 'managed')",
        "INSERT INTO conversations (thread_id, uid, agent_id, project_id, is_pinned) VALUES "
        "('thread-a', 'u-a', 'default', 'proj-a', FALSE), "
        "('thread-b', 'u-b', 'default', 'proj-b', FALSE)",
        "INSERT INTO messages (conversation_id, role, content, delivery_status) "
        "SELECT c.id, 'user', 'hi', 'complete' FROM conversations c WHERE c.thread_id = 'thread-a'",
        "INSERT INTO messages (conversation_id, role, content, delivery_status) "
        "SELECT c.id, 'user', 'hi', 'complete' FROM conversations c WHERE c.thread_id = 'thread-b'",
        "INSERT INTO tool_calls (message_id, tool_name) "
        "SELECT m.id, 'search' FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.thread_id = 'thread-a'",
        "INSERT INTO tool_calls (message_id, tool_name) "
        "SELECT m.id, 'search' FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.thread_id = 'thread-b'",
    )

    assert await _count_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT count(*) FROM tool_calls") == 1
    assert await _count_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT count(*) FROM messages") == 1
    assert await _count_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT count(*) FROM conversations") == 1
    assert await _count_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT count(*) FROM users") == 1
    # 反向确认：另一个租户看得到它自己那条，说明不是"全都读不到"这种假绿。
    assert await _count_as_tenant(manager, OTHER_TENANT_ID, "SELECT count(*) FROM tool_calls") == 1


# --------------------------------------------------------------------------------------
# C. 混合父引用：同租户不同知识库（RLS 抓不到）+ 跨租户
# --------------------------------------------------------------------------------------


async def test_knowledge_chunk_rejects_a_file_from_another_knowledge_base(isolated_tenant_schema):
    """同一租户、两个不同知识库——RLS 两边都可见，只有引用一致性拦得住。"""

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        "('kb-a', 'A 的库', 'milvus', 1), ('kb-b', '丁的库', 'milvus', 1)",
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-b', 'kb-b', 'b.txt')",
    )

    with pytest.raises(Exception, match="reference consistency violated"):
        async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
            await connection.execute(
                text(
                    "INSERT INTO knowledge_chunks (chunk_id, file_id, kb_id, chunk_index, content) "
                    "VALUES ('chunk-x', 'file-b', 'kb-a', 0, 'x')"
                )
            )


async def test_knowledge_file_rejects_a_parent_file_from_another_knowledge_base(isolated_tenant_schema):
    """可空的 `parent_id` 也要管：不设防的话能挂到别的库的文件下面。"""

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        "('kb-a', 'A 的库', 'milvus', 1), ('kb-b', '丁的库', 'milvus', 1)",
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-b', 'kb-b', 'b.txt')",
    )

    with pytest.raises(Exception, match="reference consistency violated"):
        async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
            await connection.execute(
                text(
                    "INSERT INTO knowledge_files (file_id, kb_id, filename, parent_id) "
                    "VALUES ('file-x', 'kb-a', 'x.txt', 'file-b')"
                )
            )


async def test_graph_mention_rejects_a_chunk_from_another_knowledge_base(isolated_tenant_schema):
    """图谱提及表持**三个**独立引用（实体 / 文件 / 分块），每个都要与行自身 kb 一致。"""

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        "('kb-a', 'A 的库', 'milvus', 1), ('kb-b', '丁的库', 'milvus', 1)",
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES "
        "('file-a', 'kb-a', 'a.txt'), ('file-b', 'kb-b', 'b.txt')",
        "INSERT INTO knowledge_chunks "
        "(chunk_id, file_id, kb_id, chunk_index, content, graph_structure_indexed, graph_extraction_details) "
        "VALUES ('chunk-a', 'file-a', 'kb-a', 0, 'a', FALSE, jsonb_build_object('status', 'pending')), "
        "('chunk-b', 'file-b', 'kb-b', 0, 'b', FALSE, jsonb_build_object('status', 'pending'))",
        "INSERT INTO knowledge_graph_entities "
        "(entity_id, kb_id, normalized_name, label, name, vector_status, vector_attempt_count) "
        "VALUES ('ent-a', 'kb-a', 'e', 'ORG', 'E', 'pending', 0)",
    )

    # 实体与文件都是 kb-a，只有 chunk 来自 kb-b —— 三个引用里有一个不一致就必须拒。
    with pytest.raises(Exception, match="reference consistency violated"):
        async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
            await connection.execute(
                text(
                    "INSERT INTO knowledge_graph_entity_mentions "
                    "(entity_id, kb_id, file_id, chunk_id) "
                    "VALUES ('ent-a', 'kb-a', 'file-a', 'chunk-b')"
                )
            )


async def test_graph_triple_rejects_an_entity_from_another_knowledge_base(isolated_tenant_schema):
    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        "('kb-a', 'A 的库', 'milvus', 1), ('kb-b', '丁的库', 'milvus', 1)",
        "INSERT INTO knowledge_graph_entities "
        "(entity_id, kb_id, normalized_name, label, name, vector_status, vector_attempt_count) "
        "VALUES ('ent-a', 'kb-a', 'a', 'ORG', 'A', 'pending', 0), "
        "('ent-b', 'kb-b', 'b', 'ORG', 'B', 'pending', 0)",
    )

    with pytest.raises(Exception, match="reference consistency violated"):
        async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
            await connection.execute(
                text(
                    "INSERT INTO knowledge_graph_triples "
                    "(triple_id, kb_id, source_entity_id, target_entity_id, relation_type, content, "
                    "vector_status, vector_attempt_count) "
                    "VALUES ('tri-x', 'kb-a', 'ent-a', 'ent-b', 'rel', 'c', 'pending', 0)"
                )
            )


async def test_evaluation_run_item_rejects_an_item_from_another_knowledge_base(isolated_tenant_schema):
    """`evaluation_run_items` 没有 kb_id：行自身 kb 经不可空的 `run_id` 取。"""

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        "('kb-a', 'A 的库', 'milvus', 1), ('kb-b', '丁的库', 'milvus', 1)",
        "INSERT INTO evaluation_datasets (dataset_id, kb_id, name) VALUES ('ds-b', 'kb-b', 'B 集')",
        "INSERT INTO evaluation_dataset_items (item_id, dataset_id, kb_id, item_index, query_text) "
        "VALUES ('item-b', 'ds-b', 'kb-b', 0, 'q')",
        "INSERT INTO evaluation_runs (run_id, name, kb_id) VALUES ('run-a', 'A 跑', 'kb-a')",
    )

    with pytest.raises(Exception, match="reference consistency violated"):
        async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
            await connection.execute(
                text(
                    "INSERT INTO evaluation_run_items (run_id, dataset_item_id, item_index, query_text) "
                    "VALUES ('run-a', 'item-b', 0, 'q')"
                )
            )


async def test_evaluation_runs_reject_a_dataset_from_another_knowledge_base(isolated_tenant_schema):
    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        "('kb-a', 'A 的库', 'milvus', 1), ('kb-b', '丁的库', 'milvus', 1)",
        "INSERT INTO evaluation_datasets (dataset_id, kb_id, name) VALUES ('ds-b', 'kb-b', 'B 集')",
    )

    with pytest.raises(Exception, match="reference consistency violated"):
        async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
            await connection.execute(
                text(
                    "INSERT INTO evaluation_runs (run_id, name, kb_id, dataset_id) "
                    "VALUES ('run-x', 'X 跑', 'kb-a', 'ds-b')"
                )
            )


# --------------------------------------------------------------------------------------
# D. 空引用：允许，且删除题目触发 SET NULL 之后结果仍可读
# --------------------------------------------------------------------------------------


async def test_repository_reports_a_clear_error_for_a_cross_knowledge_base_dataset_item(isolated_tenant_schema):
    """数据库触发器是强制层；repository 这一层负责给出**看得懂**的错误。

    直接拿隔离 schema 的会话调 repository，不经过 `pg_manager` 单例。没有这一层的话，调用方
    拿到的是 `IntegrityError` 加一句通用消息，看不出是哪个题目不属于哪个知识库。
    """

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        "('kb-a', 'A 的库', 'milvus', 1), ('kb-b', '丁的库', 'milvus', 1)",
        "INSERT INTO evaluation_datasets (dataset_id, kb_id, name) VALUES ('ds-b', 'kb-b', 'B 集')",
        "INSERT INTO evaluation_dataset_items (item_id, dataset_id, kb_id, item_index, query_text) "
        "VALUES ('item-b', 'ds-b', 'kb-b', 0, 'q')",
        "INSERT INTO evaluation_runs (run_id, name, kb_id) VALUES ('run-a', 'A 跑', 'kb-a')",
    )

    factory = async_sessionmaker(manager.async_engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(ValueError, match="不一致"):
            await EvaluationRepository.upsert_run_item_in_session(
                session, "run-a", 0, {"dataset_item_id": "item-b", "query_text": "q"}
            )


async def test_evaluation_run_item_allows_a_null_dataset_item(isolated_tenant_schema):
    """空 `dataset_item_id` 必须放行——归属由 Run 决定，这正是它可空的原因。"""

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES ('kb-a', 'A 的库', 'milvus', 1)",
        "INSERT INTO evaluation_runs (run_id, name, kb_id) VALUES ('run-a', 'A 跑', 'kb-a')",
    )

    async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
        await connection.execute(
            text(
                "INSERT INTO evaluation_run_items (run_id, dataset_item_id, item_index, query_text) "
                "VALUES ('run-a', NULL, 0, 'q')"
            )
        )

    assert await _count_as_tenant(manager, DEFAULT_TENANT_ID, "SELECT count(*) FROM evaluation_run_items") == 1


async def test_evaluation_run_item_stays_readable_after_its_dataset_item_is_deleted(isolated_tenant_schema):
    """删题目触发 `ON DELETE SET NULL` 时，触发器会跟着被调用一次——它不能把这次更新拒掉。"""

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES ('kb-a', 'A 的库', 'milvus', 1)",
        "INSERT INTO evaluation_datasets (dataset_id, kb_id, name) VALUES ('ds-a', 'kb-a', 'A 集')",
        "INSERT INTO evaluation_dataset_items (item_id, dataset_id, kb_id, item_index, query_text) "
        "VALUES ('item-a', 'ds-a', 'kb-a', 0, 'q')",
        "INSERT INTO evaluation_runs (run_id, name, kb_id) VALUES ('run-a', 'A 跑', 'kb-a')",
        "INSERT INTO evaluation_run_items (run_id, dataset_item_id, item_index, query_text) "
        "VALUES ('run-a', 'item-a', 0, 'q')",
    )

    async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
        await connection.execute(text("DELETE FROM evaluation_dataset_items WHERE item_id = 'item-a'"))

    async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
        rows = (
            await connection.execute(text("SELECT run_id, dataset_item_id FROM evaluation_run_items"))
        ).all()

    assert len(rows) == 1, "删除题目之后评估结果必须仍然可读"
    assert rows[0].run_id == "run-a"
    assert rows[0].dataset_item_id is None


# --------------------------------------------------------------------------------------
# E. 父行侧防护与并发：改父行归属不得让既有子行失配
#
# 子侧触发器只在**子表**写入时触发，所以"改父行的 `kb_id`"这条路根本走不到它：`kb_id` 在
# `knowledge_file_repository._writable_fields` 里，`EvaluationRepository` 的通用 `setattr`
# 更是连白名单都没有。父行侧必须自己复核既有子行——这是本段存在的理由。
#
# 并发用例的三条纪律：
#
# - 用**屏障**（`asyncio.Event` + `pg_locks` 条件轮询）把两个事务固定到指定交错点，不用固定
#   `sleep`——`sleep` 撞不到就变成"看起来绿"；
# - 判定一律读**最终提交的数据**，"观察到阻塞 / 抛了异常"本身不算证据；
# - 隔离级别是服务端默认的 **READ COMMITTED**（代码里没有任何 `isolation_level` 设置），
#   结论不推广到其它级别。
#
# 这些用例走 owner 连接：行锁对 superuser 一样生效，而夹具写入的跨知识库行（同租户）需要
# 全部可见。租户可见性由 A/B/C 段负责，不在这里重复。
# --------------------------------------------------------------------------------------


# 并发用例里的每一处等待都必须**有界**：任何一处挂住都会连累夹具收尾（收尾要取消登记任务、
# 读事务状态、关连接，一条挂着的写入事务就能把它堵死），于是"断言失败"变成"测试挂死"。
# 合法阻塞窗口是**毫秒级**（对端一 set，这边立刻放行），下面这些值只是安全网，
# **不是**让用例变绿的手段——把等待放宽不会让任何一条断言从红变绿。
_BARRIER_SECONDS = 10.0
# 这两条设在**会话**上（见夹具的 `connect_args`），不是 `SET LOCAL`：`SET LOCAL` 只对当前事务
# 有效，用例中途 commit／rollback 之后的下一个事务不会继承——那种"以为设过了"的失效最难发现。
_STATEMENT_TIMEOUT = "15s"
_IDLE_TRANSACTION_TIMEOUT = "30s"
# 夹具收尾**不再**执行 `DROP SCHEMA`（见夹具说明），所以原先给那个 DDL 用的
# `_TEARDOWN_LOCK_TIMEOUT` 已无引用，随之移除。"有界等待"这条纪律本身不变（见下）。


async def _barrier(event: asyncio.Event, what: str) -> None:
    """有界地等一个交错点；超时即失败，而不是把整个测试挂住。"""

    try:
        await asyncio.wait_for(event.wait(), timeout=_BARRIER_SECONDS)
    except TimeoutError as exc:
        raise AssertionError(f"等待「{what}」超时（{_BARRIER_SECONDS}s）——对端没有走到这个交错点") from exc


class _Transaction:
    """owner 连接上的显式事务：并发用例要自己掌握提交点。"""

    def __init__(self, connection, transaction, pid: int) -> None:
        self._connection = connection
        self._transaction = transaction
        self.pid = pid

    async def execute(self, statement: str) -> None:
        await self._connection.execute(text(statement))

    async def scalar(self, statement: str):
        return (await self._connection.execute(text(statement))).scalar()

    async def commit(self) -> None:
        await self._transaction.commit()

    async def rollback(self) -> None:
        if self._transaction.is_active:
            await self._transaction.rollback()

    async def commit_outcome(self) -> str:
        """提交并返回 `'committed'` 或错误描述；被拒时回滚，不把事务留在失败态。"""

        try:
            await self._transaction.commit()
        except Exception as exc:  # noqa: BLE001 - 断言的是"是否被拒"，原因由消息体现
            await self.rollback()
            return f"rejected: {exc}"
        return "committed"

    async def close(self) -> None:
        # 回滚失败（连接正在被取消、或服务端已中止）时**作废**这条连接。把一条还开着事务的
        # 连接还给连接池，会让那个未结束的事务跟着池子漂到后面的用例里——"失败不留残留事务"
        # 就是靠这一步。捕获 `BaseException` 是有意的：`CancelledError` 也在内。
        try:
            await self.rollback()
        except BaseException:  # noqa: BLE001
            with suppress(BaseException):
                await self._connection.invalidate()
        await self._connection.close()


@asynccontextmanager
async def _transaction(manager: PostgresManager):
    connection = await manager.async_engine.connect()
    transaction = await connection.begin()
    # 超时不在这里设：夹具把 `statement_timeout` / `idle_in_transaction_session_timeout` 设在
    # **会话**上（`connect_args.server_settings`），所以这条连接上**每个**事务都带着它们，
    # 中途 commit／rollback 之后也不会失效。
    pid = (await connection.execute(text("SELECT pg_backend_pid()"))).scalar()
    manager._transaction_pids.append(pid)
    opened = _Transaction(connection, transaction, pid)
    try:
        yield opened
    finally:
        await opened.close()


async def _settle(transaction: _Transaction, statement: str) -> str:
    """执行一条语句并提交，返回 `'committed'` 或错误描述。

    被 SQL 拒绝的语句会让事务进入失败态——先回滚再返回，断言看的是**结果**而不是异常类型。
    """

    try:
        await transaction.execute(statement)
    except Exception as exc:  # noqa: BLE001 - 断言的是"是否被拒"，原因由消息体现
        await transaction.rollback()
        return f"rejected: {exc}"
    return await transaction.commit_outcome()


async def _stage(transaction: _Transaction, statement: str, *, done: asyncio.Event) -> str | None:
    """执行一条语句但**不提交**，然后 `done.set()`；语句被拒时返回错误描述。

    与 `_settle` 的分工：并发用例的交错点在**提交之前**，所以执行与提交必须分开控制。
    `done` 无论成败都要 set，否则对端会在等待里挂死。
    """

    try:
        await transaction.execute(statement)
    except Exception as exc:  # noqa: BLE001 - 同上
        await transaction.rollback()
        done.set()
        return f"rejected: {exc}"
    done.set()
    return None


async def _collect(*tasks: asyncio.Task, timeout: float = 30.0) -> list[str]:
    """等并发任务收场并返回各自结果，**任何情况下都不留挂起的任务**。

    挂起的任务握着未提交的事务，而夹具收尾要 `DROP SCHEMA`——一条挂着的 INSERT 足以把清理
    卡死，于是"断言失败"变成"测试挂住"。超时的任务在这里被取消并回收。
    """

    done, pending = await asyncio.wait(set(tasks), timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    outcomes: list[str] = []
    for task in tasks:
        if task in pending:
            outcomes.append("<未在超时内收场，已取消>")
        elif task.exception() is not None:
            outcomes.append(f"<异常: {task.exception()}>")
        else:
            outcomes.append(task.result())
    return outcomes


def _spawn(manager: PostgresManager, coroutine) -> asyncio.Task:
    """启动并发任务并**登记**它——夹具收尾会取消登记的每一个。

    这是"失败不留残留事务"的关键一步：断言在放行之前失败时没人会去 `_collect`，两条挂着的
    写入事务会把夹具的 `DROP SCHEMA` 一起堵死（最初那次 600 秒挂死就是这个形态）。
    登记之后，无论用例从哪一行失败，收尾都先取消它们。
    """

    task = asyncio.create_task(coroutine)
    manager._concurrency_tasks.append(task)
    return task


async def _cancel_registered_tasks(manager: PostgresManager) -> None:
    """取消所有登记的并发任务并等它们收场（夹具收尾调用的是同一个函数）。"""

    tasks = list(manager._concurrency_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    manager._concurrency_tasks.clear()


async def _assert_lock_is_free(manager: PostgresManager, statement: str) -> None:
    """拿得到冲突锁才算数：1 秒内拿不到就失败。

    这比"看起来没人在等锁"可靠——它直接证明那些事务真的结束了，因为事务没结束锁就不会放。
    """

    async with _transaction(manager) as probe:
        await probe.execute("SET LOCAL lock_timeout = '1s'")
        await probe.execute(statement)


async def _open_transaction_states(
    manager: PostgresManager, pids: list[int], timeout: float = 5.0
) -> dict[int, str]:
    """轮询到这些后端不再是"事务中"（或已消失），返回 pid -> 最终状态。

    判据是**不能**停在任何 `idle in transaction*` 上——那正是能堵死夹具收尾的形态；
    后端消失（进程没了）同样算收场。
    """

    if not pids:
        return {}
    listed = ", ".join(str(int(pid)) for pid in pids)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    states: dict[int, str] = {}
    while True:
        async with manager.async_engine.connect() as probe:
            rows = (
                await probe.execute(text(f"SELECT pid, state FROM pg_stat_activity WHERE pid IN ({listed})"))
            ).all()
        states = {row.pid: (row.state or "unknown") for row in rows}
        if not any(state.startswith("idle in transaction") for state in states.values()):
            return states
        if loop.time() >= deadline:
            return states
        await asyncio.sleep(0.05)


async def _scalar(manager: PostgresManager, statement: str):
    async with manager.async_engine.begin() as connection:
        return (await connection.execute(text(statement))).scalar()


async def _wait_until_blocked(
    manager: PostgresManager,
    pid: int,
    *,
    relation: str | None = None,
    mode: str | None = None,
    blocker_pid: int | None = None,
    timeout: float = 5.0,
) -> bool:
    """等到某个后端真的在等锁（`pg_locks` 里出现未授予的锁）。

    条件轮询而不是固定 `sleep`：固定 `sleep` 撞不到就变成"看起来绿"。

    可选收窄（`relation` / `mode` / `blocker_pid`）：只认**指定表、指定锁模式、且确实被指定后端
    挡住**的那把未授予锁。不收窄时的判据是"这个后端有任意一把未授予的锁"——它对"等的是不是我们
    要的那张表、挡它的是不是我们要的那个事务、冲突的又是不是我们预期的锁模式"都没有分辨力，
    任何一个无关的锁等待都能让断言变绿。需要钉住"停写窗口"这类语义时，三样都必须收窄。
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    params: dict[str, object] = {"pid": pid}
    if relation is None and mode is None and blocker_pid is None:
        query = "SELECT count(*) FROM pg_locks WHERE NOT granted AND pid = :pid"
    else:
        joins = "JOIN pg_class c ON c.oid = l.relation JOIN pg_namespace n ON n.oid = c.relnamespace"
        where = ["NOT l.granted", "l.pid = :pid"]
        if relation is not None:
            where.append("n.nspname = current_schema() AND c.relname = :relation")
            params["relation"] = relation
        if mode is not None:
            where.append("l.mode = :mode")
            params["mode"] = mode
        if blocker_pid is not None:
            where.append(":blocker = ANY(pg_blocking_pids(l.pid))")
            params["blocker"] = blocker_pid
        query = f"SELECT count(*) FROM pg_locks l {joins} WHERE " + " AND ".join(where)
    while loop.time() < deadline:
        async with manager.async_engine.connect() as probe:
            waiting = (await probe.execute(text(query), params)).scalar()
        if waiting:
            return True
        await asyncio.sleep(0.02)
    return False


def _chunk_insert(chunk_id: str, file_id: str, kb_id: str, index: int = 0) -> str:
    """`knowledge_chunks` 的裸插入。

    `graph_structure_indexed` 与 `graph_extraction_details` 在 ORM 里只有 **Python 侧
    default**、没有 `server_default`，所以裸 SQL 插入必须显式给值（账本 Task 8 记名的那条
    分叉）。不写的话报的是 `NotNullViolation`，会把"这条被拒是因为引用不一致"这个断言变成
    一个碰巧也红的假证据。
    """

    return (
        "INSERT INTO knowledge_chunks "
        "(chunk_id, file_id, kb_id, chunk_index, content, graph_structure_indexed, "
        "graph_extraction_details) "
        f"VALUES ('{chunk_id}', '{file_id}', '{kb_id}', {index}, 'c', FALSE, "
        "jsonb_build_object('status', 'pending'))"
    )


async def _mismatched_chunk_count(manager: PostgresManager) -> int:
    """最终提交的数据里，分块与它所属文件的知识库不一致的行数。"""

    return await _scalar(
        manager,
        "SELECT count(*) FROM knowledge_chunks AS c "
        "JOIN knowledge_files AS f ON f.file_id = c.file_id "
        "WHERE c.kb_id IS DISTINCT FROM f.kb_id",
    )


async def _two_knowledge_bases(manager: PostgresManager, second_kb: str = "kb-b") -> None:
    """同一租户下的两个知识库——两边 RLS 都可见，只有引用一致性抓得住。"""

    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        f"('kb-a', 'A 的库', 'milvus', 1), ('{second_kb}', '丁的库', 'milvus', 1)",
    )


async def test_moving_a_parent_file_with_existing_children_is_rejected(isolated_tenant_schema):
    """有既有子行时改父行归属：明确拒绝，且**不改写**子行（不做级联迁移归属）。"""

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-b', 'kb-b', 'b.txt')",
        _chunk_insert("chunk-b", "file-b", "kb-b"),
    )

    async with _transaction(manager) as transaction:
        outcome = await _settle(
            transaction, "UPDATE knowledge_files SET kb_id = 'kb-a' WHERE file_id = 'file-b'"
        )

    assert outcome.startswith("rejected")
    assert "parent ownership guard violated" in outcome
    assert await _scalar(manager, "SELECT kb_id FROM knowledge_files WHERE file_id = 'file-b'") == "kb-b"
    assert await _mismatched_chunk_count(manager) == 0


async def test_moving_a_parent_file_without_children_is_allowed(isolated_tenant_schema):
    """无子行的归属修改按契约**允许**：那是显式、可审计的单行变更，不产生不一致。"""

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-b', 'kb-b', 'b.txt')",
    )

    async with _transaction(manager) as transaction:
        outcome = await _settle(
            transaction, "UPDATE knowledge_files SET kb_id = 'kb-a' WHERE file_id = 'file-b'"
        )

    assert outcome == "committed"
    assert await _scalar(manager, "SELECT kb_id FROM knowledge_files WHERE file_id = 'file-b'") == "kb-a"


async def test_moving_a_run_that_has_a_result_row_without_a_dataset_item_is_rejected(isolated_tenant_schema):
    """判据是**存在结果行**，与 `dataset_item_id` 是否为空无关。

    只比非空题目会放过"空题目的结果行"，让它们随 Run 隐式迁移归属——那正是禁止的静默级联迁移。
    """

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO evaluation_runs (run_id, name, kb_id) VALUES ('run-a', 'A 跑', 'kb-a')",
        "INSERT INTO evaluation_run_items (run_id, dataset_item_id, item_index, query_text) "
        "VALUES ('run-a', NULL, 0, 'q')",
    )

    async with _transaction(manager) as transaction:
        outcome = await _settle(
            transaction, "UPDATE evaluation_runs SET kb_id = 'kb-b' WHERE run_id = 'run-a'"
        )

    assert outcome.startswith("rejected")
    assert "parent ownership guard violated" in outcome
    assert await _scalar(manager, "SELECT kb_id FROM evaluation_runs WHERE run_id = 'run-a'") == "kb-a"


async def test_moving_a_run_that_has_a_result_row_linked_to_a_dataset_item_is_rejected(isolated_tenant_schema):
    """题目引用非空的结果行同样必须拦住——这条与上一条互不替代。"""

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO evaluation_datasets (dataset_id, kb_id, name) VALUES ('ds-a', 'kb-a', 'A 集')",
        "INSERT INTO evaluation_dataset_items (item_id, dataset_id, kb_id, item_index, query_text) "
        "VALUES ('item-a', 'ds-a', 'kb-a', 0, 'q')",
        "INSERT INTO evaluation_runs (run_id, name, kb_id) VALUES ('run-a', 'A 跑', 'kb-a')",
        "INSERT INTO evaluation_run_items (run_id, dataset_item_id, item_index, query_text) "
        "VALUES ('run-a', 'item-a', 0, 'q')",
    )

    async with _transaction(manager) as transaction:
        outcome = await _settle(
            transaction, "UPDATE evaluation_runs SET kb_id = 'kb-b' WHERE run_id = 'run-a'"
        )

    assert outcome.startswith("rejected")
    assert "parent ownership guard violated" in outcome


async def test_clearing_a_result_rows_dataset_item_stays_legal(isolated_tenant_schema):
    """显式把 `dataset_item_id` 置空必须仍然合法——归属由 Run 决定，这正是它可空的原因。

    父行侧的保护不能顺手把"子行改引用"也一起挡掉。
    """

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO evaluation_datasets (dataset_id, kb_id, name) VALUES ('ds-a', 'kb-a', 'A 集')",
        "INSERT INTO evaluation_dataset_items (item_id, dataset_id, kb_id, item_index, query_text) "
        "VALUES ('item-a', 'ds-a', 'kb-a', 0, 'q')",
        "INSERT INTO evaluation_runs (run_id, name, kb_id) VALUES ('run-a', 'A 跑', 'kb-a')",
        "INSERT INTO evaluation_run_items (run_id, dataset_item_id, item_index, query_text) "
        "VALUES ('run-a', 'item-a', 0, 'q')",
    )

    async with _transaction(manager) as transaction:
        outcome = await _settle(
            transaction,
            "UPDATE evaluation_run_items SET dataset_item_id = NULL WHERE run_id = 'run-a'",
        )

    assert outcome == "committed"
    assert await _scalar(manager, "SELECT dataset_item_id FROM evaluation_run_items WHERE run_id = 'run-a'") is None


async def test_concurrent_child_insert_then_parent_move_commits_no_mismatch(isolated_tenant_schema):
    """子行先插入（未提交）× 父行改归属：锁必须让两者串行，最终数据不得失配。

    没有 `FOR SHARE` 时，父行的 `UPDATE` 取 `FOR NO KEY UPDATE`，而子行插入的外键锁是
    `FOR KEY SHARE`——两者**不冲突**，于是父行看不到未提交的子行、照常提交，子行随后提交，
    落下一个失配。这条用例就是那个缺口的直接证据。
    """

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-f', 'kb-a', 'f.txt')",
    )

    child_ready = asyncio.Event()
    release = asyncio.Event()
    parent_pid: list[int] = []
    parent_started = asyncio.Event()

    async def child_side() -> str:
        async with _transaction(manager) as transaction:
            rejected = await _stage(
                transaction, _chunk_insert("chunk-c", "file-f", "kb-a"), done=child_ready
            )
            if rejected is not None:
                return rejected
            await _barrier(release, "主协程放行")
            return await transaction.commit_outcome()

    async def parent_side() -> str:
        await _barrier(child_ready, "子行插入就绪")
        async with _transaction(manager) as transaction:
            parent_pid.append(transaction.pid)
            parent_started.set()
            return await _settle(
                transaction, "UPDATE knowledge_files SET kb_id = 'kb-b' WHERE file_id = 'file-f'"
            )

    child = _spawn(manager, child_side())
    await _barrier(child_ready, "子行插入就绪")
    parent = _spawn(manager, parent_side())
    await _barrier(parent_started, "父行改归属开始")
    blocked = await _wait_until_blocked(manager, parent_pid[0])
    release.set()
    child_outcome, parent_outcome = await _collect(child, parent)

    assert blocked, "父行改归属没有被子行的 FOR SHARE 挡住"
    assert child_outcome == "committed"
    assert parent_outcome.startswith("rejected"), parent_outcome
    assert "parent ownership guard violated" in parent_outcome
    assert await _mismatched_chunk_count(manager) == 0


async def test_concurrent_parent_move_then_child_insert_commits_no_mismatch(isolated_tenant_schema):
    """父行先改归属（未提交）× 子行插入：对方必须等到父行落定后再读，读到新值就拒。

    没有 `FOR SHARE` 时子行读的是**旧版本**（旧值仍是 `kb-a`），于是"检查通过"和"父行改走"
    会一起提交成失配。
    """

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-f', 'kb-a', 'f.txt')",
    )

    parent_ready = asyncio.Event()
    release = asyncio.Event()
    child_pid: list[int] = []
    child_started = asyncio.Event()

    async def parent_side() -> str:
        async with _transaction(manager) as transaction:
            rejected = await _stage(
                transaction,
                "UPDATE knowledge_files SET kb_id = 'kb-b' WHERE file_id = 'file-f'",
                done=parent_ready,
            )
            if rejected is not None:
                return rejected
            await _barrier(release, "主协程放行")
            return await transaction.commit_outcome()

    async def child_side() -> str:
        await _barrier(parent_ready, "父行改归属就绪")
        async with _transaction(manager) as transaction:
            child_pid.append(transaction.pid)
            child_started.set()
            return await _settle(transaction, _chunk_insert("chunk-c", "file-f", "kb-a"))

    parent = _spawn(manager, parent_side())
    await _barrier(parent_ready, "父行改归属就绪")
    child = _spawn(manager, child_side())
    await _barrier(child_started, "子行开始")
    blocked = await _wait_until_blocked(manager, child_pid[0])
    release.set()
    parent_outcome, child_outcome = await _collect(parent, child)

    assert blocked, "子行插入没有被父行的行锁挡住"
    assert parent_outcome == "committed"
    assert child_outcome.startswith("rejected"), child_outcome
    assert "reference consistency violated" in child_outcome
    assert await _scalar(manager, "SELECT kb_id FROM knowledge_files WHERE file_id = 'file-f'") == "kb-b"
    assert await _mismatched_chunk_count(manager) == 0


async def test_concurrent_child_repoint_then_parent_move_commits_no_mismatch(isolated_tenant_schema):
    """子行**改父引用** × 父行改归属：与插入同类，也必须串行。

    没有 `FOR SHARE` 时子行读到父行的旧值 `kb-a`（与新引用同库）而放行，父行随后改成 `kb-b`
    ——`chunk.kb_id = kb-a` 指向 `file-b.kb_id = kb-b`，失配落库。
    """

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES "
        "('file-a', 'kb-a', 'a.txt'), ('file-b', 'kb-a', 'b.txt')",
        _chunk_insert("chunk-c", "file-a", "kb-a"),
    )

    parent_ready = asyncio.Event()
    release = asyncio.Event()
    child_pid: list[int] = []
    child_started = asyncio.Event()

    async def parent_side() -> str:
        async with _transaction(manager) as transaction:
            rejected = await _stage(
                transaction,
                "UPDATE knowledge_files SET kb_id = 'kb-b' WHERE file_id = 'file-b'",
                done=parent_ready,
            )
            if rejected is not None:
                return rejected
            await _barrier(release, "主协程放行")
            return await transaction.commit_outcome()

    async def child_side() -> str:
        await _barrier(parent_ready, "父行改归属就绪")
        async with _transaction(manager) as transaction:
            child_pid.append(transaction.pid)
            child_started.set()
            return await _settle(
                transaction, "UPDATE knowledge_chunks SET file_id = 'file-b' WHERE chunk_id = 'chunk-c'"
            )

    parent = _spawn(manager, parent_side())
    await _barrier(parent_ready, "父行改归属就绪")
    child = _spawn(manager, child_side())
    await _barrier(child_started, "子行开始")
    blocked = await _wait_until_blocked(manager, child_pid[0])
    release.set()
    parent_outcome, child_outcome = await _collect(parent, child)

    assert blocked, "子行改引用没有被父行的行锁挡住"
    assert parent_outcome == "committed"
    assert child_outcome.startswith("rejected"), child_outcome
    assert "reference consistency violated" in child_outcome
    assert await _mismatched_chunk_count(manager) == 0


async def test_parent_locks_within_one_row_are_taken_in_a_stable_order(isolated_tenant_schema):
    """一行引用**同一张表**的两个父行时，取锁顺序必须按「实际父行」排。

    `knowledge_graph_triples` 的 `source_entity_id` / `target_entity_id` 是同表同键列，按列名
    排出的次序是固定的，但两个事务里两个 ID 的角色可以互换（A→B 与 B→A）——只按列名排序
    仍会反向取锁。

    观测方式：让另一个事务先占住 `ent-b`，再插入 `ent-b → ent-a`。正确实现会**先拿到 `ent-a`**
    再阻塞在 `ent-b` 上，于是第三个会话对 `ent-a` 的 `FOR UPDATE` 会被挡住；若按列名顺序取锁
    （source 先），插入方会直接阻塞在 `ent-b`、手里什么都没有，那个 `FOR UPDATE` 会立刻成功。

    占住 `ent-b` 用的是 **`FOR NO KEY UPDATE`** 而不是 `FOR SHARE`——`FOR SHARE` 与 `FOR SHARE`
    **不冲突**，用它占位根本挡不住插入方；`FOR NO KEY UPDATE` 正是 `UPDATE ... SET kb_id`
    会取的那个锁。`blocked` 本身不具判别性（两种取锁顺序都会阻塞），判别的是
    `probe_was_blocked`：它证明插入方在等着 `ent-b` 的同时**手里握着 `ent-a`**。
    """

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES ('kb-a', 'A 的库', 'milvus', 1)",
        "INSERT INTO knowledge_graph_entities "
        "(entity_id, kb_id, normalized_name, label, name, vector_status, vector_attempt_count) "
        "VALUES ('ent-a', 'kb-a', 'a', 'ORG', 'A', 'pending', 0), "
        "('ent-b', 'kb-a', 'b', 'ORG', 'B', 'pending', 0)",
    )

    subject_pid: list[int] = []
    subject_started = asyncio.Event()

    async def insert_reversed_reference() -> str:
        async with _transaction(manager) as transaction:
            subject_pid.append(transaction.pid)
            subject_started.set()
            return await _settle(
                transaction,
                "INSERT INTO knowledge_graph_triples "
                "(triple_id, kb_id, source_entity_id, target_entity_id, relation_type, content, "
                "vector_status, vector_attempt_count) "
                "VALUES ('tri-x', 'kb-a', 'ent-b', 'ent-a', 'rel', 'c', 'pending', 0)",
            )

    async with _transaction(manager) as holder:
        await holder.execute(
            "SELECT 1 FROM knowledge_graph_entities WHERE entity_id = 'ent-b' FOR NO KEY UPDATE"
        )
        subject = _spawn(manager, insert_reversed_reference())
        await _barrier(subject_started, "插入方开始")
        blocked = await _wait_until_blocked(manager, subject_pid[0])

        # 插入方此刻应当握着 ent-a 的 FOR SHARE；用 FOR UPDATE 去探，必须被挡住。
        probe_was_blocked = False
        async with _transaction(manager) as probe:
            await probe.execute("SET LOCAL lock_timeout = '300ms'")
            try:
                await probe.execute(
                    "SELECT 1 FROM knowledge_graph_entities WHERE entity_id = 'ent-a' FOR UPDATE"
                )
            except Exception:  # noqa: BLE001 - 这里断言的正是"被挡住"
                probe_was_blocked = True

        await holder.commit()
        (subject_outcome,) = await _collect(subject)

    assert blocked, "插入方没有在锁上等，取锁顺序不对"
    assert probe_was_blocked, "插入方手里没有 ent-a 的锁——取锁顺序不是按实际父行排的"
    assert subject_outcome == "committed", subject_outcome
    assert await _scalar(manager, "SELECT count(*) FROM knowledge_graph_triples") == 1


async def test_swapped_references_in_two_transactions_do_not_deadlock(isolated_tenant_schema):
    """两个并发事务以相反顺序引用同一组父行（triples 两引用互换）不得死锁。

    正确性来自"按实际父行排序取锁"：两边都先取 `ent-a`，后到的一方只是等，不成环。
    **证据边界**：这条用例只覆盖本文定义的这一种交错（两个事务各一条插入），取锁顺序本身的
    判别性证据在 `test_parent_locks_within_one_row_are_taken_in_a_stable_order`；本用例不承诺
    任意业务事务都绝无死锁。
    """

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES ('kb-a', 'A 的库', 'milvus', 1)",
        "INSERT INTO knowledge_graph_entities "
        "(entity_id, kb_id, normalized_name, label, name, vector_status, vector_attempt_count) "
        "VALUES ('ent-a', 'kb-a', 'a', 'ORG', 'A', 'pending', 0), "
        "('ent-b', 'kb-a', 'b', 'ORG', 'B', 'pending', 0)",
    )

    release = asyncio.Event()
    gates = [asyncio.Event(), asyncio.Event()]

    async def insert(triple_id: str, source: str, target: str, gate: asyncio.Event) -> str:
        async with _transaction(manager) as transaction:
            gate.set()
            await _barrier(release, "主协程放行")
            return await _settle(
                transaction,
                "INSERT INTO knowledge_graph_triples "
                "(triple_id, kb_id, source_entity_id, target_entity_id, relation_type, content, "
                "vector_status, vector_attempt_count) "
                f"VALUES ('{triple_id}', 'kb-a', '{source}', '{target}', 'rel', 'c', 'pending', 0)",
            )

    first = _spawn(manager, insert("tri-1", "ent-a", "ent-b", gates[0]))
    await _barrier(gates[0], "第一个插入方就位")
    second = _spawn(manager, insert("tri-2", "ent-b", "ent-a", gates[1]))
    await _barrier(gates[1], "第二个插入方就位")
    release.set()

    outcomes = await _collect(first, second)
    assert outcomes == ["committed", "committed"], outcomes
    assert await _scalar(manager, "SELECT count(*) FROM knowledge_graph_triples") == 2


@pytest.mark.parametrize(
    ("parent_update", "child_seed", "child_read"),
    [
        pytest.param(
            # 自引用链：父行与子行在**同一张表**里。这里要证的是父侧触发器对同表子行的
            # 可见性——它必须看得见刚写下的 file-x，而不是因为"同表"就漏掉。
            "UPDATE knowledge_files SET kb_id = 'kb-b' WHERE file_id = 'file-b'",
            (
                "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-b', 'kb-a', 'b.txt')",
                "INSERT INTO knowledge_files (file_id, kb_id, filename, parent_id) "
                "VALUES ('file-x', 'kb-a', 'x.txt', 'file-b')",
            ),
            "SELECT kb_id FROM knowledge_files WHERE file_id = 'file-b'",
            id="files_referenced_by_their_own_children",
        ),
        pytest.param(
            "UPDATE knowledge_graph_entities SET kb_id = 'kb-b' WHERE entity_id = 'entity-a'",
            (
                "INSERT INTO knowledge_graph_entities "
                "(entity_id, kb_id, normalized_name, label, name, vector_status, vector_attempt_count) "
                "VALUES ('entity-a', 'kb-a', 'a', 'ORG', 'A', 'pending', 0), "
                "('entity-b', 'kb-a', 'b', 'ORG', 'B', 'pending', 0)",
                "INSERT INTO knowledge_graph_triples "
                "(triple_id, kb_id, source_entity_id, target_entity_id, relation_type, content, "
                "vector_status, vector_attempt_count) "
                "VALUES ('tri-a', 'kb-a', 'entity-a', 'entity-b', 'rel', 'c', 'pending', 0)",
            ),
            "SELECT kb_id FROM knowledge_graph_entities WHERE entity_id = 'entity-a'",
            id="entities_referenced_by_a_triple",
        ),
        pytest.param(
            # 这条链自身的子侧规则（`file_id → file-a`）会先拒掉单纯的 `kb_id` 改动，所以更新
            # **同时**换文件到一个同库（kb-b）的文件上——子侧就此放行，拒绝只可能来自父行侧
            # （mention 仍然指着这个 chunk）。
            "UPDATE knowledge_chunks SET kb_id = 'kb-b', file_id = 'file-b' WHERE chunk_id = 'chunk-a'",
            (
                "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES "
                "('file-a', 'kb-a', 'a.txt'), ('file-b', 'kb-b', 'b.txt')",
                _chunk_insert("chunk-a", "file-a", "kb-a"),
                "INSERT INTO knowledge_graph_entities "
                "(entity_id, kb_id, normalized_name, label, name, vector_status, vector_attempt_count) "
                "VALUES ('entity-a', 'kb-a', 'a', 'ORG', 'A', 'pending', 0)",
                "INSERT INTO knowledge_graph_entity_mentions (entity_id, kb_id, file_id, chunk_id) "
                "VALUES ('entity-a', 'kb-a', 'file-a', 'chunk-a')",
            ),
            "SELECT kb_id FROM knowledge_chunks WHERE chunk_id = 'chunk-a'",
            id="chunks_referenced_by_a_mention",
        ),
        pytest.param(
            # 同上：两个实体引用一起换到 kb-b 的实体上，子侧放行，只剩父行侧（mention 指着这个
            # 三元组）会拒。
            "UPDATE knowledge_graph_triples SET kb_id = 'kb-b', source_entity_id = 'entity-c', "
            "target_entity_id = 'entity-d' WHERE triple_id = 'tri-a'",
            (
                "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-a', 'kb-a', 'a.txt')",
                _chunk_insert("chunk-a", "file-a", "kb-a"),
                "INSERT INTO knowledge_graph_entities "
                "(entity_id, kb_id, normalized_name, label, name, vector_status, vector_attempt_count) "
                "VALUES ('entity-a', 'kb-a', 'a', 'ORG', 'A', 'pending', 0), "
                "('entity-b', 'kb-a', 'b', 'ORG', 'B', 'pending', 0), "
                "('entity-c', 'kb-b', 'c', 'ORG', 'C', 'pending', 0), "
                "('entity-d', 'kb-b', 'd', 'ORG', 'D', 'pending', 0)",
                "INSERT INTO knowledge_graph_triples "
                "(triple_id, kb_id, source_entity_id, target_entity_id, relation_type, content, "
                "vector_status, vector_attempt_count) "
                "VALUES ('tri-a', 'kb-a', 'entity-a', 'entity-b', 'rel', 'c', 'pending', 0)",
                "INSERT INTO knowledge_graph_triple_mentions (triple_id, kb_id, file_id, chunk_id) "
                "VALUES ('tri-a', 'kb-a', 'file-a', 'chunk-a')",
            ),
            "SELECT kb_id FROM knowledge_graph_triples WHERE triple_id = 'tri-a'",
            id="triples_referenced_by_a_mention",
        ),
        pytest.param(
            "UPDATE evaluation_datasets SET kb_id = 'kb-b' WHERE dataset_id = 'ds-a'",
            (
                "INSERT INTO evaluation_datasets (dataset_id, kb_id, name) VALUES ('ds-a', 'kb-a', 'A 集')",
                "INSERT INTO evaluation_dataset_items (item_id, dataset_id, kb_id, item_index, query_text) "
                "VALUES ('item-a', 'ds-a', 'kb-a', 0, 'q')",
            ),
            "SELECT kb_id FROM evaluation_datasets WHERE dataset_id = 'ds-a'",
            id="datasets_referenced_by_an_item",
        ),
        pytest.param(
            # 同上：题目一起挪到 kb-b 的数据集上，子侧放行，只剩父行侧（结果行指着这个题目）会拒。
            "UPDATE evaluation_dataset_items SET kb_id = 'kb-b', dataset_id = 'ds-b' "
            "WHERE item_id = 'item-a'",
            (
                "INSERT INTO evaluation_datasets (dataset_id, kb_id, name) VALUES "
                "('ds-a', 'kb-a', 'A 集'), ('ds-b', 'kb-b', 'B 集')",
                "INSERT INTO evaluation_dataset_items (item_id, dataset_id, kb_id, item_index, query_text) "
                "VALUES ('item-a', 'ds-a', 'kb-a', 0, 'q')",
                "INSERT INTO evaluation_runs (run_id, name, kb_id) VALUES ('run-a', 'A 跑', 'kb-a')",
                "INSERT INTO evaluation_run_items (run_id, dataset_item_id, item_index, query_text) "
                "VALUES ('run-a', 'item-a', 0, 'q')",
            ),
            "SELECT kb_id FROM evaluation_dataset_items WHERE item_id = 'item-a'",
            id="dataset_items_referenced_by_a_run_item",
        ),
    ],
)
async def test_every_parent_table_in_the_reverse_lookup_refuses_to_move(
    isolated_tenant_schema, parent_update, child_seed, child_read
):
    """反查表上**每一条**链都要有父行侧防护，不能只覆盖被点名的几条。

    漏一条就是一个静默的失配入口，而漏掉的那条不会自己报错——它只会安静地让子行失配。

    其中三条链（chunks / triples / evaluation_dataset_items）自己的子侧规则会先拒掉单纯的
    `kb_id` 改动，所以那几条的更新**同时改掉引用列**，让子侧放行——这样"被拒"只可能来自
    父行侧。这不是绕开子侧规则，而是把它排除掉，让这条用例真的在测父行侧。
    """

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(manager, *child_seed)

    async with _transaction(manager) as transaction:
        outcome = await _settle(transaction, parent_update)

    assert outcome.startswith("rejected"), outcome
    assert "parent ownership guard violated" in outcome
    assert await _scalar(manager, child_read) == "kb-a"


# --------------------------------------------------------------------------------------
# E2. 受限角色下的父行侧防护（RLS 在这个夹具里是**启用**的）
#
# 并发用例走 owner 连接（行锁对 superuser 一样生效，且夹具写入的同租户跨库行需要全部可见），
# 所以它们**回答不了**"RLS 开着时父侧检查还看不看得见子行"。父侧触发器是 `SECURITY INVOKER`，
# 以 `yuxi_app` + 租户上下文执行时子行查询受策略约束——这一节就是这个问题的直接证据。
# --------------------------------------------------------------------------------------


async def test_the_parent_guard_still_sees_children_under_the_restricted_role(isolated_tenant_schema):
    """受限角色 + 正确的租户上下文改父行归属：子行**看得见**，所以必须被拒。

    父子同租户是子侧规则在写入时就保证的不变量，于是同租户的子行在策略下可见，父侧检查不会
    因为 RLS 误判成"没有子行"。**范围**：这条只覆盖版本 13 的形态与"新写入"的数据。
    """

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-b', 'kb-b', 'b.txt')",
        _chunk_insert("chunk-b", "file-b", "kb-b"),
    )

    with pytest.raises(Exception, match="parent ownership guard violated"):
        async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
            await connection.execute(
                text("UPDATE knowledge_files SET kb_id = 'kb-a' WHERE file_id = 'file-b'")
            )

    assert await _scalar(manager, "SELECT kb_id FROM knowledge_files WHERE file_id = 'file-b'") == "kb-b"


async def test_without_a_tenant_context_the_parent_row_itself_is_invisible(isolated_tenant_schema):
    """没有租户上下文时，`UPDATE` 连**父行自己**都匹配不到——0 行受影响，归属改不走。

    这是"RLS 会不会把子行藏起来让父侧检查误判"的另一半：挡住子行的同时父行也被挡住，所以那条
    路径提交不了。**残余风险**是守卫上线**之前**写入的跨租户存量行——RLS 会把它们藏起来，
    而触发器不校验存量。版本 14 启用 RLS 前必须查存量（已记入 spec 的验收清单）。
    """

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-b', 'kb-b', 'b.txt')",
        _chunk_insert("chunk-b", "file-b", "kb-b"),
    )

    async with manager.async_engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL ROLE {TENANT_RUNTIME_ROLE}"))
        result = await connection.execute(
            text("UPDATE knowledge_files SET kb_id = 'kb-a' WHERE file_id = 'file-b'")
        )

    assert result.rowcount == 0
    assert await _scalar(manager, "SELECT kb_id FROM knowledge_files WHERE file_id = 'file-b'") == "kb-b"


# --------------------------------------------------------------------------------------
# E2b. RLS 下父侧检查的**隐藏行**：被藏起来的子行能不能在守卫上线**之后**产生
#
# 父侧触发器是 `SECURITY INVOKER`，它查子行时受 RLS 约束。上面两条分别证明"同租户子行看得见"
# 与"无上下文时父行自己被挡"。这一节回答剩下那个问题——**"父侧看不见但必须检查的行"是否
# 能在守卫上线之后被写出来**。分三种情形：
#
#   1. 子行自身的 kb 落在本租户、父引用指向别的租户 → **子侧触发器**拒（RLS 放行，触发器 fail-closed）；
#   2. 子行自身的 kb 落在别的租户 → RLS `WITH CHECK` 与子侧触发器都会拒；
#   3. 改父行归属时把 kb 改到别的租户 → RLS `WITH CHECK` 拒。
#
# 三条合起来的结论是：**新写入不会产生父侧看不见的子行**。因此父侧看不见的行只可能是
# **守卫上线之前**写入的存量——第 4 条把这条 fail-open 路径**实测出来**（而不是"应该没问题"）。
# 它由"启用 RLS 前的存量预检"覆盖，不在运行期触发器能力范围内，如实记名。
# --------------------------------------------------------------------------------------


async def _two_tenants(manager: PostgresManager) -> None:
    """两个**不同租户**的知识库：租户 1 的 `kb-a` / `kb-a2`，租户 2 的 `kb-b`。"""

    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) VALUES "
        "('kb-a', '一的库', 'milvus', 1), ('kb-a2', '一又的库', 'milvus', 1), "
        "('kb-b', '二的库', 'milvus', 2)",
    )


async def test_a_child_in_this_tenant_cannot_reference_another_tenants_parent(isolated_tenant_schema):
    """租户 1 写一条 `kb_id` 属于租户 1、父引用却指向租户 2 文件的子行：**必须被拒**。

    这是"新写入不会产生隐藏子行"的直接证据，而且拒绝来自**子侧触发器**——子行自身 `kb_id` 在
    本租户内（RLS `WITH CHECK` 放行），但父行在租户 1 上下文里不可见，`IS DISTINCT FROM` 判为
    不一致从而拒绝。也就是说这条路是 **fail-closed** 的，不是"看不见就当没有"。
    """

    manager = isolated_tenant_schema
    await _two_tenants(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-b', 'kb-b', 'b.txt')",
    )

    outcome = await _settle_as_tenant(
        manager,
        DEFAULT_TENANT_ID,
        "INSERT INTO knowledge_files (file_id, kb_id, filename, parent_id) "
        "VALUES ('file-x', 'kb-a', 'x.txt', 'file-b')",
    )

    assert outcome.startswith("rejected")
    assert "reference consistency violated" in outcome
    assert await _scalar(manager, "SELECT count(*) FROM knowledge_files WHERE file_id = 'file-x'") == 0


async def test_a_child_declared_for_another_tenant_is_rejected(isolated_tenant_schema):
    """租户 1 写一条 `kb_id` 属于**租户 2** 的子行：必须被拒。

    与上一条互不替代：上一条 RLS 放行、靠触发器拒；这一条在 RLS 层就被 `WITH CHECK` 拒。两条
    合起来说明——新写入的跨租户子行建不出来，**无论它自身的 kb 落在哪个租户**。这条不区分拒绝
    来自哪一层，只断言"建不出来"。
    """

    manager = isolated_tenant_schema
    await _two_tenants(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-a', 'kb-a', 'a.txt')",
    )

    outcome = await _settle_as_tenant(
        manager,
        DEFAULT_TENANT_ID,
        "INSERT INTO knowledge_files (file_id, kb_id, filename, parent_id) "
        "VALUES ('file-y', 'kb-b', 'y.txt', 'file-a')",
    )

    assert outcome.startswith("rejected")
    # 断言**拒绝的原因**，避免"因为别的约束（如 NOT NULL）碰巧也红"的假证据。
    assert "reference consistency violated" in outcome or "row-level security" in outcome
    assert await _scalar(manager, "SELECT count(*) FROM knowledge_files WHERE file_id = 'file-y'") == 0


async def test_moving_a_parent_into_another_tenants_kb_is_blocked_by_rls(isolated_tenant_schema):
    """把租户 1 的文件改到租户 2 的知识库：RLS `WITH CHECK` 拒，归属改不走。

    这挡住的是"把整行搬到另一个租户"这条最直接的跨租户写入，与父侧触发器无关。
    """

    manager = isolated_tenant_schema
    await _two_tenants(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-a', 'kb-a', 'a.txt')",
    )

    outcome = await _settle_as_tenant(
        manager,
        DEFAULT_TENANT_ID,
        "UPDATE knowledge_files SET kb_id = 'kb-b' WHERE file_id = 'file-a'",
    )

    assert outcome.startswith("rejected")
    assert "row-level security" in outcome
    assert await _scalar(manager, "SELECT kb_id FROM knowledge_files WHERE file_id = 'file-a'") == "kb-a"


async def test_the_parent_guard_sees_a_visible_child_but_not_one_hidden_by_rls(isolated_tenant_schema):
    """**同一形状、同一次归属变更，唯一的差别是子行的租户**：可见的被拦，被藏起来的放行。

    这是 fail-open 的**判别性证据**——单看"committed"不说明任何事（无子行的父行本来就能改），
    所以必须让"可见子行 → 拒"与"隐藏子行 → 放行"成对出现，两次用**不同的父行**、完全相同的
    语句形状。

    隐藏那条对应**守卫上线之前**写入的存量跨租户子行（这里临时停掉子侧触发器来模拟，真库里
    这类行产生于版本 14 启用触发器之前）。结论（不要放大）：运行期触发器在 RLS 下**不校验
    存量**，因此版本 14 启用 RLS 前必须做**存量预检**（父行与子行租户一致 + 同库引用一致），
    发现异常即失败、不自动修数据。这是触发器的能力边界，不是实现缺陷。
    """

    manager = isolated_tenant_schema
    await _two_tenants(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES "
        "('file-visible', 'kb-a', 'v.txt'), ('file-hidden', 'kb-a', 'h.txt')",
    )

    # (1) 可见子行：与父**同租户**（kb-a）。父行改到 kb-a2 会让它失配 → 父侧检查看得见 → 拒。
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename, parent_id) "
        "VALUES ('child-visible', 'kb-a', 'cv.txt', 'file-visible')",
    )
    visible_outcome = await _settle_as_tenant(
        manager,
        DEFAULT_TENANT_ID,
        "UPDATE knowledge_files SET kb_id = 'kb-a2' WHERE file_id = 'file-visible'",
    )
    assert visible_outcome.startswith("rejected")
    assert "parent ownership guard violated" in visible_outcome
    assert await _scalar(manager, "SELECT kb_id FROM knowledge_files WHERE file_id = 'file-visible'") == "kb-a"

    # (2) 隐藏子行：租户 2（存量）。停掉子侧触发器写进去，再恢复——模拟"守卫还不存在时写入"。
    await _seed(
        manager,
        "ALTER TABLE knowledge_files "
        "DISABLE TRIGGER enforce_knowledge_files_tenant_reference_consistency",
    )
    try:
        await _seed(
            manager,
            "INSERT INTO knowledge_files (file_id, kb_id, filename, parent_id) "
            "VALUES ('child-hidden', 'kb-b', 'ch.txt', 'file-hidden')",
        )
    finally:
        await _seed(
            manager,
            "ALTER TABLE knowledge_files "
            "ENABLE TRIGGER enforce_knowledge_files_tenant_reference_consistency",
        )

    # 同一形状的变更，只把父行换成 file-hidden：那条子行（租户 2）被 RLS 藏住 → 检查看不见 → 放行。
    # 改成跨租户的库会被 RLS 挡住（上一条用例），所以这里必须改到同租户的 kb-a2。
    hidden_outcome = await _settle_as_tenant(
        manager,
        DEFAULT_TENANT_ID,
        "UPDATE knowledge_files SET kb_id = 'kb-a2' WHERE file_id = 'file-hidden'",
    )
    assert hidden_outcome == "committed", "父侧检查看不见被 RLS 藏起来的存量跨租户子行——这正是要记名的缺口"
    assert await _scalar(manager, "SELECT kb_id FROM knowledge_files WHERE file_id = 'file-hidden'") == "kb-a2"


# --------------------------------------------------------------------------------------
# E2c. 归属根变更与"同库"不变量：验证 E2b 结论的前提
#
# E2b 的结论（"新写入产生不出父侧看不见的子行"）依赖一条不变量：**子行与它引用的父行必在同一
# 个知识库**——子侧触发器比的是 `child.kb == parent.kb`（不是租户）。这条不变量若成立，改任何
# kb 的 `tenant_id` 都会让该 kb 的父行与子行**一起**换租户，不会单独把子行藏起来。
#
# 独立审查提出的反例是"改 kb 的 tenant_id 会不会让已有子行变成父侧不可见"。这里**实测**它：
# 先证明"同租户、不同库"的子行建不出来（不变量成立），再证明改 kb 租户后父行在旧租户里**根本
# 改不动**（0 行）——两条合起来说明该反例走不通。
#
# 同时如实记名一处**未加守卫**的事实：`knowledge_bases` 是归属根却不在
# `TENANT_PARENT_OWNERSHIP_RULES` 里，改它的 `tenant_id` 没有任何触发器拦（第三条用例实测）。
# 它不是 E2b 的反例（耦合使然），但属**纵深防御缺口**，记入 spec §6.10。
# --------------------------------------------------------------------------------------


async def test_a_child_cannot_be_placed_in_a_different_knowledge_base_of_the_same_tenant(isolated_tenant_schema):
    """**同租户、不同知识库**的父子引用同样被拒——这是 E2b 结论依赖的那条不变量。

    子侧触发器比的是 `child.kb == parent.kb`（不是租户），所以"把子行放到另一个库"在任何租户下
    都建不出来。正因如此，改某个 kb 的租户会让它的父子行**一起**换租户，不会单独把子行藏起来。
    """

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)  # kb-a 与 kb-b，同为租户 1
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-a', 'kb-a', 'a.txt')",
    )

    outcome = await _settle_as_tenant(
        manager,
        DEFAULT_TENANT_ID,
        "INSERT INTO knowledge_files (file_id, kb_id, filename, parent_id) "
        "VALUES ('child-b', 'kb-b', 'c.txt', 'file-a')",
    )

    assert outcome.startswith("rejected")
    assert "reference consistency violated" in outcome
    assert await _scalar(manager, "SELECT count(*) FROM knowledge_files WHERE file_id = 'child-b'") == 0


async def test_changing_a_knowledge_bases_tenant_does_not_leave_a_movable_parent_with_hidden_children(
    isolated_tenant_schema,
):
    """把 kb 的租户改掉后，它在旧租户里**连父行都改不动**（0 行）——审查提的反例走不通。

    父子必同库（上一条），所以改 `kb-a.tenant_id` 会让 `file-p` 与它的子行**一起**离开租户 1。
    旧租户 1 再看 `file-p` 时它已被 RLS 挡住，`UPDATE` 影响 0 行，于是"父行可改、子行被藏"这个
    组合无法同时成立。
    """

    manager = isolated_tenant_schema
    await _two_tenants(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) "
        "VALUES ('kb-a3', '一三的库', 'milvus', 1)",
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-p', 'kb-a', 'p.txt')",
        "INSERT INTO knowledge_files (file_id, kb_id, filename, parent_id) "
        "VALUES ('child-p', 'kb-a', 'c.txt', 'file-p')",
    )
    # 归属根租户变更：现在被护栏拦住（见下一条用例），这里**显式停用护栏**来模拟"经授权的
    # 迁移流程"——只有那种流程才会去改归属根的租户。
    await _seed(
        manager,
        "ALTER TABLE knowledge_bases DISABLE TRIGGER enforce_knowledge_bases_tenant_immutable",
    )
    try:
        await _seed(manager, "UPDATE knowledge_bases SET tenant_id = 2 WHERE kb_id = 'kb-a'")
    finally:
        await _seed(
            manager,
            "ALTER TABLE knowledge_bases ENABLE TRIGGER enforce_knowledge_bases_tenant_immutable",
        )

    async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
        result = await connection.execute(
            text("UPDATE knowledge_files SET kb_id = 'kb-a3' WHERE file_id = 'file-p'")
        )

    assert result.rowcount == 0, "父行随 kb 一起换了租户，旧租户不该还能改它"
    assert await _scalar(manager, "SELECT kb_id FROM knowledge_files WHERE file_id = 'file-p'") == "kb-a"
    assert await _scalar(manager, "SELECT count(*) FROM knowledge_files WHERE file_id = 'child-p'") == 1


async def test_a_root_tables_tenant_is_immutable_on_the_owner_path(isolated_tenant_schema):
    """归属根的 `tenant_id` 在普通更新路径上**不可变**：连 owner 连接也被拒。

    改归属根的租户会把整棵归属子树（派生表靠外键链）一起搬走，属**数据契约**变更，不是普通
    字段更新。RLS 只挡受限角色（`WITH CHECK` 比 `tenant_id`），**owner 绕过 RLS**，所以必须有
    数据库层护栏。**不承诺**阻止超级用户主动 `DISABLE TRIGGER`——那属运维信任边界；真正的
    跨租户迁移走"经授权、可审计"的流程（显式停用护栏并留痕）。
    """

    manager = isolated_tenant_schema
    await _two_tenants(manager)

    async with _transaction(manager) as transaction:
        outcome = await _settle(
            transaction, "UPDATE knowledge_bases SET tenant_id = 2 WHERE kb_id = 'kb-a'"
        )

    assert outcome.startswith("rejected")
    assert "tenant ownership is immutable" in outcome
    assert await _scalar(manager, "SELECT tenant_id FROM knowledge_bases WHERE kb_id = 'kb-a'") == 1


async def test_every_root_table_carries_the_tenant_immutability_guard(isolated_tenant_schema):
    """**覆盖断言，不是行为证据**：护栏要装在**全部**归属根上，不只知识库。

    行为语义由上面那条用例证明；这条只守"将来加了归属根却忘了装护栏"这种不会自己报错的漂移。
    查的是 `pg_trigger` 目录（按 `current_schema()` 限定到本用例的隔离 schema），不是 SQL 文本。
    """

    manager = isolated_tenant_schema
    installed = await _scalar(
        manager,
        "SELECT count(*) FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE NOT t.tgisinternal "
        "  AND n.nspname = current_schema() "
        "  AND t.tgname = 'enforce_' || c.relname || '_tenant_immutable'",
    )
    assert installed == len(TENANT_ROOT_TABLES), (
        f"护栏只装在 {installed} 张表上，期望 {len(TENANT_ROOT_TABLES)}（{TENANT_ROOT_TABLES}）"
    )


async def test_the_root_guard_install_is_idempotent_on_replay(isolated_tenant_schema):
    """**正常重放**：夹具已装过一次，再装两次不得报错，也不得装出第二套护栏。

    正常重放之所以安全，是因为**此前迁移已完整完成**（`tenant_id` 已是 NOT NULL、无 NULL 行），
    **不是**"回填排在护栏安装之前"这种语句顺序——护栏一旦存在，回填同样会被它拦下。
    """

    manager = isolated_tenant_schema
    await manager.ensure_tenant_root_immutability()
    await manager.ensure_tenant_root_immutability()

    installed = await _scalar(
        manager,
        "SELECT count(*) FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE NOT t.tgisinternal "
        "  AND n.nspname = current_schema() "
        "  AND t.tgname = 'enforce_' || c.relname || '_tenant_immutable'",
    )
    assert installed == len(TENANT_ROOT_TABLES)


async def test_a_stale_null_tenant_row_blocks_the_guard_install_with_a_repair_hint(isolated_tenant_schema):
    """**异常 / 部分安装状态**：仍有 `tenant_id IS NULL` 的行时必须**明确拒绝并提示修复**。

    不关护栏、也不自动回填——否则报出来的会是护栏那句误导性的 "tenant ownership is immutable"，
    把"上一轮迁移没跑完"说成"这是一次非法迁移"。
    """

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "ALTER TABLE departments ALTER COLUMN tenant_id DROP NOT NULL",
        "INSERT INTO departments (id, name, tenant_id) VALUES (9, '无主部门', NULL)",
    )

    with pytest.raises(RuntimeError, match="不要停用护栏"):
        await manager.ensure_tenant_root_immutability()


async def test_rls_with_check_still_rejects_a_cross_tenant_update(isolated_tenant_schema):
    """**RLS 对 UPDATE 的保护要单独留证**：不可变护栏现在先拦 UPDATE，会把它盖住。

    这里**临时停用不可变护栏**（只在本用例的隔离 schema 内），证明拒绝来自 RLS `WITH CHECK`
    本身——否则"护栏有效"会被误读成"RLS 对 UPDATE 有效"。INSERT 侧的证据不能替代它。
    """

    manager = isolated_tenant_schema
    await _seed(manager, "INSERT INTO departments (id, name, tenant_id) VALUES (3, '本家部门', 1)")
    await _seed(manager, "ALTER TABLE departments DISABLE TRIGGER enforce_departments_tenant_immutable")
    try:
        with pytest.raises(Exception, match="row-level security"):
            async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
                await connection.execute(text("UPDATE departments SET tenant_id = 2 WHERE id = 3"))
    finally:
        await _seed(manager, "ALTER TABLE departments ENABLE TRIGGER enforce_departments_tenant_immutable")

    assert await _scalar(manager, "SELECT tenant_id FROM departments WHERE id = 3") == 1


async def test_without_a_tenant_context_writes_are_rejected(isolated_tenant_schema):
    """受限角色**没有**租户上下文时写入被拒——不是"写进默认租户"。

    `current_setting('app.tenant_id', true)` 为空 → 策略判据为 NULL → `WITH CHECK` 不成立 → 拒。
    **作用域**：这条证明的是**数据库层**。应用层的运行时注入也已去掉默认回落
    （`_apply_tenant_context` 在 `explicit_tenant_id()` 为 None 时**跳过注入**），所以 ORM 路径
    在缺上下文时不再带租户 1——缺上下文与"租户 1"可区分。显式建立租户的入口见 API 中间件、
    API / worker 启动的全局初始化与 worker 消费任务时的持久化租户（spec §6.10 与 worker 接线）。
    """

    manager = isolated_tenant_schema
    await _seed(
        manager,
        "INSERT INTO knowledge_bases (kb_id, name, kb_type, tenant_id) "
        "VALUES ('kb-a', '一的库', 'milvus', 1)",
    )

    async with manager.async_engine.begin() as connection:
        await connection.execute(text(f"SET LOCAL ROLE {TENANT_RUNTIME_ROLE}"))
        with pytest.raises(Exception):
            await connection.execute(
                text(
                    "INSERT INTO knowledge_files (file_id, kb_id, filename) "
                    "VALUES ('file-noctx', 'kb-a', 'x.txt')"
                )
            )

    assert await _scalar(manager, "SELECT count(*) FROM knowledge_files WHERE file_id = 'file-noctx'") == 0


# --------------------------------------------------------------------------------------
# E3. 规则表的结构守卫
# --------------------------------------------------------------------------------------


async def test_every_child_side_reference_has_a_parent_side_guard():
    """`TENANT_REFERENCE_RULES` 里每一条引用，父侧都必须在 `TENANT_PARENT_OWNERSHIP_RULES`
    里有对应条目（子表、引用列、子行自身的 kb 列都要对上）。

    守的是**覆盖关系**，不是 SQL 形状：将来加一条引用规则却忘了加父侧防护时，那种遗漏不会自己
    报错，只会安静地让父行归属被改走。**它证明的只是"规则表对得上"**，真实拒绝行为由 E 段的
    行为用例负责——不要把这条当行为证据。

    唯一的例外是刻意多加的 `evaluation_runs → evaluation_run_items.run_id`（比 `run_id` 而不是
    可空的 `dataset_item_id`）：它**不在**子侧规则里，所以这里只断言"子侧有的父侧都有"这一个方向。
    """

    for child, rules in TENANT_REFERENCE_RULES.items():
        child_kb = None if child in TENANT_REFERENCE_ROW_KB else "kb_id"
        for local_key, parent, _ in rules:
            assert parent in TENANT_PARENT_OWNERSHIP_RULES, f"{parent} 没有父侧防护规则"
            _, children = TENANT_PARENT_OWNERSHIP_RULES[parent]
            assert (child, local_key, child_kb) in children, f"{parent} 的父侧防护漏了 {child}.{local_key}"


# --------------------------------------------------------------------------------------
# F. 脚手架的失败路径：并发用例挂住时不得留下残留事务
#
# 这一段的理由是一个**已经发生过的**事故：断言在放行之前失败，两条挂着的写入事务把夹具的
# `DROP SCHEMA` 一起堵死，整套用例挂了 600 秒才被人手动停掉。修法有两层——
# 每一处等待都有界（`_barrier` / `statement_timeout`），以及夹具收尾先取消登记的任务。
# 下面两条用例分别证明这两层。
# --------------------------------------------------------------------------------------


async def test_every_case_transaction_is_bounded_and_read_committed(isolated_tenant_schema):
    """每条用例事务都装着**有限**超时，而且跑在本门槛声明的隔离级别上。

    断言的是**实际生效的服务端设置**，不是"我写了 `SET LOCAL`"：把夹具 `connect_args` 里那两项
    删掉，本用例立刻变红。隔离级别同理——并发结论只作用于 `read committed`
    （`grep isolation_level` 全仓无命中，走服务端默认），所以这里把它**断言**出来，
    将来谁改了默认级别，本用例会先红，而不是让并发结论悄悄失效。
    """

    manager = isolated_tenant_schema
    async with _transaction(manager) as transaction:
        assert await transaction.scalar("SHOW statement_timeout") == _STATEMENT_TIMEOUT
        assert (
            await transaction.scalar("SHOW idle_in_transaction_session_timeout")
            == _IDLE_TRANSACTION_TIMEOUT
        )
        assert await transaction.scalar("SHOW transaction_isolation") == "read committed"


async def test_a_blocked_round_cancels_within_its_own_timeout_and_frees_the_lock(isolated_tenant_schema):
    """超时路径：一边握着行锁不放、另一边卡在锁上，`_collect` 必须在**它自己的超时内**收场。

    判据是**功能性的**：失败之后那条行锁必须立刻能被别人拿到（事务真的结束了），
    并且没有后端停在 `idle in transaction` 上。"任务被取消了"本身不算证据。
    """

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-f', 'kb-a', 'f.txt')",
    )

    release = asyncio.Event()  # **永不** set：模拟"主协程再也不会放行"
    holder_ready = asyncio.Event()
    holder_pid: list[int] = []
    waiter_started = asyncio.Event()
    waiter_pid: list[int] = []

    async def holder() -> str:
        async with _transaction(manager) as transaction:
            holder_pid.append(transaction.pid)
            await transaction.execute(
                "SELECT 1 FROM knowledge_files WHERE file_id = 'file-f' FOR NO KEY UPDATE"
            )
            holder_ready.set()
            await _barrier(release, "主协程放行")
            return await transaction.commit_outcome()

    async def waiter() -> str:
        await _barrier(holder_ready, "持有方就绪")
        async with _transaction(manager) as transaction:
            waiter_pid.append(transaction.pid)
            waiter_started.set()
            return await _settle(
                transaction, "SELECT 1 FROM knowledge_files WHERE file_id = 'file-f' FOR UPDATE"
            )

    holder_task = _spawn(manager, holder())
    await _barrier(holder_ready, "持有方就绪")
    waiter_task = _spawn(manager, waiter())
    await _barrier(waiter_started, "等待方开始")
    assert await _wait_until_blocked(manager, waiter_pid[0]), "等待方没有卡在锁上，用例失去意义"

    loop = asyncio.get_running_loop()
    started = loop.time()
    outcomes = await _collect(holder_task, waiter_task, timeout=0.5)
    elapsed = loop.time() - started

    assert elapsed < 5.0, f"取消没有在自己的超时附近收场，用了 {elapsed:.1f}s"
    assert all(outcome.startswith("<未在超时内收场") for outcome in outcomes), outcomes
    await _assert_lock_is_free(
        manager, "SELECT 1 FROM knowledge_files WHERE file_id = 'file-f' FOR UPDATE"
    )
    states = await _open_transaction_states(manager, holder_pid + waiter_pid)
    assert not any(state.startswith("idle in transaction") for state in states.values()), states


async def test_a_failing_case_that_leaves_tasks_running_is_still_cleaned_up(isolated_tenant_schema):
    """故障注入：断言在放行之前失败，两条事务都还挂着，**本用例故意不清理**。

    这正是最初挂死的形态。收尾交给夹具：它必须取消这两个任务、释放行锁、删掉 schema，并在
    自己的收尾断言里证明"没有事务还开着"（见 `isolated_tenant_schema` 的 `finally`）。
    清理失效时本用例会以挂死或收尾断言失败收场，不会静默变绿。
    """

    manager = isolated_tenant_schema
    await _two_knowledge_bases(manager)
    await _seed(
        manager,
        "INSERT INTO knowledge_files (file_id, kb_id, filename) VALUES ('file-f', 'kb-a', 'f.txt')",
    )

    release = asyncio.Event()  # **永不** set
    child_ready = asyncio.Event()
    parent_started = asyncio.Event()
    parent_pid: list[int] = []

    async def child_side() -> str:
        async with _transaction(manager) as transaction:
            rejected = await _stage(
                transaction, _chunk_insert("chunk-c", "file-f", "kb-a"), done=child_ready
            )
            if rejected is not None:
                return rejected
            await _barrier(release, "主协程放行")
            return await transaction.commit_outcome()

    async def parent_side() -> str:
        await _barrier(child_ready, "子行插入就绪")
        async with _transaction(manager) as transaction:
            parent_pid.append(transaction.pid)
            parent_started.set()
            return await _settle(
                transaction, "UPDATE knowledge_files SET kb_id = 'kb-b' WHERE file_id = 'file-f'"
            )

    _spawn(manager, child_side())
    await _barrier(child_ready, "子行插入就绪")
    _spawn(manager, parent_side())
    await _barrier(parent_started, "父行改归属开始")

    # 到这一步两个事务都挂着（一个握着 `FOR SHARE`、一个卡在 `FOR NO KEY UPDATE` 上），
    # 本用例就此结束——不做任何清理，让夹具去收。
    assert await _wait_until_blocked(manager, parent_pid[0]), "注入失败：父行没有被子行挡住"
