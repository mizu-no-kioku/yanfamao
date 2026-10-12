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

## 四组分别断言什么

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
| worker 也没写到临时根之外（cwd 诱饵为空、沙箱端点改道） | `test_the_worker_does_not_write_outside_the_round_root` |
| 同一时刻只允许一个测试 worker（全局健康键） | `test_the_round_mutex_refuses_a_second_worker` |

**第三组：补发**时机**（`test_durable_task_republish.py`，Step D）**

两个场景只差两件事——意图相对 worker 启动的时刻，与注入的周期：

| 场景 | 意图创建于 | 注入的 `TASK_RECONCILIATION_SECONDS` | 唯一能救它的路径 |
|---|---|---|---|
| 启动补发 | worker 启动**之前**（`managed_worker_round` 的 `prepare` 钩子） | `3600`（周期等于不发生） | `_worker_startup` 的 `reconcile_and_publish_tasks()` |
| 周期补发 | worker **就绪之后** | `1` | `_reconcile_durable_tasks_forever()` 的 tick |

| 断言 | 依据 |
|---|---|
| 启动前落库的 pending 意图被推到 `success` | `test_startup_reconciliation_republishes_...` |
| 就绪后落库的 pending 意图被推到 `success` | `test_periodic_reconciliation_republishes_...` |

两条都**读库**判终态：`pending → running → success`（`attempt_count == 1`）与领域 success hook
写下的数据集 `build_metadata.status == "completed"`。**日志不算证据**——它只说明"收敛循环跑了"。
`attempt_count == 1` 同时排除了"被收敛循环直接判失败"那条路径（`fail_pending` 不增加 attempt_count）。

**周期设成 3600 不是顺手调大超时**：它让两条路径不可能互相冒充。已用**差分变异探针**实测：
注释掉启动收敛 → 只有启动场景红；注释掉周期收敛 → 只有周期场景红。

**第四组：脚手架自身**（`test_round_scaffolding.py`）

守卫是否**接线**且**每次核对**（不缓存——缓存过的版本只记部分输入，"测试 URL 不变、`REDIS_URL`
被换成同一目标"就能绕过）、报错不泄漏凭证、租约必需且原子释放并自动续期、
**真实场景下丢锁会回收整组并把本轮判失败**、组长先退出/后代忽略 TERM 时的整组回收、
回收返回后日志已写完、以及"断言失败也要保留产物"的判定。

另含**等待期间的三项健康核对**（`assert_round_health`）：租约未丢 / 无后台回收错误 /
**worker 仍存活**。第三项区分了两种"进程不在了"：本轮主动停它（`stop_worker_round` 先置
`stopping`）是预期的；它自己提前退出则**立刻**失败，而不是等到业务超时——超时会把
"进程崩了"报告成"业务没收敛"。

失败时保留本轮临时根与日志，并在输出里打印路径与日志尾部；成功则整根删除。
**保留与否看 pytest 的判定结果**，不只看子进程退出码——退出码 0 只说明"跑完了"。
覆盖范围是 **setup / call 阶段的失败**；与本夹具同批的 teardown 报告产生于清理**之后**，
所以"某个在其后才失败的 teardown 步骤"不在覆盖内。

## 已知限制（不要当成已覆盖）

- **"开发目录未被写入"的锚点只有两个标记文件**，不是整棵目录树：`/app/user-data` 与
  `/app/runtime/*` 被运行中的 api/worker 持续写入，整树对比必然抖动。未识别的**第六个根**
  由"诱饵 cwd 必须为空"这条来兜（相对路径默认值会落在那里）。
- **隔离边界只说被显式指定的那些**：四个连接、五个文件根、导入路径，以及被改道到
  `http://127.0.0.1:1` 的沙箱 provisioner（真走到沙箱会立刻失败，不会打到开发 provisioner）。
  Neo4j / Milvus / MinIO **没有**隔离。
  Step D 起本轮**会投递 job**（`dataset_generation`），所以"不投递 job"这句已经作废；
  之所以仍然安全，是因为探针意图的 payload 是 `count=0`，`_generate_dataset_task` 在
  `existing_count (0) >= total_count (0)` 处**短路返回**，既不查 Milvus 也不调 LLM。
  这是**路径选择**而不是隔离：换一个 `count > 0` 的意图，本轮就会打到开发的 Milvus 上。
- 这一步应称为**「带已满足身份前置条件的完整入口验收」**，不是身份迁移本身的证据：运行身份迁移
  （`.v072-runtime-identity`）在 api 容器里是**预置**的（该容器以 `1000:1000` 运行，而迁移步骤要求
  root）。预置是如实陈述的条件：标记内容 `UID:GID` 的**两个值都被核对**，且临时根的归属被断言过。
  身份迁移本身需要在可控的 root 环境里另行验收。
- **迁移轮次断言"库是空的"，所以每跑一轮都要先重建 `postgres-test`**；第二轮（worker）本身不要求
  空库，但复用同一套 session 前置，因此整目录一起跑时前置仍然是"全新库"。
- **`services/` 里那条同名旧用例仍依赖共享 worker 容器的周期**。
  `test_durable_task_worker_path.py::test_shipping_worker_periodic_reconciliation_recovers_pending_publication`
  的窗口已改为**由周期推导**（`TASK_RECONCILIATION_SECONDS × 2 + 余量`，原为写死的 15s = 周期的一半，
  实测 4 次里红 2 次），但它的**确定性仍受容器 loop 相位影响**，只是窗口现在覆盖得住。
  真正的紧证据是上面第三组的两个场景。
