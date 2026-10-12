"""一轮完整迁移的脚手架：唯一临时根 + 一次 `yuxi.storage_migration` 子进程。

## 一轮 = 一个空库

"空库迁移"验的是 `business_version is None` 那条路径。在**非空**库上重跑，迁移会因为版本已经是
13 而跳过建表与租户步骤——子进程照样退出 0，用例照样"通过"，但什么都没验。因此：

- 库必须是全新的。`postgres-test` 的数据目录挂 tmpfs，**重建容器**即得空库：
  `docker compose --profile test-db up -d --force-recreate postgres-test`。
- 本模块**不做任何"清残留"**：清残留漏一处，就会让空库验收因上一轮留下的行而假通过。
  每轮开始时先断言目标库确实是空的，不是空的就直接失败。

## 子进程的 cwd 是一个"诱饵"空目录

五个文件根全部由环境变量显式改道到临时根。但"我以为只有五个"不是证据。把子进程的 `cwd` 指向
临时根下一个**必须保持为空**的目录后，任何**相对路径**默认值（`legacy-saves`、`user-data`、以及
将来新增的根）都会落在那里——跑完发现它非空，就是"还有一个根没被隔离"的直接证据。

停机证明（`YUXI_STORAGE_MIGRATION_QUIESCENCE_*`）**故意不注入**：如果迁移判定自己需要停机
（说明某个历史根仍有内容，即某个根没被隔离），它会因缺证明而硬失败，而不是被一张假证明放行。

## 运行身份迁移标记

`migrate_runtime_storage_identity()` 只在 `euid == 0` 时执行 chown；api/worker 容器以 `1000:1000`
运行（生产由 `storage-migrator` 以 root 跑），所以在 api 容器里跑完整迁移会撞上那条 root 检查。
预先写下该步骤的完成标记是**如实陈述**、不是绕过：这一步要达成的后置条件——持久目录属于
`1000:1000`——对**由同一个 uid 新建的临时根**本来就成立。若当前 euid 既不是 0 也不是
`RUNTIME_UID`，就不写标记：那时后置条件不成立，让迁移自己报 `PermissionError`。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from yuxi.storage_migrations import v071_skills as _v071_skills
from yuxi.storage_migrations import v072_runtime_identity as _runtime_identity
from yuxi.storage_migrations.v072_runtime_identity import RUNTIME_GID, RUNTIME_UID

from _tenant_schema_test_db import assert_target_is_the_test_instance, tenant_schema_url

# 迁移会写到的**全部**文件根。逐项显式覆盖，不靠"继承默认值"——漏一项就是往开发目录写。
ROOT_ENV_VARS: dict[str, str] = {
    "YUXI_USER_DATA_DIR": "user-data",
    "YUXI_SKILL_DATA_DIR": "skill-sources",
    "YUXI_SKILL_PROJECTION_DIR": "skill-projections",
    "YUXI_LEGACY_STORAGE_DIR": "legacy-saves",
    "YUXI_RUNTIME_DIR": "runtime",
}

QUIESCENCE_ENV_VARS = (
    "YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN",
    "YUXI_STORAGE_MIGRATION_QUIESCENCE_FILE",
)

SCHEMA_VERSION_TABLE = "yuxi_schema_migrations"
EXPECTED_VERSIONS = {"business": 13, "knowledge": 2}

CHILD_CWD_DIRNAME = "child-cwd"

# 从生产模块取名，不写字面量：改名会在导入处直接报错，而不是让断言静默落空。
SKILL_MIGRATION_MARKER = _v071_skills._MIGRATION_MARKER
RUNTIME_IDENTITY_MARKER = _runtime_identity._MIGRATION_MARKER
RUNTIME_IDENTITY_CONTENT = f"{RUNTIME_UID}:{RUNTIME_GID}\n"

_LOG_TAIL_LINES = 40


@dataclass
class MigrationRound:
    """一轮验收的全部现场：临时根、子进程环境、运行结果。"""

    root: Path
    roots: dict[str, Path]
    child_cwd: Path
    env: dict[str, str]
    development_markers_before: dict[str, tuple[bool, int, int]] = field(default_factory=dict)
    completed: subprocess.CompletedProcess[str] | None = None

    @property
    def log_path(self) -> Path:
        return self.root / "migration.log"

    def log_tail(self) -> str:
        try:
            content = self.log_path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"(读不到迁移日志 {self.log_path}: {exc})"
        return "\n".join(content.splitlines()[-_LOG_TAIL_LINES:])


def _new_root() -> Path:
    base = os.environ.get("YUXI_TEST_MIGRATION_ROOT_BASE", "").strip() or tempfile.gettempdir()
    base_path = Path(base)
    base_path.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="yuxi-part2-", dir=str(base_path)))


def marker_state(path: Path) -> tuple[bool, int, int]:
    """标记文件的 (存在, mtime_ns, size)。不存在时后两项为 0。"""

    if not path.exists():
        return (False, 0, 0)
    stat = path.stat()
    return (True, stat.st_mtime_ns, stat.st_size)


def development_marker_paths() -> dict[str, Path]:
    """开发目录里、迁移**会写**的两个标记文件——用来核对开发目录没被写。"""

    skill_root = Path(os.environ.get("YUXI_SKILL_DATA_DIR", "").strip() or "skill-sources")
    user_root = Path(os.environ.get("YUXI_USER_DATA_DIR", "").strip() or "user-data")
    return {
        "skill": skill_root / SKILL_MIGRATION_MARKER,
        "runtime-identity": user_root / RUNTIME_IDENTITY_MARKER,
    }


def seed_runtime_identity_marker(user_data_root: Path) -> bool:
    """见模块 docstring「运行身份迁移标记」。返回是否写入了标记。"""

    if os.geteuid() != RUNTIME_UID:
        return False
    (user_data_root / RUNTIME_IDENTITY_MARKER).write_text(RUNTIME_IDENTITY_CONTENT, encoding="ascii")
    return True


def build_migration_env(roots: dict[str, Path]) -> dict[str, str]:
    """构造迁移子进程的环境：显式覆盖每个文件根，并清掉停机证明。"""

    env = dict(os.environ)
    for name in QUIESCENCE_ENV_VARS:
        env.pop(name, None)
    # owner 连接指向独立测试实例；父进程的 POSTGRES_URL 是开发实例，必须换掉。
    env["POSTGRES_URL"] = tenant_schema_url()
    # 运行时连接回落 owner：迁移只走 owner 会话，而 yuxi_app 在全新库上还不存在。
    # 父进程的 POSTGRES_APP_URL 指向开发实例，必须显式**清空**而不是留着继承。
    env["POSTGRES_APP_URL"] = ""
    for name, path in roots.items():
        env[name] = str(path)
    return env


def start_round() -> MigrationRound:
    """建唯一临时根、装好子进程环境，并记下开发目录标记的当前状态。"""

    root = _new_root()
    roots: dict[str, Path] = {}
    for env_name, subdir in ROOT_ENV_VARS.items():
        path = root / subdir
        path.mkdir()
        roots[env_name] = path
    child_cwd = root / CHILD_CWD_DIRNAME
    child_cwd.mkdir()
    seed_runtime_identity_marker(roots["YUXI_USER_DATA_DIR"])
    return MigrationRound(
        root=root,
        roots=roots,
        child_cwd=child_cwd,
        env=build_migration_env(roots),
        development_markers_before={name: marker_state(path) for name, path in development_marker_paths().items()},
    )


def run_full_migration(round_: MigrationRound, *, timeout_seconds: float = 300.0) -> None:
    """跑**完整迁移入口**（与 production 的 storage-migrator 同一个模块）。"""

    command = [sys.executable, "-m", "yuxi.storage_migration"]
    completed = subprocess.run(
        command,
        cwd=str(round_.child_cwd),
        env=round_.env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    round_.completed = completed
    round_.log_path.write_text(
        "\n".join(
            [
                f"$ {' '.join(command)}",
                f"[cwd] {round_.child_cwd}",
                f"[exit] {completed.returncode}",
                "--- stdout ---",
                completed.stdout,
                "--- stderr ---",
                completed.stderr,
                "",
            ]
        ),
        encoding="utf-8",
    )


def finish_round(round_: MigrationRound) -> None:
    """成功清理本轮资源；失败保留日志与临时产物，并把路径打出来。"""

    succeeded = round_.completed is not None and round_.completed.returncode == 0
    if succeeded:
        shutil.rmtree(round_.root, ignore_errors=True)
        return
    print(f"[part2] 本轮迁移未成功，保留临时根：{round_.root}")
    print(round_.log_tail())


async def assert_target_is_the_test_instance_and_database_is_empty() -> None:
    """防误连守卫 + 空库断言。两者都必须在迁移**之前**完成。"""

    await assert_target_is_the_test_instance()
    engine = create_async_engine(tenant_schema_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            tables = await connection.scalar(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
            )
            version_table = await connection.scalar(text(f"SELECT to_regclass('{SCHEMA_VERSION_TABLE}') IS NOT NULL"))
    finally:
        await engine.dispose()
    if tables or version_table:
        raise RuntimeError(
            "迁移验收要求全新库，但目标库不是空的"
            f"（public 有 {tables} 张表，{SCHEMA_VERSION_TABLE} 存在={version_table}）。"
            "重建测试实例后再跑：docker compose --profile test-db up -d --force-recreate postgres-test"
        )


async def schema_versions() -> dict[str, int]:
    engine = create_async_engine(tenant_schema_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(text(f"SELECT domain, version FROM {SCHEMA_VERSION_TABLE}"))).all()
    finally:
        await engine.dispose()
    return {domain: version for domain, version in rows}


async def public_table_count() -> int:
    engine = create_async_engine(tenant_schema_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
            )
    finally:
        await engine.dispose()
    return int(value)


async def rls_enabled_table_count() -> int:
    """public 下启用了 RLS（enabled 或 forced）的基表数。"""

    statement = """
        SELECT count(*)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'
          AND (c.relrowsecurity OR c.relforcerowsecurity)
    """
    engine = create_async_engine(tenant_schema_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(text(statement))
    finally:
        await engine.dispose()
    return int(value)
