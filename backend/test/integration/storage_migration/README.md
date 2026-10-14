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

跑完这一轮，测试实例上就是 `business=14 / knowledge=2` 的库——Part 2 的测试 worker 需要这个状态
（`_worker_startup` 第一步是 `require_current_schema()`）。这条前置由 `conftest.py` 的
`migrated_test_database` 提供，session 级只跑一次。

**前提：`REDIS_TEST_URL` 必须在 api 容器里**（测试 worker 用它连独立 Redis）。compose 里已经声明；
改过 compose 的**环境变量**后必须 `up -d` 重建容器，`restart` 不生效——否则这一步会硬失败并告诉你去重建。

## 各组分别断言什么

**第一轮：空库完整迁移**（`test_empty_database_migration.py`）

| 断言 | 依据 |
|---|---|
| 迁移子进程退出 0 | `test_the_full_migration_exits_zero_...` |
| 库里记着 `business=14 / knowledge=2` | `test_the_database_records_business_14_and_knowledge_2` |
| **RLS 已启用**（且确实建出了表，非空转） | `test_the_full_migration_enables_rls` |
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
| 业务断言在轮次内失败时，临时根与日志仍在 | `test_a_failing_business_assertion_inside_the_round_keeps_the_scene` |

**业务断言写在 `async with` 里面**，不是外面：收尾要看得到断言抛出的异常才会保留现场；
写在外面的话，收尾已按"轮次正常"删掉临时根，核心断言失败时反而没有日志可看。第三条用例
用**真实的业务断言失败**（把意图的 `type` 换成不注册的类型 → 收敛判成 `failed`）把这条钉住。

两条都**读库**判终态：`pending → running → success`（`attempt_count == 1`）与领域 success hook
写下的数据集 `build_metadata.status == "completed"`。**日志不算证据**——它只说明"收敛循环跑了"。
`attempt_count == 1` 同时排除了"被收敛循环直接判失败"那条路径（`fail_pending` 不增加 attempt_count）。

**周期设成 3600 不是顺手调大超时**：它让两条路径不可能互相冒充。已用**差分变异探针**实测：
注释掉启动收敛 → 只有启动场景红；注释掉周期收敛 → 只有周期场景红。

**坏消息消费链**（同文件，`test_bad_message_is_consumed_without_execution_then_periodic_republish_completes`）

上面两条验的是"补发时机"，这条验的是**错误消息的恢复**：队列里是一条**租户信息错/缺失**的消息
（参数化 `message_tenant=[None, 1]`，而任务的持久化租户是 2）。它必须被 **ARQ 消费掉、但不执行**，
随后**周期补发**用**持久化租户**重新发布并以正确租户完成。

做法：先 `LOCK TABLE tenants IN ACCESS EXCLUSIVE MODE` 把发现路径挡住（`known_tenant_ids()` 取不到
租户 id → 补发**发不出去**），再用连接池**直接投递**坏参数的消息（`process_task(task_id)` 或
`process_task(task_id, 1)`）。此时断言：

| 断言 | 含义 |
|---|---|
| job 被消费且 `success` | 坏消息**不会**卡在队列里反复重试 |
| 任务仍是 `pending`、`attempt_count == 0`、`worker_id is None` | **没有执行**——`process_task` 缺租户/租户不符时 fail-closed |
| 解锁后轮次最终 `success`、`attempt_count == 1`、数据集 `completed` | 周期补发用**持久化租户**重发并完成 |
| `pool.all_job_results()` 里有 `process_task` 参数 `(task_id, 2)` 的 success | 补发用的**确实是**持久化租户，不是默认 1 |

**为什么用锁而不是"投递坏消息后干等"**：解锁前补发**不可能**成功，所以"任务还是 pending"这条断言
不可能是"恰好在观察窗口内还没被补发"的巧合；解锁才是唯一让它前进的事件。

**第四组：迁移中途失败与重放**（`test_migration_failure_and_replay.py`）

前四组都建立在"迁移**跑成功了**"之上。这组问的是反面：**中途失败会不会把半成品库记成已完成**。

| 断言 | 依据 |
|---|---|
| 注入失败的那一步**不记录** `business=14` | `test_a_failed_step_does_not_record_version_and_a_rerun_completes` |
| 失败点之后重跑**同一条真实入口**能续上并完成 | 同上（参数化 `ensure_tenant_rls` / `ensure_tenant_discovery_boundary`） |
| 已是 14 的库上重放**幂等**：版本不变、RLS 不被关掉、授权不回退 | `test_rerunning_the_migration_on_a_current_database_is_idempotent` |

注入方式是**真实入口** `python -m yuxi.storage_migration` + 把 `ensure_tenant_rls` /
`ensure_tenant_discovery_boundary` 换成抛错的函数（`python -c`），不是把 `record_schema_version`
换成桩——桩只能证明"桩被调过"。

两个失败点覆盖**两种形态**：`ensure_tenant_rls` 失败 → 库上 RLS 表数 **0**（什么都没装）；
`ensure_tenant_discovery_boundary` 失败 → RLS 表数 **33**（**前段已提交**）。后者才是真正的
**部分迁移重放**——它证明"已提交的前段不会被回退"，而不只是"失败就整笔没发生"。

**口径注意**：全新库在租户步骤**之前**会先落一条**遗留基线** `business=9`
（由 `upgrade_agent_resource_selection()` 记录，既有行为）。所以断言写的是
`business != 14`（14 才是"重跑时跳过全部租户步骤"的那道闸），不是"一条都没记"。

重放后还核对**角色授权**没有回退：`yuxi_app` 对 `tenants` 无任何列权限、
`yuxi_worker_discovery` 只有 `tenants(id)` 的列级 `SELECT`（`name` 不可读）。

**中途失败用独立数据库**（`CREATE DATABASE` 临时库，跑完 `DROP ... WITH (FORCE)`），不动会话级的
`yuxi_test`。方式见 `_migration_round.py`：`run_full_migration()` 允许对**同一个轮次**调用多次，
每次分到一个**全新**的日志文件（`migration.log` / `migration.run2.log`）——共用一个文件会把
**第一轮（失败那轮）**的日志冲掉，而"保留失败现场"要留的正是它。
`test_a_second_run_does_not_overwrite_the_first_runs_log` 把这条钉住。

**第五组：脚手架自身**（`test_round_scaffolding.py`）

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
  坏消息消费那条也投递同一个探针任务。之所以仍然安全，是因为探针意图的 payload 是 `count=0`，
  `_generate_dataset_task` 在 `existing_count (0) >= total_count (0)` 处**短路返回**，既不查 Milvus
  也不调 LLM。这是**路径选择**而不是隔离：换一个 `count > 0` 的意图，本轮就会打到开发的 Milvus 上。
- 这一步应称为**「带已满足身份前置条件的完整入口验收」**，不是身份迁移本身的证据：运行身份迁移
  （`.v072-runtime-identity`）在 api 容器里是**预置**的（该容器以 `1000:1000` 运行，而迁移步骤要求
  root）。预置是如实陈述的条件：标记内容 `UID:GID` 的**两个值都被核对**，且临时根的归属被断言过。
  身份迁移本身需要在可控的 root 环境里另行验收。
- **迁移轮次断言"库是空的"，所以每跑一轮都要先重建 `postgres-test`**；第二轮（worker）本身不要求
  空库，但复用同一套 session 前置，因此整目录一起跑时前置仍然是"全新库"。
  **第四组（中途失败／重放）是例外**：中途失败在**另建的一次性库**上跑，不消耗"空库"这个前置；
  只有"对已是 14 的库重放"那条复用会话级的 `yuxi_test`。
  **那个一次性库不自动删除**：脚手架只在收尾时把库名与 `DROP DATABASE` 语句打出来，交人工处理。
  自动 `DROP ... WITH (FORCE)` 会在用例失败时把现场连同库一起销毁，且 `FORCE` 会强断其他会话——
  破坏性存储操作不由测试代劳。
  **收尾只在整套断言都通过时清理产物**：异常 / 断言失败 / 超时一律保留两轮日志与测试库。
- **第三组的两个场景是"补发时机"的证据**。它们在**版本 14**（`business=14`、RLS 已启用）的测试
  实例上取得：测试 worker 走受限角色 `yuxi_app` + 独立发现凭据 `yuxi_worker_discovery`，任务
  **携带并恢复**正确的租户上下文。探针用 owner 连接写前置数据，那只是**安排测试前置条件**的手段
  （owner 绕过 RLS），**不构成**"普通请求也受隔离"的证据。
- **`services/` 里那条同名旧用例仍依赖共享 worker 容器的周期，且尚未版本 14 就绪**。
  `test_durable_task_worker_path.py` 的两条 shipping-worker 用例有两个未关闭的前置：
  1. **依赖常驻 worker 容器**做周期收敛——`test_shipping_worker_periodic_reconciliation_recovers_pending_publication`
     的窗口已改为**由周期推导**（`TASK_RECONCILIATION_SECONDS × 2 + 余量`），但确定性仍受容器
     loop 相位影响；更根本的是它依赖**前一个用例**留下的状态与**共享 worker 容器**，因此
     **不是独立、完全隔离的测试**。
  2. **在版本 14 实例上，它们自身 setup 就被 RLS 拒绝**：
     `InsufficientPrivilegeError: new row violates row-level security policy for table "knowledge_bases"`
     ——它们经 `yuxi_app` 写夹具却没有建立租户上下文。**对照实验**：主树未改动的 HEAD 在同一实例上
     通过，因为主树的 `_apply_tenant_context` 会**静默回落到 `DEFAULT_TENANT_ID`**；本分支刻意
     去掉该兜底（`explicit_tenant_id()` 不回落）。因此这两条用例需先建立**显式租户上下文**
     （或夹具改用 owner 会话），并补常驻 worker，才能作为版本 14 验收。
  本轮的主要证据是上面第三组的两个场景，那两条旧用例不是。
