# 版本 14（多租户隔离）第 ② 步 —— 第十轮交接文档

> 面向：接手这份工作的下一位工程师 / 审查者
> 分支：`part2/version-14-migration`　worktree：仓库外的独立 worktree（本文件不写死绝对路径，见 §5 的 `$YUXI_V14_WT`）
> 状态：**未提交**（等独立审查）、**未迁移开发库**
> 日期：2026-10-13

---

## 0. 一句话结论

第九轮留下的"未验收"项——**预检并发窗口（停写锁契约）、连接池复用后的无上下文拒绝、
坏消息恢复整链、迁移中途失败与重放**——本轮都在**隔离环境**里取得了**有变异探针的新增证据**
（四项各自的证据集合本轮实测全绿：`storage_migration/` 43 条、`guardrails` 65 条）。

**但回归不是全绿**：受影响单元集合是 `216 passed + 1 teardown ERROR`。该 ERROR 已用 HEAD 基线
对照证明**不是本轮引入**，但那只能支持"非新增"，**不能算验收通过**——它仍是**未关闭项**（§1、§5）。

> **更新（合并主树连接池修复之后）**：该 teardown ERROR **本次回归未复现**。
> `test/unit/services/test_run_worker.py` 合并前实测 `52 passed + 1 ERROR / 139.36s`，
> **合并后 `56 passed / 0 ERROR / 20.72s`**。措辞据此改为"**本次回归未复现**"——
> 这是复跑证据，**不是**"已根治"的证明。

**ruff 口径**：CI 跑**三条**命令（目录仅 `backend/package`，ruff **0.16.4**）：`ruff check` /
`ruff format --check` / `ruff check --select I`。三条均已用"导出 HEAD 基线、跑同一组命令再对比"
的口径实测，结果为**零新增违规**——`format` 与 `--select I` 与基线逐项相同（§5）。

**仍未关闭的安全项**：`SECURITY INVOKER` 隐藏行 fail-open。它与本轮范围无交集，但**不因此解除
升级门槛**——在拿到明确、安全且经独立验收的替代保护前，它应阻断版本 14 的**可部署升级**（§3）。

> 口径限定：本文说"唯一未关闭"时，**只针对本次审查范围**，不代表整个多租户项目的全部遗留。

---

## 1. 验收结论（这一轮到底证明了什么）

| # | 验收项 | 结论 | 证据（文件 :: 用例） |
|---|---|---|---|
| 1 | 连接池复用后**无上下文读 0 行、写被拒** | ✅ 已完成 | `test/integration/services/test_tenant_isolation_guardrails.py` :: `test_runtime_pool_checkout_clears_tenant_after_commit_and_rollback`（真实 `yuxi_app` 登录、`pool_size=1, max_overflow=0`、**生产同一份** `after_begin` 钩子、同一后端 PID 上验 commit **与** rollback）、`test_a_reused_connection_without_context_reads_nothing_and_rejects_writes` |
| 2 | 错误／缺失租户消息**不执行**，随后正确补发成功 | ✅ 已完成 | `test/integration/storage_migration/test_durable_task_republish.py` :: `test_bad_message_is_consumed_without_execution_then_periodic_republish_completes`（参数化消息租户 `[None, 1]`） |
| 3 | **非默认租户**正常完成 | ✅ 已完成 | 同上（任务持久化租户 = **2**，终态 `success` / `attempt_count == 1` / 数据集 `completed`，补发参数断言为 `(task_id, 2)`） |
| 4 | `SECURITY INVOKER` 隐藏行问题 | ⛔ **未关闭** | 无（范围外，见 §3） |
| 5 | 预检→装约束**写入空窗** + 版本记录时机 | ✅ 已完成 | `guardrails` :: `test_the_install_holds_a_stop_write_lock_across_precheck_and_install`、`test_install_failure_rolls_back_trigger_ddl_and_releases_write_locks`；`test/integration/storage_migration/test_migration_failure_and_replay.py`（两条） |
| 6 | `tenants` 权限契约（含授权重放） | ✅ 已完成（第九轮） | `guardrails` 的 9 条权限用例 + 探针 L |

> **表外未关闭项（不得计入上表"已完成"）**：受影响单元集合的 **1 个 teardown ERROR**
> （`test_run_worker.py`，残留 psycopg 异步池在事件循环收尾阻塞约 60s）。
> HEAD 基线同样复现，故**不是本轮引入**；但"非本轮引入"≠"已通过"，它仍是**未关闭的回归缺陷**。

**另外**：审查提出的 `_migration_round.py:221` **日志被覆盖**问题已修并有用例钉住：
`test/integration/storage_migration/test_round_scaffolding.py` ::
`test_a_second_run_does_not_overwrite_the_first_runs_log`。

---

## 2. 本轮关键设计决定（含"为什么不能是别的做法"）

### 2.1 停写锁为什么（在本次实现里）取 `ACCESS EXCLUSIVE`

**这是针对本次实现的选择，不是"所有弱锁方案都会互锁"的普适结论。** 依据只有三条，且都绑定本次形态：
本次要装的是**触发器 DDL**（`DROP TRIGGER` / `CREATE TRIGGER`），PostgreSQL 对它要求
`ACCESS EXCLUSIVE`；本方法**按固定表名顺序逐表取锁**；调用方是**迁移入口那个独立事务**。
三者合起来，若先取弱锁"停写"、执行 DDL 时再**升级**，就多出一段"持弱锁、等强锁"的窗口，
与"先读后写"的并发事务形成**互锁**（锁升级不是原子的）。
一次取到 DDL 所需强度，这个窗口在本次形态下不存在。**换 DDL 种类、换取锁顺序、换事务边界，
结论都要重新论证**，不能照搬。
配套 `SET LOCAL lock_timeout = '10s'`：超时**失败**，不跳过、不降级。

`install_tenant_reference_consistency()` 现在是**同一个事务**里：
`SET LOCAL lock_timeout` → 逐表 `LOCK TABLE ... ACCESS EXCLUSIVE`（固定表名顺序取锁，防与并发迁移互锁）
→ 存量预检 → 触发器 DDL。失败整笔回滚。

### 2.2 `SET LOCAL row_security = off` 是绊线，不是"关闭 RLS"

**先纠正一个容易读错的点**：`row_security = off` **不**让角色绕过 RLS，也**不**赋予任何可见性。
预检能看到全部既有行，靠的是**迁移连接自身的有效权限**（owner / superuser 连接）——
不是这个设置。该设置的作用是**绊线**：预检跑在 `ensure_tenant_rls()` **之后**，
一旦角色配置退化到"查询需要受 RLS 过滤"，PostgreSQL 会让查询**直接报错**，
而不是静默返回被过滤后的子集、让既有跨库子行看不见、预检"看起来通过"。
这条异常路径**不设捕获**，会冒泡中断整个迁移且**不写版本 14**（§2.3），
因此不存在"预检捕获权限错误后继续迁移"。
预检仍然**先于**装约束（子侧触发器只约束新写入，管不到既有行）。

### 2.3 迁移的"事务边界"要分两层读（别混）

- **整个迁移不是原子的**：每步各自 `async_engine.begin()`，没有包住全部步骤的事务。
- **但"预检 + 装约束"这一段是原子的，且带停写锁**（§2.1）。

所以"迁移是原子的"这句话是**错的**；"预检到装约束之间有写入空窗"这句话**已经不再成立**。

### 2.4 迁移日志为什么每轮一个文件

`test_migration_failure_and_replay.py` 会对**同一个 `MigrationRound` 跑两次**（先注入失败、再重放）。
旧写法每次 `write_text` 写表头 = 把**第一轮（失败那轮）**的日志截断——而"保留失败现场"要留的正是它，
报错时贴出来的会变成第二轮的日志，把人引向错误的现场。
现在 `migration.log` / `migration.run2.log` 各占一个文件，`log_tail()` 多轮时**逐轮**给出（不接成一条时间线）。

### 2.5 判据为什么要"收窄"

`_wait_until_blocked()` 原来只判"该后端有任意一把未授予的锁"——它对"等的是不是我们要的表、
挡它的是不是我们要的事务、冲突的是不是预期的锁模式"**都没有分辨力**，任何无关的锁等待都能让断言变绿。
现在支持**表名 + 锁模式 + 阻塞者 pid** 三重收窄；停写锁用例三样全用。

---

## 3. 未关闭 / 遗留（接手请先看这里）

| 项 | 性质 | 说明 |
|---|---|---|
| **`SECURITY INVOKER` 触发器隐藏行 fail-open** | **安全**，未关闭，**且为版本 14 可部署升级的阻断项** | 父侧在 RLS 下对**隐藏子行**的检查行为仍 fail-open。**本轮与上一轮都未动**，需单独立项（属数据库层语义，不是接线问题）。**"本轮范围外"不等于"已允许上线"**：在拿到明确、安全且经独立验收的替代保护之前，该项应**阻断**版本 14 的可部署升级，而不是仅记为待办。 |
| **"唯一未关闭安全项"的口径** | 表述收紧 | 该说法**只在本文件（第十轮）审查范围内**成立，**不代表整个多租户项目**已无其他安全遗留。 |
| shipping-worker 两条用例未版本 14 就绪 | 测试待办 | `test/integration/services/test_durable_task_worker_path.py` 两条：经 `yuxi_app` 写夹具却没建租户上下文 → setup 被 RLS 拒。需补**显式租户上下文**（或夹具改 owner 会话）+ **常驻 worker 前置**。**已用 HEAD 对照实验证明不是环境问题，是本分支有意去掉默认租户兜底的结果。** |
| `require_discovery_credential()` 端到端 | 证据待补 | 只有**函数级**证据；真实 worker 进程在**缺凭据时起不来**的端到端证据未取。 |
| `run_worker.py` 等 16 处 `exc_info=True` | 既有缺陷 | Loguru 下是 no-op（不会输出堆栈）。本轮修的是 Durable Task 链的 5 处；其余 16 处在 agent-run / 定时任务链，属既有同类缺陷。 |

### 3.1 上线阻断清单（决策记录已记名，原交接漏列）

> 来源：`docs/develop-guides/decisions/proposed/2026-10-10-multi-tenant-isolation.md` 的「上线条件」与「风险」两节。

以下诸项在决策记录里**已明确记名**，但本交接文档的未关闭清单原先**一条都没列**。"未列"不等于"不存在"——接手者不得据此认为版本 14 已具备上线条件：

1. **配置域三表对运行时角色完全开放**：`model_providers` / `mcp_servers` / `config_options` 需要的是"读 = 自己 + NULL、写 = 只能写自己的"这类策略，与归属根（比列）、派生表（回父表）两类都不同，**决策记录尚未覆盖**。覆盖测试把它们写成**带理由的例外**，是让缺口被记名，不是宣告隔离完成。
2. **"配置覆盖与缓存隔离完成之前，本部署只支持默认单租户运行，不开放共享实例多租户"**（决策记录原文）。
3. **`yuxi:model_cache` 含明文密钥**：租户隔离完成前，任何能读 Redis 的主体可获得全部租户的 provider 密钥。
4. **checkpoint 三表在 RLS 覆盖之外**：依赖应用层校验，属已知边界而非已完成项。
5. **文件 / 缓存 / 向量 / 图谱层隔离**：Milvus / Neo4j 靠 `kb_id` 传递性隔离，KB 层校验一旦出现缺口，这两层没有任何自防御。

---

## 4. 改动清单（本轮新增／修改）

### 4.1 生产代码

| 文件 | 改动 |
|---|---|
| `package/yuxi/storage/postgres/manager.py` | `install_tenant_reference_consistency()` 改同事务 + `ACCESS EXCLUSIVE` 停写锁 + `lock_timeout`；`_precheck_reference_consistency()` 加 `SET LOCAL row_security = off`；docstring 重写 |
| `package/yuxi/storage_migration.py` | 版本 14 装配顺序：`ensure_tenant_schema → ensure_tenant_rls → install_tenant_reference_consistency → ensure_tenant_root_immutability → ensure_tenant_discovery_boundary → converge → migrate_shared_skills（**会写库并 commit**）→ record_version(14)`。**版本记录后于全部会写库的步骤**；`mark_v071_skills_migrated` / `migrate_runtime_storage_identity` 等文件侧迁移排在其后，由各自标记管理、**不并入版本契约** |
| `package/yuxi/services/run_worker.py` | import 排序修正（本轮 ruff） |
| `package/yuxi/services/task_queue_service.py` | import 段空行修正（本轮 ruff） |
| `package/yuxi/services/task_service.py` | 日志调用换行折叠（本轮 ruff format） |

> 生产代码**逻辑**改动只有前两行；后三行是 ruff 门禁要求的机械修正。

### 4.3 审查轮补修（独立审查之后）

| 文件 | 改动 |
|---|---|
| `package/yuxi/storage_migration.py` | **版本记录时机**：`record_schema_version("business", 14)` 移到 `migrate_shared_skills()`（**会写库并 commit**）之后——它原在之前，Skill 改写失败时版本已被写成 14，重跑会跳过全部租户步骤 |
| `package/yuxi/services/task_service.py` | `get_task()` 剔除顶层 `tenant_id`，**保留** `payload` / `result` 等原有字段；4 处新增日志 `%s` → `{}`（Loguru 下 `%s` 不插值） |
| `package/yuxi/services/task_queue_service.py`、`package/yuxi/storage/postgres/manager.py` | 新增的私有函数下移到公开方法之后（`backend/AGENTS.md:18` 的向下规则）；**纯位置调整，零语义变更** |
| `test/unit/services/test_tasker_behavior.py` | +归属列不外泄三条：列表、详情（且保留 `payload`/`result`）、`to_dict()` 的反面证据。打在**真实** `TaskRecord` 上，不是 `FakeRecord` |
| `test/integration/storage_migration/test_migration_failure_and_replay.py` | +`migrate_shared_skills` 失败注入：失败那轮 RLS 已装好（33 张表）但 `business != 14`，重跑能完成 |

**回归（审查轮实测）**：受影响单测 **89 passed**；CI 三条 ruff 门禁**零新增**（§5）；
`get_task` 的剔除有用**变异探针**取证（删掉过滤语句后断言变红）。

**未在宿主上跑通的**：`test/unit/services/test_storage_migration.py` 因 Windows 无 `fcntl`
**无法收集**，只能在 Linux 容器里跑——**环境限制，不是回归**。
上表两条**集成**用例（迁移失败/重放）本轮**未运行**（需要会话级迁移前置与容器），只有静态依据。

### 4.2 测试

| 文件 | 改动 |
|---|---|
| `test/integration/services/test_tenant_isolation_guardrails.py` | +停写锁契约、+DDL 中途失败回滚、+池复用（commit/rollback）、+同连接跨事务无上下文；`_wait_until_blocked()` 三重收窄 |
| `test/integration/storage_migration/test_migration_failure_and_replay.py` | **新文件**：真实入口注入失败（两个失败点）+ 重放幂等 + 授权不回退 |
| `test/integration/storage_migration/test_durable_task_republish.py` | +坏消息消费链（参数化） |
| `test/integration/storage_migration/_migration_round.py` | 每轮独立日志 + `log_tail()` 多轮逐轮 |
| `test/integration/storage_migration/test_round_scaffolding.py` | +日志不被覆盖用例 |
| `test/integration/storage_migration/_durable_task_probe.py` | 长行拆分（ruff format） |
| `docs/develop-guides/version-14-worker-tenant-chain-spec.md` | §4 加第十轮证据 + 探针 P/Q；§5 重写事务边界、两条改"已验收"；§6 状态更新 + ruff 对照表 |
| `backend/test/integration/storage_migration/README.md` | +第四组（中途失败/重放）、坏消息链；脚手架改第五组 |

---

## 5. 怎么复现（隔离栈，可直接粘贴）

```bash
# 先导出本机 worktree 路径；本文件不写死绝对路径。
export YUXI_V14_WT=<你的 yuxi-v14 worktree 绝对路径>
cd "$YUXI_V14_WT"

# 1) 重建隔离测试实例（tmpfs，重建即全新库）——迁移验收这一步是必需的
MSYS_NO_PATHCONV=1 docker compose -p yuxi-v14 --profile test-db up -d --force-recreate postgres-test redis-test

# 2) 迁移验收整目录（会先把 yuxi_test 迁到 14，作为会话级前置）
MSYS_NO_PATHCONV=1 docker compose -p yuxi-v14 run --rm --no-deps \
  -e POSTGRES_DISCOVERY_PASSWORD=postgres \
  api uv run --no-sync pytest test/integration/storage_migration/ -p no:randomly -q

# 3) 租户隔离护栏
MSYS_NO_PATHCONV=1 docker compose -p yuxi-v14 run --rm --no-deps \
  -e POSTGRES_DISCOVERY_PASSWORD=postgres \
  api uv run --no-sync pytest test/integration/services/test_tenant_isolation_guardrails.py -p no:randomly -q

# 4) compose 边界（必须挂仓库根，否则 45 条会 skip）
MSYS_NO_PATHCONV=1 docker compose -p yuxi-v14 run --rm --no-deps \
  -v "$YUXI_V14_WT:/repo:ro" -e YUXI_PROJECT_ROOT=/repo \
  api uv run --no-sync pytest test/unit/config/test_docker_compose_service_boundaries.py -p no:randomly -q

# 5) ruff：CI 的门禁是**三条**命令，目录只有 backend/package，版本锁 uv.lock = 0.16.4
#    worktree 自带的 backend/.venv 里已装 ruff 0.16.4——**不必起容器**。
RUFF=backend/.venv/Scripts/ruff.exe
$RUFF check            --no-cache --config backend/pyproject.toml package
$RUFF format --check   --no-cache --config backend/pyproject.toml package
$RUFF check --select I --no-cache --config backend/pyproject.toml package
```

**注意**：若改在容器里跑，容器**没有** ruff（`dev` 组未同步），需 `uv run --with ruff==0.16.4`
临时取，且 `--no-cache` 必须放在**子命令之后**（否则 `/app/.ruff_cache` 权限拒绝）。

**ruff 口径必须写清（否则容易被读成"CI 全过"）**：CI 跑的就是上面**三条**，**目录只有**
`backend/package`，**版本 ruff 0.16.4**（与 `uv.lock` 一致）。三条都已按"**导出 HEAD 的
`backend/package`、跑同一组命令再对比**"的口径实测：

| 命令 | HEAD 基线 | 当前 | 判定 |
|---|---|---|---|
| `ruff check package` | 通过 | **通过** | 零新增 |
| `ruff format --check package` | 4 个文件不合规 | **同样 4 个** | 零新增 |
| `ruff check --select I package` | 5 处 | **同样 5 处** | 零新增 |

不合规的 4 个文件：`milvus_graph_service.py`、`evaluation_repository.py`、
`task_repository.py`、`manager.py`——**基线即如此**。

所以准确表述是"**三条门禁均无新增违规**"，**不能**写成"ruff 门禁全部通过"：
`format` 与 `--select I` 在基线上就有欠账，这里只证明**没有变坏**，没有清账。

### 回归结果（本轮实测）

| 集合 | 结果 |
|---|---|
| `storage_migration/` 整目录 | **43 passed** |
| `test_tenant_isolation_guardrails.py` | **65 passed** |
| 受影响单元集合 | **216 passed** + 1 个 **teardown ERROR（既有，非本轮）** |
| `test_docker_compose_service_boundaries.py` | **51 passed** |
| `ruff check package` | **All checks passed!** |

### 5.1 复核轮实测证据（归档，未重复跑全套）

| 项 | 结果 |
|---|---|
| Linux 迁移单测 `test/unit/services/test_storage_migration.py` | **15 passed** |
| Compose 边界单测 | **51 passed** |
| 失败注入 + 同库重放（整文件） | **4 passed** |
| 重放（**不重建容器**：新建空库 + `POSTGRES_TEST_URL` 覆盖） | **1 passed** |
| **变异探针**：把 `record_schema_version` 移回 `migrate_shared_skills()` 之前 | ✅ **变红**（`assert 14 != 14`），已还原且 sha256 逐字一致 |
| guardrails（夹具改为"建即登记、不删"） | **65 passed** |
| CI 三条 ruff | `check` 通过；`format` 4 文件 / `select I` 5 处——**与基线逐项相同，零新增** |

**未运行／未关闭**：`SECURITY INVOKER` 隐藏行 fail-open、shipping-worker 两条、发现凭据启动 E2E，
以及 §3.1 的配置域三表与其余上线条件。

**遗留物（**均未自动删除**，交人工）**：`postgres-test` 上 8 个 `yuxi_v14_*` 数据库、
`yuxi_test` 内 69 个 `tenant_guardrail_*` schema。清理语句由夹具在**创建时**追加落盘
（`YUXI_GUARDRAIL_SCHEMA_LOG`，见 §6.8），并已导出到仓库外的 `yuxi-v14-artifacts/`。

---

## 6. 陷阱与注意事项（踩过一次的都记在这里）

1. **开发库必须保持 `business=13 / knowledge=2 / rls_enabled=0`**，且**不要提交**。
   本轮结束后已复核：`business=13 / knowledge=2`、RLS 表数 **0**、无 `yuxi_worker_discovery` 角色。✅
2. **迁移验收要求"全新库"**，所以第 5 步之前必须 `--force-recreate postgres-test`；
   否则 `migrated_test_database` 会话夹具会以 `RuntimeError: 迁移验收要求全新库…` 直接把用例判 ERROR。
   "中途失败"那条例外：它自建一次性库（`DROP DATABASE ... WITH (FORCE)`），不消耗空库前置。
3. **`--select I` 与 `format` 的基线本来就不清**：`ruff format package --check` 在 HEAD 上就有 **4 个文件**
   不合规、`--select I` 有 **5 处**。**这些不是本轮引入、也不在本轮范围**（涉及未改动文件）。
   判断"是不是自己弄脏的"必须**导出 HEAD 的 `backend/package` 跑同一组命令再对比**——
   只看工作树的绝对数字会被误导。本轮自己引入过 3 处（已修掉，现为**零新增**）。
4. **`_wait_until_blocked()` 不收窄 = 假绿风险**。新增用例请务必用 `relation` / `mode` / `blocker_pid`。
5. **"全新库会先落一条遗留基线 `business=9`"**（`upgrade_agent_resource_selection()`，既有行为）。
   所以"中途失败不记录版本"的准确口径是 **`business != 14`**，不是"一条都没记"。
6. **`@pytest.mark.asyncio` + `asyncio_mode=auto` 的 teardown 隐患**：
   `test_run_worker.py` 有 1 条用例的 teardown 会因残留 psycopg 异步池阻塞约 60s 报 ERROR
   （断言本身通过）。**HEAD 基线同样复现、同样 64.16s**，非本轮引入。跑这个文件时请带 `--timeout`。
   **更新**：并入主树的连接池修复后，**本次回归未复现**该 ERROR（合并前 `52 passed + 1 ERROR`、
   合并后 `56 passed / 0 ERROR`）。仍建议带 `--timeout`——这是"未复现"，不是"已根治"。
7. **口径纪律**：本分支**有意去掉**隐式默认租户兜底（`explicit_tenant_id()` 不回落）。
   凡是在主树"能过"的夹具用例，在这里可能因 RLS 拒绝而红——那是**预期**，不是回归。
8. **隔离 schema 与一次性测试库会累积，且**不会**被自动删除**：guardrails 每跑一轮多约 65 个
   `tenant_guardrail_*` schema；迁移失败注入每跑一轮多 1–3 个 `yuxi_v14_*` 数据库。
   这是"禁止自动 DROP"的**固有代价**，不是 bug。清理语句**在创建时就追加落盘**，不依赖会话收尾：
   - **schema**：`YUXI_GUARDRAIL_SCHEMA_LOG`（默认 `/tmp/tenant_guardrail_schemas_to_clean.sql`）。
     要带出容器就给 `docker compose run` 加 `-v "<host 目录>:/artifacts:rw"`，并把该变量指到
     `/artifacts/…`；不加 `-s` 时 pytest 会捕获 print，但**会有一条 warning 摘要**保证可见。
   - **数据库**：用例收尾打印库名与 `DROP DATABASE` 语句（`[part2] 人工清理语句：…`）。
   **清理一律人工执行**、确认无用后再跑；测试不代劳。

---

## 7. 建议的下一步

1. **独立审查必须覆盖完整范围，不是"审三处即可提交"。** 审查输入至少包含：
   **固定基线 `659da46`**、**全部 tracked diff**（37 文件 / +4373 −167）、
   **所有拟交付的未跟踪新文件**（7 个，含 `config/multi_tenant.py`、
   `server/utils/tenant_context_middleware.py`）、以及当前 spec。
   worker / 权限 / 迁移 / 认证上下文都有**实质行为变化**——
   §4 的"本轮改动清单"**不能替代**完整范围（它只描述第十轮增量，不代表工作树全貌）。
   审查通过前**不提交**。
2. 关闭既有的 **`ruff format` / `--select I`** 基线欠账（9 处，单独一轮，别混进本分支）。
3. 单独立项 **`SECURITY INVOKER` 隐藏行**——按 §3，它是**版本 14 可部署升级的阻断项**，
   不是可延后的待办。
4. 补 shipping-worker 两条用例的**显式租户上下文 + 常驻 worker 前置**，以及发现凭据缺失时
   **真实 worker 起不来**的端到端证据。
5. 上述完成后，再谈"多租户可上线"——注意 §5 的边界：**agent-run / 定时任务链尚未多租户化**，
   认证 Claim 也未接入（多租户模式下业务请求 503）。
