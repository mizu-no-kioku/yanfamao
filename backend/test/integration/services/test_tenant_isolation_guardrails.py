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

import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.evaluation_repository import EvaluationRepository
from yuxi.storage.postgres.manager import (
    TENANT_RUNTIME_ROLE,
    PostgresManager,
)
from yuxi.storage.postgres.tenant_context import DEFAULT_TENANT_ID

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
    return manager


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
    scoped_engine = create_async_engine(
        tenant_schema_url(),
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    manager = _scoped_manager(scoped_engine)
    await manager.create_business_tables()
    await manager.create_knowledge_tables()
    await manager.ensure_tenant_schema()
    await manager.ensure_tenant_rls()
    await manager.ensure_tenant_reference_consistency()
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
    try:
        yield manager
    finally:
        await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


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
    """WITH CHECK 同时挡住"把已属于自己的行改成别人的"。"""

    manager = isolated_tenant_schema
    await _seed(manager, "INSERT INTO departments (id, name, tenant_id) VALUES (3, '本家部门', 1)")

    with pytest.raises(Exception, match="row-level security"):
        async with _as_tenant(manager, DEFAULT_TENANT_ID) as connection:
            await connection.execute(text("UPDATE departments SET tenant_id = 2 WHERE id = 3"))


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
