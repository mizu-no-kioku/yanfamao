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

跑完这一轮，测试实例上就是 `business=13 / knowledge=2` 的库——Part 2 的测试 worker 需要这个状态
（`_worker_startup` 第一步是 `require_current_schema()`）。这条前置由 `conftest.py` 的
`migrated_test_database` 提供，session 级只跑一次。

**前提：`REDIS_TEST_URL` 必须在 api 容器里**（测试 worker 用它连独立 Redis）。compose 里已经声明；
改过 compose 的**环境变量**后必须 `up -d` 重建容器，`restart` 不生效——否则这一步会硬失败并告诉你去重建。

## 两轮分别断言什么

**第一轮：空库完整迁移**（`test_empty_database_migration.py`）

| 断言 | 依据 |
|---|---|
| 迁移子进程退出 0 | `test_the_full_migration_exits_zero_...` |
| 库里记着 `business=13 / knowledge=2` | `test_the_database_records_business_13_and_knowledge_2` |
| **RLS 仍未启用**（且确实建出了表，非空转） | `test_the_full_migration_does_not_enable_rls` |
| 每个文件根都被显式改道到本轮临时根 | `test_the_child_environment_overrides_every_file_root` |
| 产物全落在临时根（诱饵 cwd 保持为空） | `test_no_artifact_escapes_the_round_root` |
| 开发目录未被写入 | `test_development_directories_are_not_written` |

**第二轮：worker 子进程与健康记录的归属**（`test_worker_health_attribution.py`）

`yuxi:worker:health:*` 的键名是全局的、`redis-test` 又跨轮复用，"键存在"只说明某个 worker 报过到。
所以本轮的判定链是：**从目标子进程自己的日志**取 `worker_id`（`_worker_startup` 的启动行
`Worker startup ready: worker_id=<id>`）→ 再要求两个收敛健康键的值与它**精确相等**。
没有这条启动行就无法归属，因此那条断言同时是 Step C 的 RED 门。

| 断言 | 依据 |
|---|---|
| worker 在自己日志里报出 `worker_id` | `test_the_worker_announces_its_own_identity_in_its_log` |
| 健康键的值 = 本轮子进程的 `worker_id` | `test_the_health_records_belong_to_the_worker_we_started` |
| worker 也没写到临时根之外（cwd 诱饵为空） | `test_the_worker_does_not_write_outside_the_round_root` |
| 缺配置/指到开发 Redis 一律硬失败 | `test_the_test_redis_is_required_and_must_not_be_the_development_one` |
| 同一时刻只允许一个测试 worker（全局健康键） | `test_the_round_mutex_refuses_a_second_worker` |

失败时保留本轮临时根与日志，并在输出里打印路径与日志尾部；成功则整根删除。

## 已知限制（不要当成已覆盖）

- **"开发目录未被写入"的锚点只有两个标记文件**，不是整棵目录树：`/app/user-data` 与
  `/app/runtime/*` 被运行中的 api/worker 持续写入，整树对比必然抖动。未识别的**第六个根**
  由"诱饵 cwd 必须为空"这条来兜（相对路径默认值会落在那里）。
- 运行身份迁移（`.v072-runtime-identity`）是**预置**的，见 `_migration_round.py` 的 docstring：
  api 容器以 `1000:1000` 运行，而那一步要求 root。预置是如实陈述，不是绕过。
- **迁移轮次断言"库是空的"，所以每跑一轮都要先重建 `postgres-test`**；第二轮（worker）本身不要求
  空库，但复用同一套 session 前置，因此整目录一起跑时前置仍然是"全新库"。
- Step D（启动补发 / 周期补发两个场景）**未做**：本轮只证明"能起、能归属、能回收"，
  还没有断言补发时机。`TASK_RECONCILIATION_SECONDS` 也还没改成 env 驱动。
