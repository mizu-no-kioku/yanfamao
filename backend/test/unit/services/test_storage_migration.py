from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from yuxi import storage_migration
from yuxi.storage_migrations.v071_workdirs import (
    V071ConversationBinding,
    V071WorkdirBinding,
    V071WorkdirMigrationPlan,
)


class _Session:
    def __init__(self, calls: list[object] | None = None):
        self.calls = calls

    async def execute(self, statement):
        if self.calls is not None:
            self.calls.append(("execute", str(statement)))

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_storage_migration_reads_legacy_schema_before_cutover(monkeypatch):
    calls: list[object] = []
    sessions = [_Session(), _Session(), _Session(calls), _Session()]

    @asynccontextmanager
    async def session_context():
        yield sessions.pop(0)

    manager = SimpleNamespace(
        initialize=lambda: calls.append("initialize"),
        schema_migration_lock=lambda: _async_context(calls, "schema_lock"),
        create_schema_version_table=lambda: _record(calls, "create_schema_version_table"),
        get_schema_versions=lambda: _async_value({}),
        upgrade_agent_resource_selection=lambda: _record(
            calls, f"version:business:{storage_migration.BUSINESS_SCHEMA_VERSION}"
        ),
        record_schema_version=lambda domain, version: _record(calls, f"version:{domain}:{version}"),
        create_business_tables=lambda: _record(calls, "create_business_tables"),
        create_knowledge_tables=lambda: _record(calls, "create_knowledge_tables"),
        ensure_business_schema=lambda: _record(calls, "ensure_business_schema"),
        ensure_tenant_schema=lambda: _record(calls, "tenant_schema"),
        ensure_tenant_rls=lambda: _record(calls, "tenant_rls"),
        # 预检 + 装约束 + 停写锁现在是**同一个方法、同一个事务**；"预检先于装约束"是它内部
        # 的契约（由集成用例 `test_the_install_holds_a_stop_write_lock...` 直接取证），
        # 不再由本文件的两步顺序断言覆盖。
        install_tenant_reference_consistency=lambda: _record(calls, "tenant_reference_install"),
        ensure_tenant_root_immutability=lambda: _record(calls, "tenant_root_guard"),
        ensure_tenant_discovery_boundary=lambda: _record(calls, "tenant_discovery_boundary"),
        ensure_knowledge_schema=lambda: _record(calls, "ensure_knowledge_schema"),
        setup_langgraph_checkpointer=lambda: _record(calls, "setup_langgraph_checkpointer"),
        get_async_session_context=session_context,
        get_owner_session_context=session_context,
        close=lambda: _record(calls, "close"),
    )
    workdirs = (V071WorkdirBinding("workdir-1", "user-1"),)
    conversations = (V071ConversationBinding("thread-1", "user-1", "workdir-1"),)

    async def read_bindings(_db):
        calls.append("read_v071_workdir_plan")
        return V071WorkdirMigrationPlan(True, workdirs, conversations)

    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setattr(storage_migration, "read_v071_workdir_plan", read_bindings)
    monkeypatch.setattr(storage_migration, "_require_quiescence_proof", lambda: calls.append("proof"))
    monkeypatch.setattr(
        storage_migration,
        "_converge_database_state",
        lambda *, fail_nonterminal_runs: _record(calls, f"converge:{fail_nonterminal_runs}"),
    )
    monkeypatch.setattr(
        storage_migration,
        "import_v071_workdirs",
        lambda actual_workdirs, actual_conversations: calls.append(("import", actual_workdirs, actual_conversations)),
    )
    monkeypatch.setattr(storage_migration, "rewrite_v071_workdir_paths", lambda _db: _record(calls, "rewrite"))
    monkeypatch.setattr(storage_migration, "verify_workdir_bindings", lambda _db: _record(calls, "verify"))
    monkeypatch.setattr(
        storage_migration,
        "cleanup_v071_thread_sources",
        lambda actual_conversations: calls.append(("cleanup", actual_conversations)),
    )
    monkeypatch.setattr(storage_migration, "migrate_shared_skills", lambda _db: _record(calls, "skills"))
    monkeypatch.setattr(storage_migration, "mark_v071_skills_migrated", lambda: calls.append("mark_skills"))
    monkeypatch.setattr(
        storage_migration,
        "migrate_runtime_storage_identity",
        lambda: calls.append("runtime_identity"),
    )
    monkeypatch.setattr(storage_migration, "_legacy_skill_roots_exist", lambda: False)
    monkeypatch.setattr(storage_migration, "_legacy_system_config_exists", lambda: False)
    monkeypatch.setattr(storage_migration, "runtime_storage_requires_quiescence", lambda: True)

    await storage_migration.main()

    assert calls.index("read_v071_workdir_plan") < calls.index("ensure_business_schema")
    assert calls.index("proof") < calls.index(("import", workdirs, conversations))
    assert calls.index(("import", workdirs, conversations)) < calls.index("ensure_business_schema")
    assert calls.index("verify") < calls.index(("cleanup", conversations))
    assert calls.index("mark_skills") < calls.index("runtime_identity")
    assert calls[-1] == "close"


@pytest.mark.asyncio
async def test_storage_migration_rejects_v071_schema_without_quiescence_proof(monkeypatch, tmp_path):
    calls: list[str] = []

    @asynccontextmanager
    async def session_context():
        yield _Session()

    manager = SimpleNamespace(
        initialize=lambda: None,
        schema_migration_lock=lambda: _async_context(calls, "schema_lock"),
        create_schema_version_table=lambda: _record(calls, "create_schema_version_table"),
        get_schema_versions=lambda: _async_value({}),
        upgrade_agent_resource_selection=lambda: _record(
            calls, f"version:business:{storage_migration.BUSINESS_SCHEMA_VERSION}"
        ),
        record_schema_version=lambda domain, version: _record(calls, f"version:{domain}:{version}"),
        create_business_tables=lambda: _record(calls, "create"),
        create_knowledge_tables=lambda: _record(calls, "create_knowledge"),
        ensure_business_schema=lambda: _record(calls, "schema"),
        ensure_tenant_schema=lambda: _record(calls, "tenant_schema"),
        ensure_tenant_rls=lambda: _record(calls, "tenant_rls"),
        # 预检 + 装约束 + 停写锁现在是**同一个方法、同一个事务**；"预检先于装约束"是它内部
        # 的契约（由集成用例 `test_the_install_holds_a_stop_write_lock...` 直接取证），
        # 不再由本文件的两步顺序断言覆盖。
        install_tenant_reference_consistency=lambda: _record(calls, "tenant_reference_install"),
        ensure_tenant_root_immutability=lambda: _record(calls, "tenant_root_guard"),
        ensure_tenant_discovery_boundary=lambda: _record(calls, "tenant_discovery_boundary"),
        ensure_knowledge_schema=lambda: _record(calls, "knowledge_schema"),
        setup_langgraph_checkpointer=lambda: _record(calls, "checkpoint"),
        get_async_session_context=session_context,
        get_owner_session_context=session_context,
        close=lambda: _record(calls, "close"),
    )
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setattr(
        storage_migration,
        "read_v071_workdir_plan",
        lambda _db: _async_value(V071WorkdirMigrationPlan(True, (), ())),
    )
    monkeypatch.setattr(storage_migration, "_legacy_skill_roots_exist", lambda: False)
    monkeypatch.setattr(storage_migration, "_legacy_system_config_exists", lambda: False)
    monkeypatch.setattr(storage_migration, "runtime_storage_requires_quiescence", lambda: False)
    monkeypatch.setenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_FILE", str(tmp_path / "missing"))
    monkeypatch.delenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="migrate-storage.sh"):
        await storage_migration.main()

    assert calls == ["schema_lock", "close"]


@pytest.mark.asyncio
async def test_current_schema_skips_schema_ddl(monkeypatch):
    calls: list[str] = []
    sessions = [_Session(), _Session(), _Session()]

    @asynccontextmanager
    async def session_context():
        yield sessions.pop(0)

    manager = SimpleNamespace(
        initialize=lambda: calls.append("initialize"),
        schema_migration_lock=lambda: _async_context(calls, "schema_lock"),
        create_schema_version_table=lambda: _record(calls, "create_schema_version_table"),
        get_schema_versions=lambda: _async_value(
            {
                "business": storage_migration.BUSINESS_SCHEMA_VERSION,
                "knowledge": storage_migration.KNOWLEDGE_SCHEMA_VERSION,
            }
        ),
        upgrade_agent_resource_selection=lambda: _record(
            calls, f"version:business:{storage_migration.BUSINESS_SCHEMA_VERSION}"
        ),
        record_schema_version=lambda domain, version: _record(calls, f"version:{domain}:{version}"),
        create_business_tables=lambda: _record(calls, "create_business"),
        create_knowledge_tables=lambda: _record(calls, "create_knowledge"),
        ensure_business_schema=lambda: _record(calls, "business_schema"),
        ensure_tenant_schema=lambda: _record(calls, "tenant_schema"),
        ensure_tenant_rls=lambda: _record(calls, "tenant_rls"),
        # 预检 + 装约束 + 停写锁现在是**同一个方法、同一个事务**；"预检先于装约束"是它内部
        # 的契约（由集成用例 `test_the_install_holds_a_stop_write_lock...` 直接取证），
        # 不再由本文件的两步顺序断言覆盖。
        install_tenant_reference_consistency=lambda: _record(calls, "tenant_reference_install"),
        ensure_tenant_root_immutability=lambda: _record(calls, "tenant_root_guard"),
        ensure_tenant_discovery_boundary=lambda: _record(calls, "tenant_discovery_boundary"),
        ensure_knowledge_schema=lambda: _record(calls, "knowledge_schema"),
        setup_langgraph_checkpointer=lambda: _record(calls, "checkpoint"),
        get_async_session_context=session_context,
        get_owner_session_context=session_context,
        close=lambda: _record(calls, "close"),
    )
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setattr(
        storage_migration,
        "read_v071_workdir_plan",
        lambda _db: _async_value(V071WorkdirMigrationPlan(False, (), ())),
    )
    monkeypatch.setattr(storage_migration, "_legacy_skill_roots_exist", lambda: False)
    monkeypatch.setattr(storage_migration, "_legacy_system_config_exists", lambda: False)
    monkeypatch.setattr(storage_migration, "runtime_storage_requires_quiescence", lambda: False)
    monkeypatch.setattr(
        storage_migration,
        "_converge_database_state",
        lambda *, fail_nonterminal_runs: _record(calls, f"converge:{fail_nonterminal_runs}"),
    )
    monkeypatch.setattr(storage_migration, "migrate_shared_skills", lambda _db: _record(calls, "skills"))
    monkeypatch.setattr(storage_migration, "mark_v071_skills_migrated", lambda: calls.append("mark_skills"))
    monkeypatch.setattr(storage_migration, "migrate_runtime_storage_identity", lambda: calls.append("runtime_identity"))

    await storage_migration.main()

    assert {
        "create_business",
        "create_knowledge",
        "business_schema",
        "knowledge_schema",
        "checkpoint",
        f"version:business:{storage_migration.BUSINESS_SCHEMA_VERSION}",
        f"version:knowledge:{storage_migration.KNOWLEDGE_SCHEMA_VERSION}",
    }.isdisjoint(calls)
    assert "converge:False" in calls


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported_version", [1, 3, 4, 5, 6, storage_migration.BUSINESS_SCHEMA_VERSION + 1])
async def test_main_rejects_unsupported_business_schema_before_ddl(monkeypatch, unsupported_version: int):
    calls: list[str] = []

    @asynccontextmanager
    async def session_context():
        yield _Session()

    manager = SimpleNamespace(
        initialize=lambda: calls.append("initialize"),
        schema_migration_lock=lambda: _async_context(calls, "schema_lock"),
        create_schema_version_table=lambda: _record(calls, "create_schema_version_table"),
        get_schema_versions=lambda: _async_value(
            {"business": unsupported_version, "knowledge": storage_migration.KNOWLEDGE_SCHEMA_VERSION}
        ),
        get_async_session_context=session_context,
        get_owner_session_context=session_context,
        close=lambda: _record(calls, "close"),
    )
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setattr(
        storage_migration,
        "read_v071_workdir_plan",
        lambda _db: _async_value(V071WorkdirMigrationPlan(False, (), ())),
    )
    monkeypatch.setattr(storage_migration, "_legacy_skill_roots_exist", lambda: False)
    monkeypatch.setattr(storage_migration, "_legacy_system_config_exists", lambda: False)
    monkeypatch.setattr(storage_migration, "runtime_storage_requires_quiescence", lambda: False)

    with pytest.raises(RuntimeError, match=f"Unsupported business schema version: {unsupported_version}"):
        await storage_migration.main()

    assert calls == ["initialize", "schema_lock", "create_schema_version_table", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("business_version", [2, 7, 8])
async def test_supported_legacy_business_schema_is_converged_and_versioned_as_current(monkeypatch, business_version):
    calls: list[str] = []
    sessions = [_Session(), _Session(), _Session()]

    @asynccontextmanager
    async def session_context():
        yield sessions.pop(0)

    manager = SimpleNamespace(
        initialize=lambda: calls.append("initialize"),
        schema_migration_lock=lambda: _async_context(calls, "schema_lock"),
        create_schema_version_table=lambda: _record(calls, "create_schema_version_table"),
        get_schema_versions=lambda: _async_value(
            {"business": business_version, "knowledge": storage_migration.KNOWLEDGE_SCHEMA_VERSION}
        ),
        # 用**独立**标记：这条桩若也叫 `version:business:...`，`calls.index(...)` 会先命中它，
        # 于是"版本记录在全部数据库步骤之后"就变成一句测不出来的话。
        upgrade_agent_resource_selection=lambda: _record(calls, "agent_resource_selection"),
        record_schema_version=lambda domain, version: _record(calls, f"version:{domain}:{version}"),
        create_business_tables=lambda: _record(calls, "create_business"),
        create_knowledge_tables=lambda: _record(calls, "create_knowledge"),
        ensure_business_schema=lambda: _record(calls, "business_schema"),
        ensure_tenant_schema=lambda: _record(calls, "tenant_schema"),
        ensure_tenant_rls=lambda: _record(calls, "tenant_rls"),
        # 预检 + 装约束 + 停写锁现在是**同一个方法、同一个事务**；"预检先于装约束"是它内部
        # 的契约（由集成用例 `test_the_install_holds_a_stop_write_lock...` 直接取证），
        # 不再由本文件的两步顺序断言覆盖。
        install_tenant_reference_consistency=lambda: _record(calls, "tenant_reference_install"),
        ensure_tenant_root_immutability=lambda: _record(calls, "tenant_root_guard"),
        ensure_tenant_discovery_boundary=lambda: _record(calls, "tenant_discovery_boundary"),
        ensure_knowledge_schema=lambda: _record(calls, "knowledge_schema"),
        setup_langgraph_checkpointer=lambda: _record(calls, "checkpoint"),
        get_async_session_context=session_context,
        get_owner_session_context=session_context,
        close=lambda: _record(calls, "close"),
    )
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setattr(
        storage_migration,
        "read_v071_workdir_plan",
        lambda _db: _async_value(V071WorkdirMigrationPlan(False, (), ())),
    )
    monkeypatch.setattr(storage_migration, "_legacy_skill_roots_exist", lambda: False)
    monkeypatch.setattr(storage_migration, "_legacy_system_config_exists", lambda: False)
    monkeypatch.setattr(storage_migration, "runtime_storage_requires_quiescence", lambda: False)
    monkeypatch.setattr(
        storage_migration,
        "_converge_database_state",
        lambda *, fail_nonterminal_runs: _record(calls, f"converge:{fail_nonterminal_runs}"),
    )
    monkeypatch.setattr(storage_migration, "migrate_shared_skills", lambda _db: _record(calls, "skills"))
    monkeypatch.setattr(storage_migration, "mark_v071_skills_migrated", lambda: calls.append("mark_skills"))
    monkeypatch.setattr(storage_migration, "migrate_runtime_storage_identity", lambda: calls.append("runtime_identity"))

    await storage_migration.main()

    assert ("business_schema" in calls) is (business_version in {2, 7})
    assert f"version:business:{storage_migration.BUSINESS_SCHEMA_VERSION}" in calls
    assert "tenant_schema" in calls
    assert {"create_business", "checkpoint", "knowledge_schema"}.isdisjoint(calls)
    # 顺序契约（版本 14）：RLS **先于** 预检 + 装约束（同一事务）；归属根护栏在它之后；
    # 发现边界 **最后**；版本记录 **后于** 全部数据库步骤。
    # "预检先于装约束"现在**内部**于 `install_tenant_reference_consistency()`（同事务 + 停写锁），
    # 由集成用例直接取证，不在本文件的调用序列里。
    assert calls.index("tenant_rls") < calls.index("tenant_reference_install")
    assert calls.index("tenant_reference_install") < calls.index("tenant_root_guard")
    assert calls.index("tenant_root_guard") < calls.index("tenant_discovery_boundary")
    assert calls.index("tenant_discovery_boundary") < calls.index("converge:False")
    assert calls.index("converge:False") < calls.index(
        f"version:business:{storage_migration.BUSINESS_SCHEMA_VERSION}"
    )


@pytest.mark.asyncio
async def test_failed_business_migration_does_not_record_version(monkeypatch):
    calls: list[str] = []

    @asynccontextmanager
    async def session_context():
        yield _Session()

    async def fail_checkpoint_setup():
        calls.append("checkpoint")
        raise RuntimeError("broken checkpoint migration")

    manager = SimpleNamespace(
        initialize=lambda: calls.append("initialize"),
        schema_migration_lock=lambda: _async_context(calls, "schema_lock"),
        create_schema_version_table=lambda: _record(calls, "create_schema_version_table"),
        get_schema_versions=lambda: _async_value({}),
        upgrade_agent_resource_selection=lambda: _record(
            calls, f"version:business:{storage_migration.BUSINESS_SCHEMA_VERSION}"
        ),
        record_schema_version=lambda domain, version: _record(calls, f"version:{domain}:{version}"),
        create_business_tables=lambda: _record(calls, "create_business"),
        create_knowledge_tables=lambda: _record(calls, "create_knowledge"),
        ensure_business_schema=lambda: _record(calls, "business_schema"),
        ensure_tenant_schema=lambda: _record(calls, "tenant_schema"),
        ensure_tenant_rls=lambda: _record(calls, "tenant_rls"),
        # 预检 + 装约束 + 停写锁现在是**同一个方法、同一个事务**；"预检先于装约束"是它内部
        # 的契约（由集成用例 `test_the_install_holds_a_stop_write_lock...` 直接取证），
        # 不再由本文件的两步顺序断言覆盖。
        install_tenant_reference_consistency=lambda: _record(calls, "tenant_reference_install"),
        ensure_tenant_root_immutability=lambda: _record(calls, "tenant_root_guard"),
        ensure_tenant_discovery_boundary=lambda: _record(calls, "tenant_discovery_boundary"),
        ensure_knowledge_schema=lambda: _record(calls, "knowledge_schema"),
        setup_langgraph_checkpointer=fail_checkpoint_setup,
        get_async_session_context=session_context,
        get_owner_session_context=session_context,
        close=lambda: _record(calls, "close"),
    )
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setattr(
        storage_migration,
        "read_v071_workdir_plan",
        lambda _db: _async_value(V071WorkdirMigrationPlan(False, (), ())),
    )
    monkeypatch.setattr(storage_migration, "_legacy_skill_roots_exist", lambda: False)
    monkeypatch.setattr(storage_migration, "_legacy_system_config_exists", lambda: False)
    monkeypatch.setattr(storage_migration, "runtime_storage_requires_quiescence", lambda: False)

    with pytest.raises(RuntimeError, match="broken checkpoint migration"):
        await storage_migration.main()

    assert "business_schema" in calls
    assert f"version:business:{storage_migration.BUSINESS_SCHEMA_VERSION}" not in calls
    assert "create_knowledge" not in calls
    assert calls[-1] == "close"


@pytest.mark.asyncio
async def test_current_schema_does_not_rewrite_workdir_data(monkeypatch):
    calls: list[str] = []
    sessions = [_Session(), _Session(), _Session()]

    @asynccontextmanager
    async def session_context():
        yield sessions.pop(0)

    manager = SimpleNamespace(
        initialize=lambda: calls.append("initialize"),
        schema_migration_lock=lambda: _async_context(calls, "schema_lock"),
        create_schema_version_table=lambda: _record(calls, "create_schema_version_table"),
        get_schema_versions=lambda: _async_value({}),
        upgrade_agent_resource_selection=lambda: _record(
            calls, f"version:business:{storage_migration.BUSINESS_SCHEMA_VERSION}"
        ),
        record_schema_version=lambda domain, version: _record(calls, f"version:{domain}:{version}"),
        create_business_tables=lambda: _record(calls, "create"),
        create_knowledge_tables=lambda: _record(calls, "create_knowledge"),
        ensure_business_schema=lambda: _record(calls, "schema"),
        ensure_tenant_schema=lambda: _record(calls, "tenant_schema"),
        ensure_tenant_rls=lambda: _record(calls, "tenant_rls"),
        # 预检 + 装约束 + 停写锁现在是**同一个方法、同一个事务**；"预检先于装约束"是它内部
        # 的契约（由集成用例 `test_the_install_holds_a_stop_write_lock...` 直接取证），
        # 不再由本文件的两步顺序断言覆盖。
        install_tenant_reference_consistency=lambda: _record(calls, "tenant_reference_install"),
        ensure_tenant_root_immutability=lambda: _record(calls, "tenant_root_guard"),
        ensure_tenant_discovery_boundary=lambda: _record(calls, "tenant_discovery_boundary"),
        ensure_knowledge_schema=lambda: _record(calls, "knowledge_schema"),
        setup_langgraph_checkpointer=lambda: _record(calls, "checkpoint"),
        get_async_session_context=session_context,
        get_owner_session_context=session_context,
        close=lambda: _record(calls, "close"),
    )
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setattr(
        storage_migration,
        "read_v071_workdir_plan",
        lambda _db: _async_value(V071WorkdirMigrationPlan(False, (), ())),
    )
    monkeypatch.setattr(storage_migration, "_legacy_skill_roots_exist", lambda: False)
    monkeypatch.setattr(storage_migration, "_legacy_system_config_exists", lambda: False)
    monkeypatch.setattr(storage_migration, "runtime_storage_requires_quiescence", lambda: False)
    monkeypatch.setattr(
        storage_migration,
        "_converge_database_state",
        lambda *, fail_nonterminal_runs: _record(calls, f"converge:{fail_nonterminal_runs}"),
    )
    monkeypatch.setattr(storage_migration, "import_v071_workdirs", lambda *_args: calls.append("import"))
    monkeypatch.setattr(storage_migration, "rewrite_v071_workdir_paths", lambda _db: _record(calls, "rewrite"))
    monkeypatch.setattr(storage_migration, "verify_workdir_bindings", lambda _db: _record(calls, "verify"))
    monkeypatch.setattr(storage_migration, "cleanup_v071_thread_sources", lambda *_args: calls.append("cleanup"))
    monkeypatch.setattr(storage_migration, "migrate_shared_skills", lambda _db: _record(calls, "skills"))
    monkeypatch.setattr(storage_migration, "mark_v071_skills_migrated", lambda: calls.append("mark_skills"))
    monkeypatch.setattr(
        storage_migration,
        "migrate_runtime_storage_identity",
        lambda: calls.append("runtime_identity"),
    )

    await storage_migration.main()

    assert "converge:False" in calls
    assert "schema" in calls
    assert "skills" in calls
    assert "runtime_identity" in calls
    assert {"import", "rewrite", "verify", "cleanup"}.isdisjoint(calls)


def test_personal_workspace_skills_never_trigger_shared_skill_migration(monkeypatch, tmp_path):
    personal_skill = tmp_path / "user-data/shared/user-1/workspace/agents/skills/notes"
    personal_skill.mkdir(parents=True)
    monkeypatch.setattr(storage_migration, "get_legacy_storage_dir", lambda: tmp_path / "legacy")
    monkeypatch.setattr(storage_migration, "v071_skill_migration_completed", lambda: False)

    assert storage_migration._legacy_skill_roots_exist() is False


@asynccontextmanager
async def _async_context(calls: list[object], value: str):
    calls.append(value)
    yield


async def _record(calls: list[object], value: str) -> None:
    calls.append(value)


async def _async_value(value):
    return value
