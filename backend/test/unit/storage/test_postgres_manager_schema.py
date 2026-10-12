from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from yuxi.storage.postgres.manager import (
    BUSINESS_SCHEMA_VERSION,
    KNOWLEDGE_SCHEMA_VERSION,
    TENANT_ROOT_TABLES,
    BusinessBase,
    KnowledgeBase,
    PostgresManager,
)
from yuxi.storage.postgres.models_business import AgentRun, User


def test_business_and_knowledge_metadata_are_disjoint():
    """业务与知识域 metadata 保持独立，迁移器分别创建两个域。"""

    assert BusinessBase is not KnowledgeBase
    assert "users" in BusinessBase.metadata.tables
    assert "knowledge_bases" not in BusinessBase.metadata.tables
    assert "evaluation_runs" not in BusinessBase.metadata.tables
    assert "knowledge_bases" in KnowledgeBase.metadata.tables
    assert "evaluation_runs" in KnowledgeBase.metadata.tables
    assert "users" not in KnowledgeBase.metadata.tables


@pytest.mark.asyncio
async def test_require_current_schema_rejects_missing_or_incompatible_domains(monkeypatch):
    manager = PostgresManager()

    monkeypatch.setattr(manager, "get_schema_versions", lambda: _async_value({}))
    with pytest.raises(RuntimeError, match=r"business=missing .*knowledge=missing"):
        await manager.require_current_schema()

    monkeypatch.setattr(manager, "get_schema_versions", lambda: _async_value({"business": 99}))
    with pytest.raises(RuntimeError, match=r"business=99"):
        await manager.require_current_schema()

    monkeypatch.setattr(
        manager,
        "get_schema_versions",
        lambda: _async_value({"business": BUSINESS_SCHEMA_VERSION, "knowledge": KNOWLEDGE_SCHEMA_VERSION}),
    )
    await manager.require_current_schema()


async def _async_value(value):
    return value


def test_project_uid_foreign_key_has_schema_convergence_name():
    """ORM fresh schema 必须与后续收敛 SQL 使用同一 FK 名称。"""
    projects = BusinessBase.metadata.tables["projects"]

    assert [constraint.name for constraint in projects.foreign_key_constraints] == ["fk_projects_uid_users"]


def test_project_lifecycle_columns_and_constraint_are_in_fresh_schema():
    """Fresh schema 与升级收敛必须共享 Project 软删除契约。"""
    projects = BusinessBase.metadata.tables["projects"]

    assert projects.c.status.nullable is False
    assert "deleted_at" in projects.c
    assert "ck_projects_status" in {constraint.name for constraint in projects.constraints}


def test_agent_run_serialization_does_not_project_removed_redis_cursor():
    """AgentRun 序列化不再暴露已删除的 Redis 游标字段。"""
    run = AgentRun(
        id="run-1",
        conversation_thread_id="thread-1",
        runtime_scope_id="thread-1",
        agent_slug="main",
        uid="user-1",
        request_id="request-1",
        input_payload={},
    )

    assert "last_event_id" not in AgentRun.__table__.c
    assert "last_event_id" not in run.to_dict()


class _RecordingConnection:
    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))


class _RecordingBegin:
    def __init__(self, connection: _RecordingConnection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingEngine:
    def __init__(self, connection: _RecordingConnection):
        self.connection = connection

    def begin(self):
        return _RecordingBegin(self.connection)


@asynccontextmanager
async def _recording_manager():
    manager = PostgresManager()
    original_initialized = manager._initialized
    original_engine = manager.async_engine
    connection = _RecordingConnection()

    manager._initialized = True
    manager.async_engine = _RecordingEngine(connection)
    try:
        yield manager, connection
    finally:
        manager._initialized = original_initialized
        manager.async_engine = original_engine


@pytest.mark.asyncio
async def test_ensure_business_schema_backfills_subagent_thread_columns_before_dropping_legacy_columns():
    async with _recording_manager() as (manager, connection):
        await manager.ensure_business_schema()

    statements = "\n".join(connection.statements)

    assert "SET agent_slug = agent_id" in statements
    assert "SET conversation_thread_id = thread_id" in statements
    assert "SET created_by_run_id = COALESCE(parent_agent_run_id, parent_run_id)" in statements
    assert "SET subagent_slug = c.agent_id" in statements
    assert "SET created_by_run_id = created_by_parent_run_id::VARCHAR" in statements
    assert "ALTER COLUMN subagent_slug SET NOT NULL" in statements
    assert "ALTER COLUMN created_by_run_id SET NOT NULL" in statements
    assert statements.index("SET agent_slug = agent_id") < statements.index("DROP COLUMN IF EXISTS agent_id")
    assert statements.index("SET conversation_thread_id = thread_id") < statements.index(
        "DROP COLUMN IF EXISTS thread_id"
    )
    assert statements.index("COALESCE(parent_agent_run_id, parent_run_id)") < statements.index(
        "DROP COLUMN IF EXISTS parent_agent_run_id"
    )
    assert statements.index("created_by_parent_run_id") < statements.index(
        "DROP COLUMN IF EXISTS created_by_parent_run_id"
    )
    assert "ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'" in statements
    assert "ADD COLUMN IF NOT EXISTS deleted_at" in statements
    assert "ADD CONSTRAINT ck_projects_status" in statements


@pytest.mark.asyncio
async def test_release_upgrade_converges_run_timing_and_removes_cursor():
    """发布版升级使用同一套完整 DDL，包含全部模型前时间且删除旧游标。"""
    async with _recording_manager() as (manager, connection):
        await manager.ensure_business_schema()

    for column in ("prepared_at", "first_output_at", "first_model_request_at"):
        assert (
            f"ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS {column} TIMESTAMP WITHOUT TIME ZONE"
            in connection.statements
        )
    assert "ALTER TABLE IF EXISTS agent_runs DROP COLUMN IF EXISTS last_event_id" in connection.statements


@pytest.mark.asyncio
async def test_ensure_business_schema_cleans_duplicate_active_agent_runs_before_unique_index():
    async with _recording_manager() as (manager, connection):
        await manager.ensure_business_schema()

    statements = "\n".join(connection.statements)

    assert "WITH duplicated_active_runs AS" in statements
    assert "active_run_migration_conflict" in statements
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_one_active_per_thread" in statements
    assert statements.index("WITH duplicated_active_runs AS") < statements.index(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_one_active_per_thread"
    )


@pytest.mark.asyncio
async def test_ensure_business_schema_backfills_unviewed_marker_for_no_run_threads():
    """没有 chat/resume Run 的历史会话要写入未读哨兵，确保回填探测条件收敛为 false。"""
    async with _recording_manager() as (manager, connection):
        await manager.ensure_business_schema()

    statements = "\n".join(connection.statements)

    assert "SELECT EXISTS (SELECT 1 FROM conversations WHERE last_viewed_run_id IS NULL)" in statements
    assert "SET last_viewed_run_id = r.run_id" in statements
    assert "SET last_viewed_run_id = :marker WHERE last_viewed_run_id IS NULL" in statements
    assert statements.index("SET last_viewed_run_id = r.run_id") < statements.index(
        "SET last_viewed_run_id = :marker WHERE last_viewed_run_id IS NULL"
    )


@pytest.mark.asyncio
async def test_ensure_business_schema_creates_user_config_table():
    async with _recording_manager() as (manager, connection):
        await manager.ensure_business_schema()

    statements = "\n".join(connection.statements)

    assert "CREATE TABLE IF NOT EXISTS user_config" in statements
    assert "enable_memory BOOLEAN NOT NULL DEFAULT FALSE" in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_creates_generic_config_options_table():
    async with _recording_manager() as (manager, connection):
        await manager.ensure_business_schema()

    statements = "\n".join(connection.statements)

    assert "CREATE TABLE IF NOT EXISTS config_options" in statements
    assert "params JSONB NOT NULL" in statements
    assert "value JSONB NOT NULL" in statements
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ix_config_options_key" in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_adds_run_origin_snapshot_columns():
    async with _recording_manager() as (manager, connection):
        await manager.ensure_business_schema()

    statements = "\n".join(connection.statements)
    assert "agent_runs ADD COLUMN IF NOT EXISTS source VARCHAR(32)" in statements
    assert "agent_runs ADD COLUMN IF NOT EXISTS channel VARCHAR(32)" in statements
    assert "agent_runs ADD COLUMN IF NOT EXISTS external_id VARCHAR(128)" in statements
    assert "agent_runs ADD COLUMN IF NOT EXISTS origin_metadata JSONB" in statements
    assert "agent_run_requests ADD COLUMN IF NOT EXISTS channel VARCHAR(32)" in statements
    assert "agent_run_requests ADD COLUMN IF NOT EXISTS external_id VARCHAR(128)" in statements
    assert "agent_run_requests ADD COLUMN IF NOT EXISTS origin_metadata JSONB" in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_adds_idempotent_agent_run_lease_columns_and_index():
    async with _recording_manager() as (manager, connection):
        await manager.ensure_business_schema()

    statements = "\n".join(connection.statements)
    assert "agent_runs ADD COLUMN IF NOT EXISTS worker_id VARCHAR(128)" in statements
    assert "agent_runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP WITHOUT TIME ZONE" in statements
    assert "agent_runs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP WITHOUT TIME ZONE" in statements
    assert "CREATE INDEX IF NOT EXISTS ix_agent_runs_status_lease_expires" in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_adds_nonterminal_run_shape_constraint_without_scanning_history():
    async with _recording_manager() as (manager, connection):
        await manager.ensure_business_schema()

    statements = "\n".join(connection.statements)
    assert "ck_agent_runs_nonterminal_shape" in statements
    assert "run_type = 'resume'" in statements
    assert "run_type = 'subagent'" in statements
    assert "NOT VALID" in statements
    assert "EXCEPTION WHEN duplicate_object" in statements


@pytest.mark.asyncio
async def test_ensure_business_schema_removes_unbound_api_keys_before_requiring_user_id():
    async with _recording_manager() as (manager, connection):
        await manager.ensure_business_schema()

    statements = "\n".join(connection.statements)

    assert "UPDATE cli_auth_sessions" in statements
    assert "DELETE FROM api_keys WHERE user_id IS NULL" in statements
    assert "ALTER TABLE IF EXISTS api_keys ALTER COLUMN user_id SET NOT NULL" in statements
    assert statements.index("UPDATE cli_auth_sessions") < statements.index("DELETE FROM api_keys WHERE user_id IS NULL")
    assert statements.index("DELETE FROM api_keys WHERE user_id IS NULL") < statements.index(
        "ALTER TABLE IF EXISTS api_keys ALTER COLUMN user_id SET NOT NULL"
    )
    assert "ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS request_id VARCHAR(64)" in statements
    assert "ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS intent_hash VARCHAR(64)" in statements
    assert (
        "ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMP WITHOUT TIME ZONE" in statements
    )
    assert "users.is_deleted <> 0" in statements
    assert "COALESCE(api_key.revoked_at, users.deleted_at, CURRENT_TIMESTAMP)" in statements
    assert statements.index("ADD COLUMN IF NOT EXISTS revoked_at") < statements.index("users.is_deleted <> 0")
    assert statements.index("users.is_deleted <> 0") < statements.index(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_api_keys_request_id"
    )
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ix_api_keys_request_id" in statements
    assert "CREATE INDEX IF NOT EXISTS ix_api_keys_revoked_at" in statements


@pytest.mark.asyncio
async def test_share_config_migration_wraps_legacy_scopes_as_read_only():
    """Agent/skill 迁移只把旧 scope 写入 read_scope，manage_scope 置空，避免把历史只读/使用权限追溯升级为 MANAGE。"""
    async with _recording_manager() as (manager, connection):
        await manager.ensure_business_schema()
        await manager.ensure_knowledge_schema()

    statements = "\n".join(connection.statements)
    assert "UPDATE agents SET share_config = jsonb_build_object" in statements
    assert "UPDATE skills SET share_config = jsonb_build_object" in statements
    assert "UPDATE knowledge_bases SET share_config = jsonb_build_object" in statements
    assert "'read_scope'" in statements
    assert "'manage_scope', NULL" in statements
    assert "ALTER TABLE IF EXISTS agents ALTER COLUMN share_config TYPE JSONB USING share_config::jsonb" in statements
    assert "ALTER TABLE IF EXISTS skills ALTER COLUMN share_config TYPE JSONB USING share_config::jsonb" in statements
    assert statements.index(
        "ALTER TABLE IF EXISTS agents ALTER COLUMN share_config TYPE JSONB USING share_config::jsonb"
    ) < statements.index("UPDATE agents SET share_config = jsonb_build_object")
    assert statements.index(
        "ALTER TABLE IF EXISTS skills ALTER COLUMN share_config TYPE JSONB USING share_config::jsonb"
    ) < statements.index("UPDATE skills SET share_config = jsonb_build_object")
    assert "ALTER TABLE IF EXISTS agents ALTER COLUMN share_config DROP DEFAULT" in statements
    assert "ALTER TABLE IF EXISTS skills ALTER COLUMN share_config DROP DEFAULT" in statements

    # 迁移语句必须产出**新形状**：`access_level` 只允许留在归一器里做读时归一，
    # 不能由迁移再写出来——否则新写入路径会拒绝自己造的行。
    assert '"access_levels": ["global"]' in statements
    assert '"access_levels": ["user"]' in statements
    assert "jsonb_build_array('user')" in statements
    assert "jsonb_build_array(created_by)" in statements
    assert '"access_level"' not in statements

    # 幂等：三条 UPDATE 都跳过已是 version 2 的行，再跑一次不改动任何已合规行。
    assert statements.count("share_config->>'version' IS DISTINCT FROM '2'") == 3


@pytest.mark.asyncio
async def test_ensure_knowledge_schema_rebuilds_vectors_for_incomplete_legacy_chunks():
    async with _recording_manager() as (manager, connection):
        await manager.ensure_knowledge_schema()

    statements = "\n".join(connection.statements)

    assert (
        "UPDATE knowledge_chunks SET graph_structure_indexed = TRUE "
        "WHERE graph_indexed IS TRUE AND graph_structure_indexed IS NOT TRUE"
    ) in statements
    assert "mention.entity_id = entity.entity_id AND chunk.graph_indexed IS NOT TRUE" in statements
    assert "mention.triple_id = triple.triple_id AND chunk.graph_indexed IS NOT TRUE" in statements
    assert "THEN 'pending' ELSE 'indexed'" in statements


@pytest.mark.asyncio
async def test_tenant_schema_creates_table_and_identity_constraint():
    """tenants 表与 (issuer, external_tenant_id) 唯一约束必须由迁移产出。"""
    async with _recording_manager() as (manager, connection):
        await manager.ensure_tenant_schema()

    create_table = next(
        statement for statement in connection.statements if "CREATE TABLE IF NOT EXISTS tenants" in statement
    )
    for column in ("id", "external_tenant_id", "issuer", "name", "code", "default_department_id", "created_at"):
        assert column in create_table, column

    statements = "\n".join(connection.statements)
    assert "uq_tenants_issuer_external" in statements
    # 约束必须走存在性判断，否则重复迁移会炸。
    assert "pg_constraint" in statements


def test_business_schema_version_is_thirteen():
    """版本号是迁移门禁的唯一真相；语句改动后必须推版本，否则已升级的库拿不到新语句。"""
    from yuxi.storage.postgres import manager as manager_module

    assert manager_module.BUSINESS_SCHEMA_VERSION == 13


@pytest.mark.asyncio
async def test_rls_policies_cover_roots_and_derived_tables():
    """归属根按列比较，派生表按递归 EXISTS；两侧都同时定义 USING 与 WITH CHECK。"""
    async with _recording_manager() as (manager, connection):
        await manager.ensure_tenant_rls()

    statements = "\n".join(connection.statements)
    for table in TENANT_ROOT_TABLES:
        assert f"ALTER TABLE IF EXISTS {table} ENABLE ROW LEVEL SECURITY" in statements, table
        assert f"ALTER TABLE IF EXISTS {table} FORCE ROW LEVEL SECURITY" in statements, table
        assert f"CREATE POLICY tenant_isolation ON {table}" in statements, table

    assert "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::int)" in statements
    assert (
        "EXISTS (SELECT 1 FROM conversations AS parent_row WHERE parent_row.id = messages.conversation_id)"
        in statements
    )
    # 初稿的写法会把前缀拼两次，生成不存在的列。
    assert "parent_row.parent_row." not in statements


@pytest.mark.asyncio
async def test_tenant_schema_does_not_enable_rls():
    """全新库会走到 ensure_tenant_schema —— 它不得夹带尚未验证的 RLS 策略。"""
    async with _recording_manager() as (manager, connection):
        await manager.ensure_tenant_schema()

    statements = "\n".join(connection.statements)
    assert "ROW LEVEL SECURITY" not in statements
    assert "CREATE POLICY" not in statements


@pytest.mark.asyncio
async def test_tenant_rls_is_its_own_step_covering_both_root_and_derived_tables():
    """拆出的方法仍要覆盖全部受保护表——少一张就是一个直连读写缺口。"""
    from yuxi.storage.postgres.manager import DERIVED_TABLE_PARENTS

    async with _recording_manager() as (manager, connection):
        await manager.ensure_tenant_rls()

    statements = "\n".join(connection.statements)
    for table in set(TENANT_ROOT_TABLES) | set(DERIVED_TABLE_PARENTS):
        assert f"CREATE POLICY tenant_isolation ON {table}" in statements, table


def test_derived_table_parents_match_real_foreign_keys():
    """父表与连接列必须与真库 FK 一致。

    初稿把 tool_calls 的父写成 conversations（真实父是 messages）、把 knowledge_chunks 的
    父列写成 id（真实是 file_id）；写错的策略会创建失败或恒不匹配。
    """
    from yuxi.storage.postgres.manager import DERIVED_TABLE_PARENTS

    assert DERIVED_TABLE_PARENTS["tool_calls"] == ("messages", "message_id", "id")
    assert DERIVED_TABLE_PARENTS["knowledge_chunks"] == ("knowledge_files", "file_id", "file_id")
    assert DERIVED_TABLE_PARENTS["subagent_threads"] == ("conversations", "parent_conversation_id", "id")


def test_every_derived_parent_chain_terminates_in_an_ownership_root():
    """递归 EXISTS 的正确性依赖整条父链都受有效 RLS 保护。

    链上每一环要么是归属根（有直接策略），要么自己也在 DERIVED_TABLE_PARENTS 里（有 EXISTS
    策略）。任何一环缺失，递归就会漏——这才是"整条链受保护"，而不是"策略建过一遍"。
    """
    from yuxi.storage.postgres.manager import DERIVED_TABLE_PARENTS

    for table, (parent, _local_key, _parent_key) in DERIVED_TABLE_PARENTS.items():
        seen: set[str] = set()
        current = parent
        while current in DERIVED_TABLE_PARENTS:
            assert current not in seen, f"{table} 的父链成环：{current}"
            seen.add(current)
            current = DERIVED_TABLE_PARENTS[current][0]
        assert current in TENANT_ROOT_TABLES, f"{table} 的父链终止于 {current}，它不是归属根、没有策略"


def test_every_model_table_is_covered_by_a_tenant_policy_or_an_explicit_exception():
    """独立于策略映射的覆盖检查：从模型枚举全部表，逐张确认已受保护或是有理由的例外。

    只检查"已列出的表"抓不到漏表——图谱四表与评估四表就曾整片漏掉，而运行时账号对这些表
    有全表权限，没有策略就等于一个直接的读写缺口。
    """
    from yuxi.storage.postgres.manager import DERIVED_TABLE_PARENTS

    protected = set(TENANT_ROOT_TABLES) | set(DERIVED_TABLE_PARENTS)
    all_tables = set(BusinessBase.metadata.tables) | set(KnowledgeBase.metadata.tables)
    exceptions = {
        # tenants 是平台级实体，它本身就是租户的定义，不按租户隔离。
        "tenants",
        # 这三张是「平台默认 + 租户覆盖」表：策略形状不同（读 = 自己 + NULL，写 = 只能写自己），
        # 由「每租户系统配置」那个计划实现。**在它落地前，这三张表对运行时角色是完全开放的**
        # ——这是已知且被记名的缺口，不是遗漏。
        "model_providers",
        "mcp_servers",
        "config_options",
    }

    uncovered = sorted(all_tables - protected - exceptions)
    assert not uncovered, f"这些表既没有租户策略、也没有例外理由：{uncovered}"


def test_default_tenant_constants_are_stable():
    """默认租户常量是认证计划接入前的唯一租户来源，取值不能被随手改动。"""
    from yuxi.storage.postgres import manager as manager_module

    assert manager_module.DEFAULT_TENANT_ID == 1
    assert manager_module.DEFAULT_TENANT_EXTERNAL_ID == "default"
    assert manager_module.DEFAULT_TENANT_ISSUER == "local"


@pytest.mark.asyncio
async def test_every_ownership_root_gets_a_tenant_id_column():
    """归属根由迁移加可空 tenant_id；派生表靠外键链，不在此列。"""
    async with _recording_manager() as (manager, connection):
        await manager.ensure_tenant_schema()

    statements = "\n".join(connection.statements)
    for table in TENANT_ROOT_TABLES:
        assert f"ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS tenant_id INTEGER" in statements, table
        assert f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table}(tenant_id)" in statements, table


def test_ownership_roots_are_declared_on_the_orm_models():
    """ORM 侧与 DDL 侧必须同时有列，否则全新库（create_all）会缺列。"""
    for table in TENANT_ROOT_TABLES:
        metadata = KnowledgeBase.metadata if table == "knowledge_bases" else BusinessBase.metadata
        assert "tenant_id" in metadata.tables[table].c, table


@pytest.mark.asyncio
async def test_user_identity_columns_are_added_by_migration():
    """宿主身份三列由迁移补上，老库没有它们也要能补齐。"""
    async with _recording_manager() as (manager, connection):
        await manager.ensure_tenant_schema()

    statements = "\n".join(connection.statements)
    assert "ADD COLUMN IF NOT EXISTS external_issuer VARCHAR(255)" in statements
    assert "ADD COLUMN IF NOT EXISTS external_subject VARCHAR(255)" in statements
    assert "ADD COLUMN IF NOT EXISTS identity_source VARCHAR(32)" in statements
    assert "SET DEFAULT 'local'" in statements
    # users 在当前库里可能还不存在（知识域先行迁移、或该域未启用），回填必须自己判断。
    assert "to_regclass('users')" in statements


def test_user_model_keeps_uid_meaning_and_adds_external_identity():
    """uid 仍是内部身份：它参与 Workspace 路径派生与既有外键，不能改成宿主 subject。"""
    assert "uid" in User.__table__.c
    for column in ("external_issuer", "external_subject", "identity_source"):
        assert column in User.__table__.c, column
    assert User.__table__.c.identity_source.default.arg == "local"
