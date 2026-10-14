"""版本 14 完成门禁里的两条：**中途失败不记录版本**、**重放幂等**。

## 为什么单跑一遍成功不算验收

`test_empty_database_migration.py` 证明"空库能跑到 `business=14`"。它**没有**证明两件事：

1. **版本记录后于全部数据库步骤**：某一步失败时，库里不能已经写着 `business=14`——否则重跑会
   因为"版本已是 14"而**跳过**剩下的步骤，把一个半成品库当成已完成。这条用**真实迁移入口 +
   注入失败**取证（不是把 `record_schema_version` 换成桩——那只能证明"桩被调过"）。
2. **重放幂等**：对一个已经是 14 的库重跑迁移，必须退出 0、版本不变、RLS 不被关掉、也不把
   已装好的护栏改回去。

## 中途失败用**独立数据库**，不动会话级的 `yuxi_test`

会话夹具 `migrated_test_database` 已经把 `yuxi_test` 迁到 14；中途失败要一个**全新**的库。
所以这里在同一个测试实例上另建一个一次性数据库——不重建容器、不影响其它用例。

**这个库不会被自动删除**：脚手架只在收尾时把库名与清理语句打出来，交由人工处理。
理由与"保留失败现场"同源——自动 `DROP ... WITH (FORCE)` 会把失败现场连同库一起销毁。

注入失败的方式：把真实入口 `yuxi.storage_migration.main()` 里的 `ensure_tenant_rls` 换成一个
抛错的函数（`python -c` 起子进程）。失败点落在"建表之后、RLS 之前"，正是"半成品"的形态。
"""

from __future__ import annotations

import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import _migration_round as round_module
from _migration_round import EXPECTED_VERSIONS, MigrationRound
from _tenant_schema_test_db import assert_target_is_the_test_instance, tenant_schema_url

pytestmark = [pytest.mark.integration]


@pytest.fixture(name="migration_round")
def migration_round_fixture(migrated_test_database: MigrationRound) -> MigrationRound:
    """会话级的迁移轮次（空库那轮）；这里只是让断言读起来是"本轮这一轮"。"""

    return migrated_test_database


def _url_for_database(name: str) -> str:
    """把测试实例的连接串改到另一个库上（host/port/凭证不变）。"""

    return make_url(tenant_schema_url()).set(database=name).render_as_string(hide_password=False)


async def _create_database(name: str) -> None:
    """在测试实例上建一个空库。`CREATE DATABASE` 不能在事务块里，所以用 AUTOCOMMIT。"""

    await assert_target_is_the_test_instance()
    engine = create_async_engine(tenant_schema_url(), isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


def _report_database_for_manual_cleanup(name: str) -> None:
    """报出本轮**创建**的测试库并给出清理语句——**不自动删**。

    这里**不**执行 `DROP DATABASE ... WITH (FORCE)`：它会在用例失败时把现场连同库一起销毁，
    而 `WITH (FORCE)` 还会强断挂在库上的其他会话。破坏性存储操作一律交人工执行，
    脚手架只负责给出**精确目标**。
    """

    print(f"[part2] 本轮创建的测试库保留（**未**自动删除）：{name}")
    print(f'[part2] 人工清理语句：DROP DATABASE IF EXISTS "{name}";')


def _failing_migration_command(step: str) -> list[str]:
    """真实入口 + 把 `ensure_tenant_rls` 换成抛错的桩——失败点在建表之后、RLS 之前。"""

    script = (
        "import asyncio\n"
        "import yuxi.storage_migration as m\n"
        "from yuxi.storage.postgres.manager import PostgresManager\n"
        "async def _boom(self):\n"
        "    raise RuntimeError('injected mid-migration failure (probe)')\n"
        f"PostgresManager.{step} = _boom\n"
        "asyncio.run(m.main())\n"
    )
    return [sys.executable, "-c", script]


def _failing_skill_migration_command() -> list[str]:
    """真实入口 + 把 `migrate_shared_skills` 换成抛错的桩。

    它**不是** `PostgresManager` 的方法，所以补的是 `yuxi.storage_migration` 模块的全局名——
    `main()` 正是按模块全局查这个名字的。失败点落在"租户步骤全部做完之后、版本记录之前"，
    这是版本记录时机最容易出错的位置。
    """

    script = (
        "import asyncio\n"
        "import yuxi.storage_migration as m\n"
        "async def _boom(session):\n"
        "    raise RuntimeError('injected skill migration failure (probe)')\n"
        "m.migrate_shared_skills = _boom\n"
        "asyncio.run(m.main())\n"
    )
    return [sys.executable, "-c", script]


async def test_a_failed_skill_migration_does_not_record_version_and_a_rerun_completes() -> None:
    """**会写库的 Skill 迁移**失败时不得记录 `business=14`；对同一个库重跑能完成。

    `migrate_shared_skills()` 会改 `Skill.dir_path` 并 `commit()`，所以它属于"数据库步骤"。
    把它排在版本记录**之后**，会让"Skill 改写失败"被 `business=14` 掩盖：重跑因为版本已是 14
    而跳过全部租户步骤。这条用例钉的就是那个顺序——失败那轮 RLS 已经装好（33 张表），
    但 `business` **仍不能是 14**。
    """

    database = f"yuxi_v14_skillfail_{uuid.uuid4().hex[:12]}"
    await _create_database(database)
    url = _url_for_database(database)
    round_ = round_module.start_round()
    round_.env["POSTGRES_URL"] = url
    completed = False
    try:
        # 第一轮：Skill 迁移注入失败 → 非零退出，且**不**记录 business=14。
        round_module.run_full_migration(round_, command=_failing_skill_migration_command())
        assert round_.returncode != 0, (
            f"注入的失败没有让迁移非零退出（returncode={round_.returncode}）\n{round_.log_tail()}"
        )
        # 失败点在最末端：租户步骤已全部提交，RLS 确实装好了——正因如此，"版本没记"才是
        # 唯一能阻止重跑跳过的信号，必须实测。
        assert await round_module.rls_enabled_table_count(url) == 33, (
            "Skill 迁移失败前，租户步骤应已全部提交（33 张表带 RLS）"
        )
        failed_versions = await round_module.schema_versions(url)
        assert failed_versions.get("business") != 14, (
            f"会写库的 Skill 迁移失败却记录了 business=14，重跑会被跳过、半成品库被当成已完成："
            f"{failed_versions}"
        )

        # 第二轮：同一条真实入口重跑（这次不注入失败）→ 完成。
        round_module.run_full_migration(round_)
        assert round_.returncode == 0, f"重跑没有成功：\n{round_.log_tail()}"
        assert await round_module.schema_versions(url) == EXPECTED_VERSIONS
        assert await round_module.rls_enabled_table_count(url) == 33
        completed = True
    finally:
        # 只有**整套断言都通过**才清理产物；异常 / 断言失败 / 超时一律保留现场。
        # `must_keep_artifacts()` 只看迁移子进程的退出码，看不到"迁移成功但断言失败"，
        # 所以必须在这里补上 `completed`。
        round_module.finish_round(
            round_,
            keep_artifacts=not completed or round_module.must_keep_artifacts(round_),
        )
        _report_database_for_manual_cleanup(database)


@pytest.mark.parametrize("step", ["ensure_tenant_rls", "ensure_tenant_discovery_boundary"])
async def test_a_failed_step_does_not_record_version_and_a_rerun_completes(step) -> None:
    """中途失败**不记录** `business=14`；对同一个库重跑能续上并完成。

    这是"版本记录后于全部数据库步骤"的直接证据：失败那轮结束后库里没有 `business` 行；重跑
    走的是同一条真实入口，最终 `business=14 / knowledge=2`。
    """

    database = f"yuxi_v14_midfail_{uuid.uuid4().hex[:12]}"
    await _create_database(database)
    url = _url_for_database(database)
    round_ = round_module.start_round()
    round_.env["POSTGRES_URL"] = url
    completed = False
    try:
        # 第一轮：注入失败 → 非零退出，且**不**记录 business=14。
        round_module.run_full_migration(round_, command=_failing_migration_command(step))
        assert round_.returncode != 0, (
            f"注入的失败没有让迁移非零退出（returncode={round_.returncode}）\n{round_.log_tail()}"
        )
        # **准确口径**：全新库在租户步骤**之前**会先落一条**遗留基线**（`business=9`，由
        # `upgrade_agent_resource_selection()` 记录，是既有行为）。这里要证明的不是"一条都没记"，
        # 而是**目标版本 14 没被记**——14 正是"重跑时跳过全部租户步骤"的那道闸。
        failed_versions = await round_module.schema_versions(url)
        assert failed_versions.get("business") != 14, (
            f"中途失败却记录了 business=14，重跑会被跳过、半成品库被当成已完成：{failed_versions}"
        )
        enabled = await round_module.rls_enabled_table_count(url)
        if step == "ensure_tenant_rls":
            assert enabled == 0
        else:
            assert enabled == 33, "后段失败前应已提交RLS，必须验证真实部分迁移重放"

        # 第二轮：同一条真实入口重跑 → 完成。
        round_module.run_full_migration(round_)
        assert round_.returncode == 0, f"重跑没有成功：\n{round_.log_tail()}"
        assert await round_module.schema_versions(url) == EXPECTED_VERSIONS
        assert await round_module.rls_enabled_table_count(url) == 33
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                assert not await connection.scalar(text(
                    "SELECT has_any_column_privilege('yuxi_app', 'tenants', 'SELECT')"
                ))
                assert await connection.scalar(text(
                    "SELECT has_column_privilege('yuxi_worker_discovery', 'tenants', 'id', 'SELECT')"
                ))
                assert not await connection.scalar(text(
                    "SELECT has_column_privilege('yuxi_worker_discovery', 'tenants', 'name', 'SELECT')"
                ))
        finally:
            await engine.dispose()
        completed = True
    finally:
        # 同前：只有**整套断言都通过**才清理产物，否则保留现场（日志 + 测试库）。
        round_module.finish_round(
            round_,
            keep_artifacts=not completed or round_module.must_keep_artifacts(round_),
        )
        _report_database_for_manual_cleanup(database)


async def test_rerunning_the_migration_on_a_current_database_is_idempotent(
    migration_round: MigrationRound,
) -> None:
    """对已经是 14 的库重跑迁移：退出 0、版本不变、RLS 不被关掉（重放幂等）。

    这一轮用的是**会话级那轮已经迁好的库**（`yuxi_test`），所以走的正是"重放"路径：数据库步骤
    因为版本已是 14 被跳过，只跑文件根收敛。断言的是**结果**——版本仍是 14、RLS 仍启用。
    """

    replay = round_module.start_round()
    completed = False
    try:
        round_module.run_full_migration(replay)
        assert replay.returncode == 0, f"对已是 14 的库重跑迁移失败了：\n{replay.log_tail()}"
        assert await round_module.schema_versions() == EXPECTED_VERSIONS
        assert await round_module.rls_enabled_table_count() == 33
        engine = create_async_engine(tenant_schema_url())
        try:
            async with engine.connect() as connection:
                assert not await connection.scalar(text(
                    "SELECT has_any_column_privilege('yuxi_app', 'tenants', 'SELECT')"
                ))
                assert await connection.scalar(text(
                    "SELECT has_column_privilege('yuxi_worker_discovery', 'tenants', 'id', 'SELECT')"
                ))
        finally:
            await engine.dispose()
        completed = True
    finally:
        # 同前：只有**整套断言都通过**才清理产物，否则保留现场。
        # 本用例不建库（走会话级的 `yuxi_test`），所以没有需要人工清理的库。
        round_module.finish_round(
            replay,
            keep_artifacts=not completed or round_module.must_keep_artifacts(replay),
        )
