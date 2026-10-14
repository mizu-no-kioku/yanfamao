"""在 API、worker 与 Sandbox 启动前完成一次性存储迁移。"""

from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path

from sqlalchemy import text

from yuxi.config import get_legacy_storage_dir
from yuxi.config.options import ensure_options_in_db
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.storage.postgres.manager import (
    BUSINESS_SCHEMA_VERSION,
    KNOWLEDGE_SCHEMA_VERSION,
    V071_WORKDIR_CUTOVER_STATEMENTS,
    pg_manager,
)
from yuxi.storage_migrations.v071_options import migrate_system_options
from yuxi.storage_migrations.v071_skills import (
    mark_migrated as mark_v071_skills_migrated,
)
from yuxi.storage_migrations.v071_skills import (
    migrate_shared_skills,
)
from yuxi.storage_migrations.v071_skills import (
    migration_completed as v071_skill_migration_completed,
)
from yuxi.storage_migrations.v071_workdirs import (
    cleanup_v071_thread_sources,
    import_v071_workdirs,
    read_v071_workdir_plan,
    rewrite_v071_workdir_paths,
    verify_workdir_bindings,
)
from yuxi.storage_migrations.v072_runtime_identity import (
    migrate_runtime_storage_identity,
    runtime_storage_requires_quiescence,
)

_QUIESCENCE_TOKEN_ENV = "YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN"
_QUIESCENCE_FILE_ENV = "YUXI_STORAGE_MIGRATION_QUIESCENCE_FILE"


def _legacy_skill_roots_exist() -> bool:
    """判断是否仍有会被停机迁移删除的历史共享 Skill 目录。"""
    if v071_skill_migration_completed():
        return False
    shared_skills = get_legacy_storage_dir() / "skills"
    if shared_skills.is_symlink() or (shared_skills.is_dir() and any(shared_skills.iterdir())):
        return True
    return False


def _legacy_system_config_exists() -> bool:
    """判断旧广域目录是否仍保存系统配置。"""
    return (get_legacy_storage_dir() / "config/base.toml").is_file()


def _require_quiescence_proof() -> None:
    """校验由宿主停机脚本创建的一次性证明，禁止普通 up 执行破坏性迁移。"""
    expected = os.getenv(_QUIESCENCE_TOKEN_ENV, "").strip()
    proof_file = Path(
        os.getenv(
            _QUIESCENCE_FILE_ENV,
            str(get_legacy_storage_dir() / ".storage-migration-quiesced"),
        )
    )
    try:
        actual = proof_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("检测到旧文件数据；请先运行 scripts/migrate-storage.sh 停止旧运行环境") from exc
    if not expected or not actual or not hmac.compare_digest(actual, expected):
        raise RuntimeError("存储迁移停机证明无效；请重新运行 scripts/migrate-storage.sh")


async def _converge_database_state(*, fail_nonterminal_runs: bool) -> None:
    """导入历史系统配置；仅在已停机的旧布局切换中收敛非终态 Run。"""
    async with pg_manager.get_owner_session_context() as session:
        if fail_nonterminal_runs:
            await AgentRunRepository(session).fail_nonterminal_for_storage_migration()
        await ensure_options_in_db(session)
        await migrate_system_options(
            session,
            legacy_config_file=get_legacy_storage_dir() / "config/base.toml",
        )
        await session.commit()


def _require_supported_version(
    domain: str,
    actual: int | None,
    expected: int,
    *,
    upgrade_from: tuple[int, ...] = (),
) -> None:
    """接受未版本化 baseline、当前版本与显式可升级版本。"""
    if actual not in (None, expected, *upgrade_from):
        raise RuntimeError(f"Unsupported {domain} schema version: {actual}; expected {expected}")


async def main() -> None:
    """独占迁移数据库 Schema，并在停机窗口切换历史文件 Owner。"""
    pg_manager.initialize()
    try:
        async with pg_manager.schema_migration_lock():
            async with pg_manager.get_owner_session_context() as session:
                workdir_plan = await read_v071_workdir_plan(session)
            migrates_workdirs = workdir_plan.requires_cutover or bool(workdir_plan.workdirs)
            requires_quiescence = (
                migrates_workdirs
                or _legacy_skill_roots_exist()
                or _legacy_system_config_exists()
                or runtime_storage_requires_quiescence()
            )
            if requires_quiescence:
                _require_quiescence_proof()

            await pg_manager.create_schema_version_table()
            versions = await pg_manager.get_schema_versions()
            business_version = versions.get("business")
            _require_supported_version(
                "business",
                business_version,
                BUSINESS_SCHEMA_VERSION,
                upgrade_from=(2, 7, 8, 9, 10, 11, 12, 13),
            )
            knowledge_version = versions.get("knowledge")
            _require_supported_version(
                "knowledge",
                knowledge_version,
                KNOWLEDGE_SCHEMA_VERSION,
                upgrade_from=(1,),
            )

            if business_version is None:
                await pg_manager.create_business_tables()
            if migrates_workdirs:
                await asyncio.to_thread(import_v071_workdirs, workdir_plan.workdirs, workdir_plan.conversations)
                async with pg_manager.get_owner_session_context() as session:
                    for statement in V071_WORKDIR_CUTOVER_STATEMENTS:
                        await session.execute(text(statement))
                    await rewrite_v071_workdir_paths(session)
                    await verify_workdir_bindings(session)
                    await session.commit()
            if business_version in {None, 2, 7}:
                await pg_manager.ensure_business_schema()
                if business_version is None:
                    await pg_manager.setup_langgraph_checkpointer()
            if business_version in {None, 2, 7, 8}:
                await pg_manager.upgrade_agent_resource_selection()

            if knowledge_version is None:
                await pg_manager.create_knowledge_tables()
                await pg_manager.ensure_knowledge_schema()
                await pg_manager.record_schema_version("knowledge", KNOWLEDGE_SCHEMA_VERSION)
            elif knowledge_version == 1:
                await pg_manager.upgrade_knowledge_schema_v1_to_v2()
                await pg_manager.record_schema_version("knowledge", KNOWLEDGE_SCHEMA_VERSION)

            # 租户模型必须排在**两个域都建表之后**：它给归属根加列，还要给所有受保护表建
            # RLS 策略（`CREATE POLICY ... ON <表>` 的 IF EXISTS 只守策略、不守表，表不存在
            # 就直接报错）。全新数据库走到这里时知识域的表刚刚才建出来。
            # `business_version` 是进入本轮时读到的快照，上面的收敛可能已把库里写成 9，
            # 因此这里用快照比较，不重读。
            if business_version != BUSINESS_SCHEMA_VERSION:
                await pg_manager.ensure_tenant_schema()
                await pg_manager.ensure_tenant_rls()
                # **存量预检与装约束同一事务 + 全程停写**：预检只证明"提交那一刻没有跨库子行"，
                # 触发器只约束新写入；分两个事务会在"预检已过、触发器未装"之间留一段写入窗口。
                # `install_tenant_reference_consistency()` 对受保护表持 `ACCESS EXCLUSIVE`
                # 停写锁（与触发器 DDL 所需强度一致，避免锁升级），把这段窗口关掉
                # （详见该方法 docstring）。
                await pg_manager.install_tenant_reference_consistency()
                # 归属根护栏**不属于** `ensure_tenant_schema()`（那会无记录地改写既有版本语义），
                # 随本版本单独装。
                await pg_manager.ensure_tenant_root_immutability()
                # **发现边界必须最后**：它依赖上面的 blanket 授权先建立，再用 REVOKE 收窄。
                # 语句幂等，但顺序不幂等——先跑边界、再重放 blanket 会把权限放回来。
                await pg_manager.ensure_tenant_discovery_boundary()

            await _converge_database_state(fail_nonterminal_runs=requires_quiescence)
            legacy_config_file = get_legacy_storage_dir() / "config/base.toml"
            if legacy_config_file.is_file() and not legacy_config_file.is_symlink():
                legacy_config_file.unlink()
            if migrates_workdirs:
                await asyncio.to_thread(cleanup_v071_thread_sources, workdir_plan.conversations)
            # Skill 迁移**会写库并提交**（改 `Skill.dir_path`），属于"会改数据库的步骤"，必须排在
            # 版本记录之前——否则它失败时库里已经写着 14，重跑会跳过全部租户步骤，半成品被当已完成。
            async with pg_manager.get_owner_session_context() as session:
                await migrate_shared_skills(session)
            # **业务版本记录后于全部会写库的步骤**：上面任何一步失败都不写 14，重跑可续。
            # 记录放在收敛之后，是为了不让"护栏已装但数据未收敛"的中间态被标成已完成。
            #
            # 版本号只认证**数据库**变换。文件侧迁移（v071 workdir 收敛、Skill 迁移标记、
            # v072 运行时身份）各有自己的完成标记与幂等路径，**不并入版本契约**：它们失败时
            # 不回滚版本，也不需要版本号证明。两层契约分开读，别混。
            if business_version != BUSINESS_SCHEMA_VERSION:
                await pg_manager.record_schema_version("business", BUSINESS_SCHEMA_VERSION)
            mark_v071_skills_migrated()
            await asyncio.to_thread(migrate_runtime_storage_identity)
    finally:
        await pg_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
