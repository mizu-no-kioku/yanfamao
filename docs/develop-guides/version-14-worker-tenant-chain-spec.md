# 版本 14：worker 租户整链与默认租户契约（第 ② 步）

**准确状态**：**单租户兼容入口、Durable Task 租户处理、发现权限边界与 RLS 均已在迁移入口接线；
未完成的是 agent-run / 定时任务链的多租户化与认证 Claim 接入（见 §5 边界）。** 未提交、未迁移开发库。

适用范围：通用 **Durable Task** 链（`tasks`）。**不含** agent-run / 定时任务链（见 §5 边界）。

## 1. 要解决的两个问题

1. **默认租户兜底与"缺上下文拒绝"冲突**：`current_tenant_id()` 未设置时回落租户 1，于是运行时
   数据库会话**永远**带着上下文——"忘了建立上下文"与"以租户 1 运行"不可区分。
2. **worker 完全没有租户接线**：`process_task` / `publish_pending_tasks` / 失联收敛 / 周期补发
   全程走 `get_async_session_context()` 的默认租户 1。RLS 一旦启用，**非租户 1 的任务不可见** →
   `get_by_id` 返回 None → 被**静默跳过**（既不执行、也不失败）。属多租户**可用性**缺陷。

## 2. 租户上下文契约（去默认兜底）

**两个访问器，别混用**（`tenant_context.py`）：

| 访问器 | 语义 | 使用者 |
|---|---|---|
| `explicit_tenant_id()` | **显式**设置的租户；未设置返回 `None`，**不回落** | 运行时数据库会话（`_apply_tenant_context`） |
| `current_tenant_id()` | 未设置回落默认租户 | ORM **列默认值**（建表 / SQLite 单测 / 迁移） |

**运行时注入 fail-closed**：`_apply_tenant_context` 用 `explicit_tenant_id()`；为 `None` 时
**跳过** `set_config('app.tenant_id', ...)` → 会话的 `app.tenant_id` 保持未设置 → RLS 按"无租户"
处理（读 0 行、写被 `WITH CHECK` 拒）。这样"缺上下文"不再静默变成"租户 1"。

**列默认值逐表判断（不机械删除）**：保留 Python 侧 `default=current_tenant_id` 与
`server_default=DEFAULT_TENANT_ID`。理由：它们只服务"建表 / SQLite 单测 / 迁移"这类没有会话
上下文的插入路径；**在运行时路径上不构成绕过**——缺上下文时注入被跳过，`WITH CHECK` 必然拒绝
该写入，默认值根本落不了库（数据库层证据见护栏套件
`test_without_a_tenant_context_writes_are_rejected` 与 `..._the_parent_row_itself_is_invisible`）。

**显式建立租户的可信入口**（回落被去掉后，这些入口是唯一的上下文来源）：

| 入口 | 位置 | 当前租户来源 |
|---|---|---|
| API 请求 | `server/utils/tenant_context_middleware.py`（`main.py` 最外层） | 单租户/遗留部署显式建立默认租户；多租户部署（`YUXI_MULTI_TENANT`）缺 Claim 解析时业务请求 **503**，不默认。**免租户端点**（`/api/system/health`、`/api/system/ready`、`/api/system/discovery`、`/docs`、`/redoc`、`/openapi.json`）放行，并**显式清除**上下文（`clear_tenant_context()`，不是"不设置"——外层若已设过租户，不设置会**继承**它）。 |
| API 启动全局初始化 | `server/utils/lifespan.py` `_startup` | 显式默认租户（内置 Skills/Agents/供应商/选项） |
| worker 启动全局初始化 | `run_worker.py` `_worker_startup` | 显式默认租户（同上，另含未逐租户化的 agent-run/定时收敛） |
| worker 消费任务 | `task_service.process_task` | **消息携带的路由提示**（仅用于建立上下文），归属以持久化记录核对为准（见 §3） |

> **开关解析是 fail-closed 的**（单一事实源 `yuxi/config/multi_tenant.py`，API 与 worker 共用）：
> 只接受真值（`1/true/yes/on`）、假值（`0/false/no/off`）、缺省或空值（=关闭）；**非空未知值**
> （如拼错的 `ture`）在启动时抛错。把未知值当成"关闭"会让以为开了多租户的部署静默地以默认租户
> 运行——与"缺上下文不得视为租户 1"是同一条原则。
>
> 中间件在依赖与端点之前执行，所以 `get_current_user` 读 `users`（认证引导需要读按租户隔离的
> 凭证）时上下文已就位。

## 3. Durable Task 整链

决策记录（`2026-10-10-multi-tenant-isolation.md` §异步与缓存）：
**payload 显式携带 `tenant_id`，但 worker 不信任 payload——消费时回查持久化记录校验一致。**

| 环节 | 位置 | 契约 |
|---|---|---|
| **任务发现** | `task_queue_service.known_tenant_ids()` | 以 **`tenants` 注册表**为锚点（它**不是归属根、没有 RLS**，运行时可读）。归属根表在 RLS 下按当前租户过滤，必须先知道租户才能逐个进入其上下文。**这是全库唯一的跨租户读，只暴露租户 id**（见 §5 的权限边界状态）。 |
| **发布** | `publish_task(task_id, tenant_id)` | ARQ payload 携带 `tenant_id`。 |
| **可信租户解析 + 消费** | `process_task(ctx, task_id, tenant_id)` | payload 里的租户只是**待校验的路由提示**——先用它建立事务上下文（RLS 据此决定可见性），**再以受 RLS 过滤的持久化记录核对**归属。**缺 `tenant_id`**：不猜、不处理，留给周期补发。**payload 租户与持久化归属不一致**：在 payload 租户下回查不到该行（RLS 生效时本就读不到）→ 不处理；RLS 未生效（误配）时由显式比较兜住 → **不在错误租户下执行**。两种情况任务都仍是 pending，周期补发会用**持久化记录里的租户**重新发布，从而正确恢复。 |
| **执行** | `_process_claimed_task` | claim / heartbeat / finish 全在 `use_tenant(tenant_id)` 内；`asyncio.create_task` 复制上下文，故 handler 与 heartbeat 任务继承同一租户。 |
| **失败收尾** | `finalize_task_failure` | 在失败事务内执行，事务已带租户上下文。 |
| **周期恢复** | `reconcile_and_publish_tasks()` | **逐租户**进入上下文后收敛失联 owner、prune 终态、补发 pending；不在单个租户上下文里做一次全局扫描（会漏其他租户）。 |
| **补发失败隔离** | `publish_pending_tasks()` | **单任务**发布失败：保留 pending、记 `task_id`/`tenant_id` + 异常堆栈（Loguru `{}` + `opt(exception=...)`）、继续处理同租户下一个与其他租户。**整轮全失败**（有任务要发却一条都没入队）→ 抛 `TaskPublicationUnavailableError`。 |
| **整轮失败的归因** | `TaskPublicationUnavailableError` | 文案只说"**全部发布尝试失败**"，**不**断言"系统级故障"；`queue_unavailable_suspected` 只在**失败全部落在已知连接类异常**（内建 `ConnectionError` / `TimeoutError` + redis `ConnectionError` / `TimeoutError`；**不含**宽泛的 `OSError`，也**不含** redis `ResponseError`）时为真，且名字里带 `suspected`——它是**诊断提示**，不是根因结论，**不得**据此自动采取破坏性恢复动作；原始异常通过 `__cause__`（`raise ... from`）保留类型与堆栈。 |

**"全部失败"的后果链（已核对）**：启动期首轮补发**不吞异常** → 抛错则 worker 起不来，且
`_publish_task_reconciliation_health()` 不执行 → 健康键**从未写入** → readiness 的 worker 探针
立即失败（启动路径没有宽限窗口）。周期循环里健康续租放在补发**成功之后**，失败即**不续租**——
但**这是健康宽限契约，不是"失败即立即撤销就绪"**：上一次成功写入的健康键仍带原有 TTL，readiness
会**继续保持就绪直到旧 TTL 到期**（最长 `TASK_RECONCILIATION_HEALTH_TTL_SECONDS`，默认 65 秒），
到期后才撤销就绪。

**"先用消息租户建立上下文、再以持久化记录核对"是否成立**：成立，但**不能**说成"先从持久化记录
解析出租户"——消息里的租户是路由提示，持久化记录是**校验依据**。RLS 生效时"看得见"即蕴含"归属
相符"，比较是冗余的；但 RLS 未生效（`POSTGRES_APP_URL` 未配置、误配受限角色）时，它是唯一的
兜底——把"payload 声明的租户"钉回"持久化归属"，避免在错误租户下执行。

## 4. 证据（隔离环境实测）

| 断言 | 用例 | 层级 |
|---|---|---|
| 缺上下文 `explicit_tenant_id()` 返回 None、不回落 | `test_explicit_tenant_is_unset_by_default_and_never_falls_back` | 单元 |
| 缺上下文**不注入** `app.tenant_id`、也不回调 | `test_apply_tenant_context_skips_injection_without_an_explicit_tenant` | 单元 |
| 有显式上下文时注入并回调 | `test_apply_tenant_context_injects_the_explicit_tenant` | 单元 |
| 缺 tenant_id 的 ARQ 消息**不执行**、不猜租户 | `test_process_task_without_tenant_context_does_not_execute` | 单元 |
| payload 租户与持久化归属不一致时**不执行** | `test_process_task_rejects_a_payload_tenant_that_disagrees_with_persistence` | 单元 |
| 执行期间租户上下文 = payload 租户、结束后复原 | `test_process_task_runs_under_the_persisted_tenant` | 单元 |
| 发布携带 `(task_id, tenant_id)` | `test_arq_publication_uses_fresh_messages_instead_of_stale_job_lock` | 单元 |
| **非默认租户**的 pending 任务也被发现并携带自己的租户 | `test_publish_pending_tasks_discovers_and_carries_non_default_tenants` | 集成（真 PG） |
| 多租户开关：真值/假值/空值/拼错值（拼错抛错） | `test_switch_accepts_truthy_values` / `..._falsy_...` / `..._blank_...` / `..._rejects_unknown_...` | 单元 |
| 多租户模式：业务请求 503、免租户端点放行且不带上下文 | `test_multi_tenant_mode_blocks_business_requests` / `..._lets_infrastructure_endpoints_through` | 单元 |
| 单租户模式：请求建立默认租户上下文 | `test_single_tenant_mode_establishes_the_default_tenant` | 单元 |
| 跨租户发现只暴露租户 id（SQL 边界，**语句层**） | `test_the_cross_tenant_discovery_query_exposes_only_tenant_ids` | 单元 |
| 免租户端点在**外层已有租户上下文**时仍确实无上下文 | `test_tenant_free_endpoint_clears_an_inherited_tenant_context` | 单元 |
| **发布失败日志真的输出** task_id / tenant_id / 堆栈 | `test_publication_failure_log_carries_the_ids_and_the_traceback` / `..._does_not_leak_the_literal_placeholder` | 单元（捕获真实日志文本） |
| 队列连接类 vs 程序错误的归因分类（**含 `PermissionError` / `FileNotFoundError` / redis `ResponseError` 反例**） | `test_builtin_connection_errors_count_as_queue_unavailable` / `test_non_connection_errors_are_not_classified_as_queue_unavailable` / `test_programming_errors_are_not_classified_as_queue_unavailable` / `test_mixed_failures_are_reported_as_possibly_programming_errors` | 单元 |
| 原始异常保留在 `__cause__`，且只报"疑似" | `test_publication_failure_for_every_task_reports_the_round_failed` | 集成（真 PG） |
| 多租户模式下 readiness **不报就绪**（防假就绪信号） | `test_multi_tenant_without_claim_resolution_is_not_ready` | 单元 |
| Postgres 就绪探针不读任何业务表（`SELECT 1`） | `test_postgres_readiness_probe_does_not_read_business_tables` | 单元 |
| **运行时角色对 `tenants` 完全无权限**（表级 + 列级，`SELECT` 亦无） | `test_the_runtime_role_has_no_access_to_tenants_at_all` | 集成（真 PG，受限角色） |
| 运行时角色在 `tenants` 上**没有任何授权行**（`table_privileges` + `column_privileges`） | `test_the_runtime_role_has_no_grant_rows_on_tenants` | 集成（真 PG） |
| 运行时角色**不能** INSERT / UPDATE / DELETE `tenants`，且数据未被改动 | `test_the_runtime_role_cannot_create_update_or_delete_tenants` | 集成（真 PG，受限角色） |
| **发现角色能读租户 id**（正向） | `test_the_discovery_role_can_read_tenant_ids` | 集成（真 PG，发现角色） |
| 发现角色授权清单**恰好**只有 `id` 一列 SELECT | `test_the_discovery_role_grant_inventory_is_exactly_the_id_column` | 集成（真 PG） |
| 发现角色**不能**读其他列、不能读业务表 | `test_the_discovery_role_cannot_read_other_columns_or_business_tables` | 集成（真 PG，发现角色） |
| 发现边界可重放（幂等） | `test_the_discovery_boundary_is_idempotent_and_survives_a_replay` | 集成（真 PG） |
| **重放 blanket 授权后按正确顺序再跑边界 → `tenants` 仍不可达** | `test_replaying_the_blanket_grant_does_not_leave_tenants_reachable` | 集成（真 PG） |
| **顺序依赖的反向对照**：先跑边界再重放 blanket → 权限被放宽（故边界必须最后跑） | `test_the_boundary_must_run_after_the_blanket_grant` | 集成（真 PG） |
| **迁移前预检**：一致数据放行 / 既有跨库子行被点名拦截 | `test_the_precheck_passes_on_consistent_data` / `test_the_precheck_flags_existing_cross_kb_children` | 集成（真 PG） |
| **发现凭据启动护栏**：缺 URL 直接失败（fail-closed，不回落 owner） | `test_discovery_guard_rejects_a_missing_credential` | 集成（真 PG） |
| **发现凭据启动护栏**：连接落在 superuser / 角色名不符 → 拒绝 | `test_discovery_guard_rejects_a_privileged_discovery_role` | 集成（真 PG） |
| **发现凭据启动护栏**：配置正确的 `yuxi_worker_discovery` 连接 → 通过 | `test_discovery_guard_accepts_the_configured_discovery_role` | 集成（真 PG，发现角色） |
| **前一个租户发布失败，后一个租户仍被发布**（失败隔离） | `test_one_tenants_publication_failure_does_not_block_another_tenant` | 集成（真 PG） |
| 整轮全失败 → 显式报告本轮失败 | `test_publication_failure_for_every_task_reports_the_round_failed` | 集成（真 PG） |
| 数据库层：无上下文读 0 行、写被拒 | `test_without_a_tenant_context_writes_are_rejected` / `..._invisible` | 集成（真 PG，受限角色） |

**隔离栈实测（原始命令结果，不改写口径）**：

- **较早一次全量单测**（`test/unit -m "not slow"`，手工 `docker run` **缺** `skill-projections`
  可写挂载、**未挂载** `server`）：**2600 passed、1 failed、58 skipped**。唯一失败是
  `test_sandbox_backends` 的 PermissionError，属**环境缺挂载**，不是代码回归。
- **补齐环境后的相关集合**（只重跑该文件，加 `--tmpfs /app/skill-projections`）：**93 passed**。
- **最终受影响集合**（tenant-context + tasker + task_queue_config + durable_task_repository +
  guardrails）：**143 passed**。
- **完整单测套件**（挂载 `server`/`scripts`/`sandbox_provisioner` + `--tmpfs /app/skill-projections`）：
  **2632 passed、58 skipped、0 failed**。

- **第六轮受影响集合**（publication-logging + readiness + middleware + task_queue_config +
  tenant-context + tasker + system_router + durable_task_repository + guardrails）：**173 passed**。
- **第六轮完整单测套件**（同上挂载）：**2646 passed、58 skipped、0 failed**。
- **第七轮受影响集合**（同上一组 9 个文件）：**179 passed**（+3 tenants 写权限集成用例、
  +3 队列归因负向单测）。
- **第七轮完整单测套件**（同上挂载 + `--tmpfs /app/skill-projections`）：**2701 passed、
  58 skipped、0 failed**。

> 口径纪律：全量命令的实际结果**如实记录**——较早那次是 **2600 passed、1 failed、58 skipped**；
> 补全环境后完整套件是 **2632 passed、58 skipped、0 failed**；第六轮为 **2646 passed、58 skipped、
> 0 failed**；第七轮为 **2701 passed、58 skipped、0 failed**。局部复验**足以支持**"环境原因"的
> 判断，但**不能**用来把较早那次命令的结果改写成"全绿"。另需记名：较早那次未挂载 `server`，
> 所以 `test/unit/server/*` 跑的是镜像内烘焙的 `server` 副本，不是 worktree 版本——补挂载后已修正。
>
> **第七轮发现并修掉一处与本次改动无关的既有红灯**：`test/unit/services/test_run_worker.py` 的
> `test_worker_startup_ensures_builtin_mcp_servers` 与 `test_worker_startup_fails_when_system_options_cannot_initialize`
> 会 `AttributeError: 'NoneType' object has no attribute 'connect'`。**证明为既有缺陷、非本次引入**：
> 把 `run_worker.py` + `manager.py` **还原成 `HEAD` 版本**后，两条用例以**完全相同**的堆栈失败
> （`run_worker.py:1565 → manager.py:1976`）；在**主树 `docs/multi-tenant-isolation` 分支**上同样
> 失败。根因是**测试隔离缺口**：两条用例打桩了 `require_current_schema`，却没打桩同一启动路径上
> 紧邻的 `require_restricted_runtime_role()`（真库调用）。修法是**补桩**（不弱化断言），并把
> `require_restricted_runtime_role` 加进首条用例的**调用顺序**断言——用例因此**更强**。角色是否
> 受限由 `test/integration/services/test_tenant_runtime_role.py` 在真库上验收，未受影响。
> **同时纠正口径**：第五/六轮报告里的"完整单测 0 failed"未覆盖该文件（第七轮计入后其红灯才暴露）。

**变异探针（第六轮，全部红在业务断言，已还原、`grep PROBE-[A-Z]` 为空）**：

| 探针 | 改动 | 变红 |
|---|---|---|
| G | `_log_publication_failure` 改回 `%s` + `exc_info=True` | 日志文本里**字面**出现 `task_id=%s`、无 ID、无堆栈（2 条断言） |
| H | 去掉 readiness 的多租户诚实检查 | `assert 'ready' == 'not_ready'`（假就绪信号复现） |
| I | 免租户端点改回"不设置"（不显式清除） | `assert 42 is None`（外层租户上下文泄漏进探活端点） |
| F | 发现边界只加列授权、不 `REVOKE` | 3 条：`SELECT * FROM tenants` 仍 `committed`；授权清单含全部列 |

**变异探针（第七轮，同样红在业务断言，已还原、`grep PROBE-[A-Z]` 为空）**：

| 探针 | 改动 | 变红 |
|---|---|---|
| J | `TENANT_DISCOVERY_BOUNDARY_STATEMENTS` 退回"只 `REVOKE SELECT`"（第六轮写法） | 2 条：受限角色 `INSERT/UPDATE/DELETE tenants` 变 `committed`（`assert ... startswith("rejected")` 失败） |
| K | `QUEUE_UNAVAILABLE_ERRORS` 退回宽口径 `(OSError, ...)` | 3 条：`PermissionError` / `FileNotFoundError` / `IsADirectoryError` 被误判为"队列不可用"（`assert True is False`） |

第七轮 ruff `All checks passed`。

**方案 (b)（worker 独立发现凭据）落地证据——隔离环境实测（原始命令结果，不改写口径）**：

- **guardrails 套件单跑**（`test_tenant_isolation_guardrails.py`，真 PG 受限角色）：**61 passed**。
  含 9 条 `tenants` 权限契约用例（运行时角色无权限 / 发现角色只读 id / 授权清单 / 重放 / 顺序反向对照）
  与 2 条迁移前预检用例。
- **受影响单元集合**（9 个文件：run_worker + storage_migration + task_queue_config + tasker +
  readiness + task_queue_publication_logging + postgres_manager_schema + postgres_tenant_context +
  tenant_context_middleware）：**193 passed**。
- **受影响集成集合**（5 个文件：guardrails + durable_task_repository + durable_task_worker_path +
  schema_migration_version + tenant_backfill，收集 113 条）：**111 passed、2 failed**。
  两条失败**均为环境性**（见下），非本次改动引入。
- **运行时角色 / 发现凭据启动护栏**（`test_tenant_runtime_role.py`，收集 5 条）：**4 passed、1 skipped**
  （skip 的是"配置了 `POSTGRES_APP_URL` 才跑"的运行时角色正向用例）。
- **开发库保持原状**（未迁移）：`business=13 / knowledge=2 / rls_enabled=0`，且
  `yuxi_worker_discovery` **不存在**于开发库——方案 (b) 只在隔离环境实现。
- **ruff**：`All checks passed!`。（注：`ruff format --check` 对 `manager.py` 与
  `test_tenant_isolation_guardrails.py` 报"would be reformatted"——**在 `HEAD` 上同样如此**，
  是**既有格式漂移**，非本次引入；CI 的门是 `ruff check`。）

**两条集成失败的归因（环境性，已证明）**：`test_durable_task_worker_path.py` 的两条"shipping
worker"用例（`test_shipping_worker_periodic_reconciliation_recovers_pending_publication`、
`test_shipping_worker_failure_runs_domain_hook`）**依赖一个正在运行的 worker 容器**做周期收敛；
本轮按纪律**保持开发栈停止**（`yuxi-main-worker-1` 已 `Exited (0)`），没有任何 worker 在跑，所以
不可能收敛。**证明为环境性、非本次引入**：把这两条用例在**主树 `docs/multi-tenant-isolation`
分支（未含本次任何改动）**上、对同一个库重跑，得到**同样的两条失败**（其中一条同样报
"did not converge through the shipping worker"）。该限制本就写在
`test/integration/storage_migration/README.md`（"依赖共享 worker 容器，不是独立、完全隔离的测试"）。
真正的版本 14 收敛证据是 `storage_migration/test_durable_task_republish.py`——它**自己拉起 worker
子进程**，不依赖常驻容器。

**变异探针（第八轮 / 方案 (b)，同样红在业务断言，已还原、`grep PROBE-[A-Z]` 为空）**：

| 探针 | 改动 | 变红 |
|---|---|---|
| L | `TENANT_DISCOVERY_BOUNDARY_STATEMENTS` 退回"只 `REVOKE SELECT`、把列级读授权给运行时角色"（不区分消费者） | **6/7 条**权限契约用例红：运行时角色 `SELECT id FROM tenants` 变 `committed`、授权清单非空、发现角色读不到 id、授权清单为空、运行时角色写权限仍在、重放后 `tenants` 仍可达 |
| M | `precheck_tenant_reference_consistency()` 改成 no-op | 1 条：`test_the_precheck_flags_existing_cross_kb_children` → `DID NOT RAISE RuntimeError` |

> 探针 L 里第 7 条（`test_the_discovery_role_cannot_read_other_columns_or_business_tables`）**保持绿**：
> 它是**负向**断言，在"发现角色一条授权都没有"的退化状态下**真空成立**——负向用例不会因
> "权限被收得更死"而变红，这不是漏检。

**第九轮（审查后补强）证据——隔离环境实测**：

- **发现角色护栏加强**（只核对角色名 / superuser 不足）：`require_discovery_credential()` 现在核对
  角色名、`superuser`、`BYPASSRLS`、经 `pg_has_role` 判定的**继承**基表所有权、是否为
  superuser/BYPASSRLS 角色的**成员**、**有效权限**（当前 schema 下只允许 `tenants(id)` 的列级
  `SELECT`；用 `has_table_privilege` / `has_column_privilege` 判定，能抓 `PUBLIC` 授予与继承），
  以及**发现连接与运行时连接同库**（比对 `host:port/database`）。
  `test_tenant_runtime_role.py`：**8 passed / 0 skipped**。
- **API 不持有发现凭据——部署层核对发现真实泄漏并已修**：核对渲染后的 Compose 配置发现，
  `.env` 里的 `POSTGRES_DISCOVERY_PASSWORD` 经 `env_file: .env` 被**注入到 api 容器**（文档写
  "API 不持有"并不等于部署做到）。已修：凭据移到独立的 `.env.discovery`，**只由 worker 与
  storage-migrator 的 env_file 加载**；dev 与 prod 两个 compose 都改。修后渲染结果：发现变量
  **只出现在 `worker` 与 `storage-migrator`，api 一个都没有**。新增 4 条 Compose 边界用例
  （含 3 条负控）+ 1 条"API 启动路径不引用发现"的源码级用例；`test_docker_compose_service_boundaries.py`
  **50 passed**；另加 1 条 `known_tenant_ids()` 无凭据时 fail-closed 的单测。
- **`1 skipped` 已指名并消除**：跳过的是 `test_guard_accepts_the_configured_runtime_role`
  （需要 `POSTGRES_APP_URL`）。补齐该变量后 **8 passed / 0 skipped**。
- **两条 shipping-worker 用例的准确归因（纠正上一版"纯环境性"的说法）**：在**版本 14**
  （`business=14`、RLS 已启用 33 张表）的测试实例上，这两条用例**在自身 setup 阶段就失败**：
  `InsufficientPrivilegeError: new row violates row-level security policy for table "knowledge_bases"`
  ——它们经运行时角色 `yuxi_app` 写夹具数据却**没有建立租户上下文**。**对照实验**：把**主树未改动的
  HEAD** 用同一实例、同一变量重跑同一条用例 → **PASSED**。根因**不是环境**，而是**本分支有意去掉的
  隐式默认租户兜底**：主树的 `_apply_tenant_context` 走 `current_tenant_id()`（回落
  `DEFAULT_TENANT_ID=1`）→ 静默注入租户 1；本分支改成 `explicit_tenant_id()`（**不回落**）→ 无上下文
  即不注入 → RLS 拒绝。所以这两条用例**尚未版本 14 就绪**，属**待办**，不是回归。

**变异探针（第九轮，同样红在业务断言，已还原、`grep PROBE-[A-Z]` 为空）**：

| 探针 | 改动 | 变红 |
|---|---|---|
| N | 关掉 `require_discovery_credential()` 的**有效权限**断言（表级 / 列级） | 2 条：多授业务表 SELECT、多授 `tenants.name` SELECT → `DID NOT RAISE RuntimeError` |

**第十轮（隔离环境验收补齐）证据——隔离环境实测**：

这一轮把第九轮遗留的"未验收"逐条做成**真实验收**（判据都在隔离栈 `-p yuxi-v14` 的测试库
`yuxi_test` 上取，且测试实例先 `--force-recreate` 得到全新库）：

- **迁移停写／锁定契约（第九轮审查提出）**：`install_tenant_reference_consistency()` 改为
  **同一事务内** `ACCESS EXCLUSIVE` 停写 + 存量预检 + 触发器 DDL，`SET LOCAL lock_timeout = '10s'`；
  预检加 `SET LOCAL row_security = off`。用例
  `test_the_install_holds_a_stop_write_lock_across_precheck_and_install` 在"取锁后、装触发器前"暂停，
  另一连接的写入必须被挡住；`test_install_failure_rolls_back_trigger_ddl_and_releases_write_locks`
  证明 DDL 中途失败整笔回滚、无残留触发器、锁释放、重放成功。
  > **为什么不满足于"停写锁"这个名字**：把 `LOCK TABLE` 去掉时，断言必须变红——已用探针 P 实测
  > （见下表）。判据本身也收窄了：从"该后端有任意未授予的锁"改成**表名 + 锁模式 + 阻塞者**三者都对上，
  > 否则任何无关的锁等待都能让断言变绿。
- **真连接池上的无上下文拒绝**：`test_runtime_pool_checkout_clears_tenant_after_commit_and_rollback`
  用真实 `yuxi_app` 登录、`pool_size=1, max_overflow=0`、装**生产同一份** `after_begin` 钩子，
  在**同一后端 PID** 上验 commit 与 rollback 两条路径；另有
  `test_a_reused_connection_without_context_reads_nothing_and_rejects_writes` 直接在同一连接上
  跨两个事务复现"无上下文读 0 行、写被拒"。
- **坏消息消费 → 补发完成**：`test_bad_message_is_consumed_without_execution_then_periodic_republish_completes`
  （参数化消息租户 `[None, 1]`、持久化租户 2）。用 `LOCK TABLE tenants IN ACCESS EXCLUSIVE MODE`
  让补发**发不出去**，再直接向 ARQ 投递坏参数消息；断言坏消息 job 被消费且 `success`、任务仍
  `pending` / `attempt_count == 0` / `worker_id is None`，解锁后收敛 `success` / `attempt_count == 1`
  / 数据集 `completed`，且 `pool.all_job_results()` 里补发参数是 `(task_id, 2)`。
- **迁移中途失败与重放**：`test_migration_failure_and_replay.py` 用**真实入口**注入失败
  （`ensure_tenant_rls` / `ensure_tenant_discovery_boundary` 两处），断言不记录 `business=14`；
  后者对应 **RLS 已提交 33 张表**的**部分迁移**形态，重跑同一入口完成，并核对授权未回退。
- **每轮日志不被覆盖（审查提出 `_migration_round.py:221`）**：`run_full_migration()` 每轮分到
  **全新**日志文件（`migration.log` / `migration.run2.log`），`log_tail()` 多轮时**逐轮**给出；
  `test_a_second_run_does_not_overwrite_the_first_runs_log` 钉住。

**变异探针（第十轮，红在业务断言，已还原、`grep PROBE-[A-Z]` 为空）**：

| 探针 | 改动 | 变红 |
|---|---|---|
| P | 注释掉 `install_tenant_reference_consistency()` 里的 `LOCK TABLE ... ACCESS EXCLUSIVE` | 1 条：停写锁用例 → `blocked` 为 `False`（写入没被挡住） |
| Q | 让 `MigrationRound.next_log_path()` 复用同一个日志文件 | 1 条：`test_a_second_run_does_not_overwrite_the_first_runs_log` → 第一轮失败日志被截断 |

## 5. 边界（不承诺，不得读成"多租户已可用"）

- **仅 Durable Task 链部分完成**：agent-run / 定时任务链（`_reconcile_agent_run_leases_forever`、
  `recover_pending_dispatches`、`claim_and_dispatch_due_jobs`、`process_agent_run` 等）仍以**显式
  默认租户**运行。所以本步是"**Durable Task 链路**完成"，**不是**"全部 worker 多租户化"。多租户
  上线前必须补这条链，否则非默认租户的 Run / 定时任务不会被收敛。
- **认证 Claim 未接入**：请求租户目前是"显式默认租户"；多租户部署打开 `YUXI_MULTI_TENANT` 时业务
  请求 **503**（免租户端点仍放行），**不是**已支持多租户。API Key 的"尚不知道租户却要读凭证"引导
  问题仍未解（决策记录指向 `SECURITY DEFINER` 最小函数或受限 `BYPASSRLS` 连接，属**安全决策**，
  本轮不做）。
- **跨租户发现的权限边界：已按消费者区分（决策 (b)），并已接入迁移入口。** 准确说法分三层：
  1. **接口已集中**：`known_tenant_ids()` 是全库唯一的跨租户读。
  2. **权限边界语句已实现并接入迁移入口**（`TENANT_DISCOVERY_BOUNDARY_STATEMENTS`，在版本 14
     装配里是**最后**一批）：`yuxi_app` 对 `tenants` **完全无权限**（表级与列级都 `REVOKE ALL`，
     `REVOKE ALL PRIVILEGES` 会同时清掉 `relacl` 与 `attacl`，故重放安全）；专用发现角色
     `yuxi_worker_discovery` 只有列级 `SELECT (id) ON tenants`（禁写、禁读其他列）。
  3. **消费者已真正区分**（**已批准的决策 (b)**）：worker 用**独立连接串** `POSTGRES_DISCOVERY_URL`
     登录 `yuxi_worker_discovery` 做发现；**API 不持有该凭据**。所以普通业务请求（`yuxi_app`）
     **无法枚举租户 id**，只有 worker 能。`SECURITY DEFINER` 函数若把 `EXECUTE` 授给 `yuxi_app`，
     暴露面与列级授权**相同**、达不到区分效果，故**未采用**。

  **执行顺序**：边界必须在 blanket 授权**之后**（版本 14 装配里是**最后**一批）。语句幂等，
  但**顺序不幂等**：先跑边界、再重放 blanket 授权会把写权限放回来（反向对照用例已钉住）。

  **决策记录（`租户 ID 枚举契约`）——已批准 = 方案 (b)**：
  - **(a) 批准"所有运行时消费者可读租户 ID"**：接受 `yuxi_app` 可枚举租户 id，边界到此为止。
    —— **未采用**。
  - **(b) 给 worker 独立数据库凭据**（**已采用**）：新增 `yuxi_worker_discovery`
    （仅 `SELECT (id) ON tenants`），`yuxi_app` 对 `tenants` **完全无权限**；部署层新增
    `POSTGRES_DISCOVERY_URL` / `POSTGRES_DISCOVERY_PASSWORD`（**worker 与 storage-migrator 持有，
    API 不持有**），并有启动校验 `require_discovery_credential()`（缺 URL 或角色名不符 /
    是 superuser / 有 BYPASSRLS → 启动失败，fail-closed）。
  - **启动校验的完整条件**（第九轮加强，只核对角色名 / superuser 不足）：角色名、
    `rolsuper`、`rolbypassrls`、经 `pg_has_role` 判定的**继承**基表所有权、是否为
    superuser/BYPASSRLS 角色的**成员**、**有效权限**（当前 schema 下只允许 `tenants(id)` 的列级
    `SELECT`——不得有表级读、不得有写、不得触及业务表），以及**发现连接与运行时连接同库**。
  - **部署层的"API 不持有"已核对（并修掉一处真实泄漏）**：`env_file` 会把文件**内容**注入容器，
    所以凭据放在共享 `.env` 里就等于给了 api。现改为独立 `.env.discovery`，**只由 worker 与
    storage-migrator 加载**；dev/prod 两个 compose 一致。渲染核对：发现变量只出现在
    `worker` / `storage-migrator`。API 的启动路径（`server/utils/lifespan.py`）**不引用**发现凭据。
  - **批准口径（用户原文）**：普通角色撤销 `tenants` 写权限，并补**正向、越权及授权重放**测试；
    **仅在隔离环境实现，不迁移开发库**。任务执行仍使用受 RLS 约束的角色和明确租户上下文。

- **版本 14 的"实际接线位置与事务边界"（第十轮更新，避免把"README 改成 14"误读成迁移完成）**：
  接线在 `backend/package/yuxi/storage_migration.py`（`business_version != 14` 分支）。**会写库的步骤
  全部排在版本记录之前**——`record_schema_version("business", 14)` 之后只剩文件侧迁移：
  `ensure_tenant_schema()` → `ensure_tenant_rls()` → `install_tenant_reference_consistency()` →
  `ensure_tenant_root_immutability()` → `ensure_tenant_discovery_boundary()` →
  `_converge_database_state()` → `migrate_shared_skills()`（**会改库并 commit**）→
  `record_schema_version("business", 14)`（任一步失败都不写 14，重跑可续）→
  `mark_v071_skills_migrated()` / `migrate_runtime_storage_identity()`（文件侧，各有自己的标记）。
  **版本号只认证数据库变换**：文件侧迁移失败不回滚版本，也不需要版本号证明。两层契约分开读。
  **事务边界分两层，别混读**：整个迁移**没有**一个包住全部步骤的事务（每步各自
  `async_engine.begin()`），所以"迁移是原子的"是**错的**；但**预检与装引用约束这一段**是**同一事务 +
  显式停写锁**——`install_tenant_reference_consistency()` 先按固定表名顺序对每张受护表取
  `ACCESS EXCLUSIVE`（`SET LOCAL lock_timeout = '10s'`，超时失败而非跳过），再跑存量预检，最后执行
  触发器 DDL，因此"预检已通过、触发器还没装上"这个写入空窗**在数据库层被关闭**，不再只靠部署顺序
  （`storage-migrator` 是 API/worker 的启动前置）。
  **为什么在本次实现里取 `ACCESS EXCLUSIVE`（而非 `SHARE ROW EXCLUSIVE`）**：这是**针对本次形态**
  的选择，不是普适结论。本次 DDL 是触发器（`DROP TRIGGER` / `CREATE TRIGGER`），PostgreSQL 对它要求
  `ACCESS EXCLUSIVE`；取锁按固定表名顺序；调用方是迁移入口的独立事务。三者合起来，先取弱锁再升级会
  多出一段"持弱锁、等强锁"的窗口，与"先读后写"的事务形成**互锁**；一次取到 DDL 所需强度即消除该面。
  **换 DDL 种类、换取锁顺序或换事务边界，结论都要重新论证。**
  **预检为什么要 `SET LOCAL row_security = off`**：预检跑在 FORCE RLS 之后。`row_security = off`
  **不是**让角色绕过 RLS，也**不**赋予任何可见性——预检能看全行，靠的是迁移连接本身的有效权限
  （owner / superuser 连接）。该设置的作用是**绊线**：一旦角色配置退化到"查询需要受 RLS 过滤"，
  查询会由 PostgreSQL **直接报错**，而不是静默返回被过滤后的子集、让预检"看起来通过"。
  异常不设捕获，会中断整个迁移（版本 14 不写入）。
  预检仍然**先于**装约束，理由与之前一致（子侧触发器只约束新写入，管不到既有行）；独立的只读入口
  `precheck_tenant_reference_consistency()` 保留给夹具，迁移入口**不用**它。

- **发现凭据的密码只在角色创建时设置（运维注意，与运行时角色同一约定）**：`yuxi_worker_discovery`
  与 `yuxi_app` 一样，`CREATE ROLE ... PASSWORD` 只在角色**不存在**时执行，之后只收敛属性、不动密码。
  因此**改 `POSTGRES_DISCOVERY_PASSWORD` 不会自动改已存在角色的密码**——需要显式 `ALTER ROLE`
  或重建角色。测试侧的表现是：同一实例上角色已存在时，用新密码连接会
  `password authentication failed`（`POSTGRES_DISCOVERY_PASSWORD` 只在**全新库**上生效）。
  验收用的 `postgres-test` 数据目录挂 tmpfs、每轮 `--force-recreate`，所以走的是"全新库、CREATE 生效"
  这条路径；**已存在实例上重跑需要先把角色密码对齐**。
- **就绪信号在多租户模式下的诚实性**：readiness 的 Postgres 探针是**纯连通性**查询（`SELECT 1`，
  不引用任何表），所以"空业务表结果被当作成功"这条路不存在；但多租户模式下业务请求全 503，
  因此 `tenant_context` 检查会报错 → **不报就绪**（防"探活通过、业务全挂"的错误信号）。
- **`exc_info=True` 在本项目是静默 no-op（Loguru）**：它不会输出堆栈。本轮修好了 **Durable Task
  链**的 **5** 处（`task_queue_service.py` 发布失败日志 1 处、`task_service.py` 3 处、
  `run_worker.py` durable-task 收敛循环 1 处），并**记名**：`package/` + `server/` 下**仍有 16 处**
  同类**代码**写法——`run_worker.py` 12 处、`scheduled_agent_service.py` 3 处、
  `agents/callbacks/model_request_timing.py` 1 处，全部属 **agent-run / 定时任务链**，是**既有
  同类缺陷**、未在本轮改动范围内。多租户上线前应一并整改，否则那条链的故障同样没有堆栈。
- **`_apply_tenant_context` 跳过注入的前提是连接上没有遗留的 session 级租户设置——已在真连接池上取证**：
  两条隔离用例（`test_tenant_isolation_guardrails.py`）覆盖：
  1. **`test_a_reused_connection_without_context_reads_nothing_and_rejects_writes`**：同一条后端连接上
     先以租户 1 事务读到 1 行，随后**无上下文**的事务读 **0 行**、写入被拒（`SET LOCAL ROLE yuxi_app`）。
  2. **`test_runtime_pool_checkout_clears_tenant_after_commit_and_rollback`**：真实 `yuxi_app` 登录、
     `pool_size=1, max_overflow=0`、装**生产同一份** `after_begin` 钩子；重取到**同一 PID** 时，无论上
     一次事务是 **commit 还是 rollback**，无上下文都读 0 行、写被拒。
  结论：`set_config(..., true)` 的事务级作用域没有被绕过，无上下文**不会**继承上一个租户。→ **已验收**。
- **错误消息 → 恢复 的端到端证据（含坏消息消费）已补齐**：
  - **补发时机**：`test_durable_task_republish.py` 在**版本 14 + 受限角色 `yuxi_app` + 独立发现凭据**下
    跑通"启动补发"与"周期补发"（`pending → running → success`，`attempt_count == 1`，并读库断言领域
    success hook 的 `build_metadata.status == "completed"`）。
  - **坏消息消费**：`test_bad_message_is_consumed_without_execution_then_periodic_republish_completes`
    （参数化消息租户 `[None, 1]`，任务持久化租户为 2）证的是"**ARQ 消费掉错误消息、不执行 → 周期补发用
    持久化租户重发 → 最终完成**"这条**含坏消息**的整链：坏消息 job 被消费且 `success`（不卡队列）、任务
    保持 `pending` / `attempt_count == 0` / `worker_id is None`（**没执行**），解锁后收敛为 `success` /
    `attempt_count == 1`，且 `pool.all_job_results()` 里补发用的参数是 `(task_id, 2)`——**持久化租户**、
    不是默认 1。→ **已验收**。
- **列默认值保留**：结论是"在运行时路径上不构成绕过"，不等于"任意非运行时连接都不会写租户 1"。
  非运行时（owner / 迁移）连接属信任边界。
- **`tenants` 注册表是发现的硬前提**：缺 `tenants` 表时 `known_tenant_ids()` 直接失败（不静默回落
  到默认租户）。生产库由迁移保证其存在。
- **仍未解决（版本 14 前置）**：`SECURITY INVOKER` 触发器的隐藏行问题（父侧在 RLS 下对隐藏子行
  fail-open）。**已关闭**：迁移预检的写入空窗（`ACCESS EXCLUSIVE` 停写锁，见上）、数据库版本记录的
  时机分层（`record_schema_version("business", 14)` 在全部数据库步骤**之后**，且有注入失败用例取证）。

## 6. 下一步：版本 14 隔离接线与验收清单

接线**只能作为验证步骤**，不构成可部署交付。接线内容：把 `ensure_tenant_rls()` +
`ensure_tenant_reference_consistency()` + `ensure_tenant_root_immutability()` +
`ensure_tenant_discovery_boundary()` 接入迁移入口，常量推 14，存量跨租户子行预检**先于**装约束，
**发现边界最后跑**，数据库版本记录**后于**全部数据库步骤。

验收必须包含（缺一不可）——**下列各条的状态是"是否已完成验收"，不是"是否已写代码"**：

1. **连接池复用后的无上下文拒绝**：连接被复用过（前一个事务设过 `app.tenant_id`、本次无上下文）
   时确实**读 0 行、写被拒**。→ **已完成**
   （`test_a_reused_connection_without_context_reads_nothing_and_rejects_writes` +
   `test_runtime_pool_checkout_clears_tenant_after_commit_and_rollback`：真实 `yuxi_app` 登录、
   `pool_size=1`、生产同一份 `after_begin` 钩子、commit 与 rollback 两条路径都在同一 PID 上验过）。
2. **错误 / 缺失租户消息不执行，随后正确补发成功**：真实受限 worker 跑完"消费坏消息 → 不执行 →
   周期补发用持久化租户重发 → 最终完成"整链。→ **已完成**
   （`test_bad_message_is_consumed_without_execution_then_periodic_republish_completes`，参数化消息租户
   `[None, 1]`；另有启动／周期补发两条时机用例）。
3. **非默认租户正常完成**：非默认租户的任务在 RLS 生效下被 claim、执行、终态化。→ **已完成**
   （同上坏消息用例：任务持久化租户为 **2**，最终 `success` / `attempt_count == 1` / 数据集
   `build_metadata.status == "completed"`，且补发参数经断言为 `(task_id, 2)`）。
4. **INVOKER 隐藏行问题**：父侧对 RLS 隐藏子行的行为在真实配置下取证（现状：fail-open）。→ **未关闭**。
5. **预检到约束安装的写入空窗与版本记录时机**：证明空窗内不可能写入跨租户子行，且版本只在
   全部数据库步骤成功后记录。→ **已完成**
   - 空窗：`install_tenant_reference_consistency()` 同一事务内 `ACCESS EXCLUSIVE` 停写 +
     预检 + 触发器 DDL；`test_the_install_holds_a_stop_write_lock_across_precheck_and_install`
     在"取锁后、装触发器前"暂停安装，从另一连接写入必须**卡在 `knowledge_chunks` 的
     `RowExclusiveLock` 上、且确实被安装后端挡住**（表名 / 锁模式 / 阻塞者三重收窄），放行后该写入被
     触发器以引用一致性错误（`23514`）拒绝，最终失配计数 0。变异探针（去掉 `LOCK TABLE`）→ 该断言变红。
   - 失败回滚：`test_install_failure_rolls_back_trigger_ddl_and_releases_write_locks`
     （DDL 中途 `division by zero` → 整笔回滚、无残留触发器、锁释放、重放成功）。
   - 版本时机 + 重放：`test_migration_failure_and_replay.py`（真实入口注入失败在
     `ensure_tenant_rls` / `ensure_tenant_discovery_boundary` 两处 → 不记录 `business=14`；重跑完成；
     已是 14 的库上重放幂等且授权不回退）。
6. **`tenants` 权限契约**：接线后真实受限角色**不能**读整表、不能 INSERT/UPDATE/DELETE，只能
   读 `id`；且**重放 blanket 授权后**（按正确顺序）权限没有被重新放宽。→ **已完成**
   （9 条隔离用例 + 探针 L 6/7 红）。

**另外两条本轮新增的未完成项**：

7. **`test_durable_task_worker_path.py` 的两条 shipping-worker 用例尚未版本 14 就绪**：它们在
   版本 14 实例上**自身 setup 就被 RLS 拒绝**（经 `yuxi_app` 写夹具却没建租户上下文）。
   对照实验证明主树之所以"通过"是靠**已移除的隐式默认租户兜底**。需为这些用例建立**显式租户
   上下文**（或改用 owner 会话做夹具），并补**常驻 worker** 前置后才能作为验收。
8. **`require_discovery_credential()` 的启动失败用例**：缺 URL / 落在 superuser / 多授权限 /
   目标不一致 → 启动失败，已有单测；但**真实 worker 进程**在缺凭据时**起不来**的端到端证据
   仍未取（只有函数级证据）。

**准确记录（第十轮实测，隔离栈 `-p yuxi-v14`，测试库 `yuxi_test` 于 `--force-recreate` 后全新迁移）**：

| 集合 | 结果 |
|---|---|
| `test/integration/storage_migration/`（整目录） | **43 passed** |
| `test/integration/services/test_tenant_isolation_guardrails.py` | **65 passed** |
| 受影响单元集合（storage_migration / storage / task_queue / tasker / run_worker / tenant 中间件） | **216 passed，1 个 teardown ERROR（非本轮引入，见下）** |
| `test/unit/config/test_docker_compose_service_boundaries.py`（挂载仓库根后） | **51 passed** |
| `ruff check package`（CI 门禁） | **All checks passed!** |

**那 1 个 teardown ERROR 的归因（已用基线对照排除本轮）**：
`test/unit/services/test_run_worker.py::test_finish_run_terminal_loser_does_not_append_end_event`
**断言本身通过**，卡在 **teardown**——一个残留的 psycopg 异步连接池在事件循环收尾时尝试连接并阻塞，
被 `--timeout` 判为 ERROR（`psycopg.pool: error connecting in 'pool-1'`）。
**对照实验**：把 **HEAD 基线**放到独立 worktree、同一镜像、同一 `.env`、同一条命令跑
**同一个用例** → **同样** `1 passed, 1 error`、**同样 64.16s**、**同样的 pool 告警**。
所以它是**既有的环境相关 teardown 问题**，与本轮改动无关，也**不是**业务断言失败。
（另：该文件在基线上跑全量时还会出现 1 条 `asyncpg ... Event loop is closed` 的
`test_worker_startup_ensures_builtin_mcp_servers` 失败，同样是这类连接/循环收尾问题的另一副面孔。）

**`test_docker_compose_service_boundaries.py` 的 45 个 `skipped` 不是失败**：该文件按
`YUXI_PROJECT_ROOT` 或向上找 `docker-compose.yml` 定位仓库根，而 `docker compose run` 只挂了
`backend/*`；把仓库根挂进来（`-v <worktree>:/repo:ro -e YUXI_PROJECT_ROOT=/repo`）后 **51 passed**。

**ruff 门禁的口径（第十轮补齐，此前未跑）**：CI（`.github/workflows/ruff.yml`）**只检查
`backend/package`**，且锁定的 ruff 版本是 `uv.lock` 里的 **0.16.4**；命令为
`ruff check package` / `ruff format package --check` / `ruff check --select I package`。
本轮把工作树与 **HEAD 基线**逐条对齐后确认：

| 命令 | HEAD 基线 | 本轮工作树 |
|---|---|---|
| `ruff check package` | 通过 | **通过** |
| `ruff format package --check` | 4 个文件不合规 | **同样 4 个（无新增）** |
| `ruff check --select I package` | 5 处 | **同样 5 处（无新增）** |

即：**本轮改动没有新增任何 ruff 违规**。但**基线本身**在 `package` 上就有
`format` 4 处 / import 排序 5 处不合规（`milvus_graph_service.py`、`evaluation_repository.py`、
`task_repository.py`、`knowledge/base.py`、`knowledge/utils/__init__.py`、
`services/business_database_service.py`、`storage/postgres/manager.py`），这些**不是本轮引入**、
也**不在本轮范围内**；若要 CI 全绿需单独一轮处理。
本轮自己引入过的 3 处（`task_service.py` 格式、`run_worker.py` 与 `task_queue_service.py` 的 import
排序）已修掉并复核回到基线。

**已决定**：`租户 ID 枚举契约` 取 §5 的 **(b) 给 worker 独立数据库凭据**（已批准）。因此可以
宣称"发现权限已按消费者区分"：普通请求（`yuxi_app`）不能读 `tenants`，只有 worker 的
`yuxi_worker_discovery` 能读 `id`；API 不持有发现凭据。**决定之前**的旧口径（"尚未区分"）作废。

**开发库保持 `business=13 / knowledge=2 / rls_enabled=0`，不提交、不迁移，除非另行授权。**
口令轮换优先用**受控 `ALTER ROLE`**，不把重建数据库当默认修复手段。
