"""Part 2 Step B：完整迁移入口在独立实例 + 临时文件根上的空库验收。

一轮的前置、机制与已知限制见同目录 `_migration_round.py` 的模块 docstring 与 `README.md`。
简言之：**先重建 `postgres-test`**（tmpfs，重建即空库），再跑本文件。

这一轮是 Part 2 Step C/D 的前置：`_worker_startup` 第一步是 `require_current_schema()`，
要求库里记着精确的 `business=13 / knowledge=2`；现有隔离 schema 夹具只建表不写版本记录，
所以测试 worker 在它们上面起不来。本文件把这步跑通。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import _migration_round as round_module
from _migration_round import EXPECTED_VERSIONS, ROOT_ENV_VARS, MigrationRound

pytestmark = [pytest.mark.integration]

@pytest.fixture(name="migration_round")
def migration_round_fixture(migrated_test_database: MigrationRound) -> MigrationRound:
    """conftest 里 session 级的迁移轮次；这里只是让断言读起来是"本轮这一轮"。"""

    return migrated_test_database


def test_the_child_environment_overrides_every_file_root(migration_round: MigrationRound) -> None:
    """结构性证据：每个文件根都在环境里被显式改道到本轮临时根。"""

    for name in ROOT_ENV_VARS:
        value = migration_round.env.get(name, "")
        assert value, f"{name} 没有被显式设置——子进程会继承开发路径"
        assert Path(value).is_relative_to(migration_round.root), f"{name} 指向了临时根之外：{value}"


def test_the_full_migration_exits_zero_and_keeps_its_log_in_the_round_root(migration_round: MigrationRound) -> None:
    assert migration_round.captured is not None, "迁移子进程没有跑起来"
    assert migration_round.returncode == 0, (
        f"迁移子进程退出 {migration_round.returncode}（超时={migration_round.timed_out}）\n"
        f"{migration_round.log_tail()}"
    )
    assert migration_round.log_path.is_file(), "迁移日志没有落在本轮临时根里"


async def test_the_database_records_business_14_and_knowledge_2(migration_round: MigrationRound) -> None:
    versions = await round_module.schema_versions()
    assert versions == EXPECTED_VERSIONS


async def test_the_full_migration_enables_rls(migration_round: MigrationRound) -> None:
    """版本 14 的迁移入口**会**启用 RLS——策略随本版本一起接线（此前只建表不启用）。

    这是版本 14 与 13 的行为分界：13 只建表，策略等隔离验证通过后单独接线；14 把
    RLS + 引用一致性 + 归属根护栏 + 发现权限边界一起纳入迁移门禁。
    """

    tables = await round_module.public_table_count()
    assert tables > 0, "迁移没有建出任何表，这条断言会退化成空转"
    assert await round_module.rls_enabled_table_count() > 0


def test_no_artifact_escapes_the_round_root(migration_round: MigrationRound) -> None:
    """产物必须全部落在临时根：诱饵 cwd 保持为空 + 正向标记出现在临时根里。"""

    leftovers = sorted(path.name for path in migration_round.child_cwd.iterdir())
    assert leftovers == [], f"子进程在 cwd 里留下了内容，说明还有文件根没被隔离：{leftovers}"

    skill_marker = migration_round.roots["YUXI_SKILL_DATA_DIR"] / round_module.SKILL_MIGRATION_MARKER
    assert skill_marker.is_file(), (
        f"临时 Skill 根里没有迁移完成标记 {round_module.SKILL_MIGRATION_MARKER}——"
        "迁移没有写到本轮临时根上"
    )


def test_development_directories_are_not_written(migration_round: MigrationRound) -> None:
    """开发目录里迁移会写的标记文件，本轮前后必须一模一样。"""

    assert migration_round.development_markers_before["skill"][0], (
        "开发 Skill 目录里没有迁移标记，这条断言没有锚点（先确认开发容器跑过迁移）"
    )

    after = round_module.development_marker_paths()
    for name, before in migration_round.development_markers_before.items():
        assert round_module.marker_state(after[name]) == before, f"开发目录里的 {name} 标记被本轮改写了：{before}"
