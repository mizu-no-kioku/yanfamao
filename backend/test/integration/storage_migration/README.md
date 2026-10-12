# 迁移验收（Part 2）

这一目录验的是**完整迁移入口**（`python -m yuxi.storage_migration`，与生产 `storage-migrator`
同一个模块）在**独立 PostgreSQL 实例 + 临时文件根**上的行为，与运行中的 API/worker 无关。

它不在 `test/integration/services` 里，是**故意的**：一轮的前置是**全新库**，跑完会给测试
实例留下已迁移的 `public`。放进默认套件会让那套命令第二次跑就红。

## 一轮怎么跑

```bash
# 1. 重建测试实例——tmpfs，重建即空库。不做"清残留"：清漏一处就会让空库验收假通过。
docker compose --profile test-db up -d --force-recreate postgres-test redis-test

# 2. 跑一轮
docker compose exec -T api uv run --no-sync pytest test/integration/storage_migration -q -p no:cacheprovider
```

跑完这一轮，测试实例上就是 `business=13 / knowledge=2` 的库——Part 2 Step C/D 的测试 worker
需要这个状态（`_worker_startup` 第一步是 `require_current_schema()`）。

## 这一轮断言什么

| 断言 | 依据 |
|---|---|
| 迁移子进程退出 0 | `test_the_full_migration_exits_zero_...` |
| 库里记着 `business=13 / knowledge=2` | `test_the_database_records_business_13_and_knowledge_2` |
| **RLS 仍未启用**（且确实建出了表，非空转） | `test_the_full_migration_does_not_enable_rls` |
| 每个文件根都被显式改道到本轮临时根 | `test_the_child_environment_overrides_every_file_root` |
| 产物全落在临时根（诱饵 cwd 保持为空） | `test_no_artifact_escapes_the_round_root` |
| 开发目录未被写入 | `test_development_directories_are_not_written` |

失败时保留本轮临时根与 `migration.log`，并在输出里打印路径与日志尾部；成功则整根删除。

## 已知限制（不要当成已覆盖）

- **"开发目录未被写入"的锚点只有两个标记文件**，不是整棵目录树：`/app/user-data` 与
  `/app/runtime/*` 被运行中的 api/worker 持续写入，整树对比必然抖动。未识别的**第六个根**
  由"诱饵 cwd 必须为空"这条来兜（相对路径默认值会落在那里）。
- 运行身份迁移（`.v072-runtime-identity`）是**预置**的，见 `_migration_round.py` 的 docstring：
  api 容器以 `1000:1000` 运行，而那一步要求 root。预置是如实陈述，不是绕过。
