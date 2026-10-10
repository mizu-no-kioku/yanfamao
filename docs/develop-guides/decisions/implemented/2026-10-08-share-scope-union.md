# 共享范围支持「部门 ∪ 指定人」

状态：implemented
类型：architecture
Owner：backend/package/yuxi/permissions/resource_permission.py

## 问题

共享范围原本是**单维**的：一个 `read_scope` / `manage_scope` 只有一个 `access_level`（`global` /
`department` / `user`），`_normalize_scope` 强制只有当前档的名单非空（部门档返回 `user_uids: []`，
用户档返回 `department_ids: []`），判定 `scope_matches` 也只看当前那一档。于是「部门 A 的成员都能读」
与「再额外指定 B、C 两个人能读」在同一个库上无法同时表达。

上报现象：在知识库「权限配置」里保存部门共享（1 个部门）后点「指定人」，之前的部门选择就没了，
切回「部门共享」计数为 0，且校验以「至少需要选择一个部门」挡住保存。根因在前端
`ShareConfigForm.normalizeScope` 每次调用都清掉不属于当前档的名单，而切档与 `emitConfig` 都调它；
再叠加 `watch(scopes, …, { deep: true })` **每次本地改动都立刻 emit**、父组件的值又回灌派生一次，
清空因此不可逆。**数据库没有被动过**：弹窗只在点「保存」时提交。

期望（用户 2026-10-08 决定）：部门与指定人是**并集**关系，可以同时生效。这不是知识库独有的语义——
`ShareConfigForm` 被知识库、Skill、Agent 多处复用，后端 `normalize_permission_config` / `scope_matches` /
`_scope_contains` 也是所有可共享资源共用。因此按**平台级范围语义**处理，而不是给知识库打补丁。

## 决策

范围形状升级为并集，判定与包含关系随之改为「逐个主体」，旧数据靠**读时归一**保持语义不变。

### 实现方案

**形状**（`resource_permission.py` 的范围归一与判定，Owner）

```json
{ "access_levels": ["department", "user"], "department_ids": [2], "user_uids": ["u7"] }
```

- `access_levels` 非空、去重、按 `global → department → user` 规范化排序；含 `global` 时必须是唯一
  元素（global 已是全集，另两档冗余）。
- 只有**一种合法表达**：含 `department` ⇒ `department_ids` 非空；含 `user` ⇒ `user_uids` 非空；
  **不含 `department` ⇒ `department_ids` 必须为空**。违反即显式失败，**不静默清空**。前端同样守住这条：
  `ShareConfigForm` 在 emit 前清掉「已取消档位」的残留名单（表单内部仍留着，用户把该档选回来时原选择还在）。
- **读时归一**：入参若有 `access_level` 而无 `access_levels`，归一为 `access_levels: [access_level]`。
  存量行的 `access_level` 键**保留不删**（不做破坏性变更），任何一次保存后落成新形状；写入路径只写
  新形状。迁移语句里的默认值字面量同步改成新形状——它们只在 `version != 2` 的行上执行。

**判定与包含关系**

- `scope_matches`：含 `global` ⇒ 命中；否则 `部门 ∈ department_ids` **或** `uid ∈ user_uids`。
- `resolve_resource_permission` 结构不变（超管 → 所有权 → manage 范围 → read 范围 → 角色上限夹回）。
- **个人库 fail-closed 升级**：只认「`access_levels` 不含 `global`/`department` 的纯定向分享」才放宽到
  READ，个人库即便被误设部门档也不会撑大可见性。
- **包含关系**（`_validate_manage_scope` 与技能依赖的 `_scope_contains`）改为**逐个主体**覆盖：
  manage 的每个部门 id 必须出现在 read 的 `department_ids` 里（或 read 含 `global`），每个 uid 必须
  出现在 read 的 `user_uids` 里（或含 `global`）。**不做**「某用户属于某部门」的推导：那要查用户表、
  把范围判定与目录数据耦合，且保守方向是 fail-closed。对单维存量配置，该规则与旧的
  「同档 + 名单子集」**等价**（档位不同的组合两侧都判否）。

**校验**

- `_normalize_scope` 按上面的形状规则校验；`allowed_access_levels` 的约束从「单档 ∈ 允许集」变成
  `set(access_levels) ⊆ 允许集`。非管理员仍只允许 `["user"]`（Agent 与 Skill 一直有各自的
  `get_allowed_*_access_levels`）。
- **实现期补齐**：知识库写入路径原本**没有**把这条上限接上（`_normalize_share_config` 调
  `normalize_permission_config` 时不传 `allowed_access_levels`），于是非管理员能给自己建的知识库开
  部门档。本轮新增 `get_allowed_kb_access_levels(role)` 并在 create/update 传入 `role`：非管理员提交
  部门档/全局档被 **400 显式拒绝**（不是静默降级成 `user` 档）。读取路径不传 `role`，存量行不因谁来读
  而读不出来。
- 前端 `validate()` 用同一套规则（哪一档缺名单就报哪一档），并在 `requireReadScope` 为真而响应里没有
  可用的 `read_scope` 时判非法——挡住保存，而不是回落到 `global`。

**实现期收紧**：知识库写入路径原本会把操作者自己的部门/uid **无条件并进已声明的名单**
（`_normalize_share_config`），于是用户显式把自己所在部门从多部门名单里去掉后，保存又会被加回来——
一次静默放大授权，且与前端「只在该档名单为空时才自动补」的行为矛盾。现改为**照单保存**：操作者对自己
库的访问来自所有权判定里的所有者早返回（`resolve_resource_permission`），不依赖出现在名单里。

**前端**（`web/src/components/ShareConfigForm.vue` 及其消费者）

- 三张卡片从「单选」改成「全局独占 + 部门/指定人可多选」：点全局 ⇒ `access_levels: ['global']` 并清空
  两名单；点部门/指定人 ⇒ 加/减该档。**最后一档不能取消**（取消会让范围变成无人可见，点击不生效）。
  自动补当前部门/本人只在**该档名单为空**时发生，不覆盖已显式选好的名单（旧行为会在名单非空时也强插，
  使用户无法主动排除自己所在部门）。
- 开启范围开关时给一个**合法默认**（首个可用档 + 补当前主体）。否则在新形状下空名单的默认范围没有合法
  表达，开关一开就被归一回 `null`，成为死开关。
- `getShareConfigLabel` 渲染并集（`只读部门(2) · 用户(3)`），global 仍为 `只读全局`，有 manage 时的
  `读… · 管…` 保持；旧形状仍能显示。
- `toAccessLevels` / `isGlobalScope` / `cloneScope` 三处判据收敛在 `web/src/utils/shareConfig.js` 单一实现。
- `databaseCreateForm.createDerivedShareConfig` 与各处弹窗产出/传递新形状。

**不做**：数据迁移（读时归一）、清理存量 `access_level` 键、给非管理员开放部门档、跨档的成员推导。

## 替代方案

- **保留标量 `access_level`，把「部门」档放宽成「部门 ∪ 指定人」**。拒绝原因：并集之后**授权上唯一
  还有意义的区分只剩 global / 非 global**，`department` 与 `user` 不再影响判定，该字段从"授权档"退化
  成"最高档提示"。仓里已经有一处把它当授权档直接读的代码（`agent_repository.py` 的
  `read_scope.get("access_level") != "global"`），留一个会骗人的字段只会继续制造这类误读。
- **用组织架构表达**（把要指定的人放进部门）。拒绝原因：要求改组织结构，不是用户要的东西，且会让
  部门语义被当作访问控制名单使用。
- **只修前端交互，后端保持互斥**（切档时把各档选择留在草稿里，切回来还在）。拒绝原因：这修的是切换
  体验，不是用户要的语义——用户要的是部门与指定人**同时生效**。
- **包含关系做跨档推导**（`read = {user: [u7]}` 覆盖 `manage = {department: [2]}` 当且仅当 u7 ∈ 部门 2）。
  拒绝原因：需要为用户查部门、把范围判定与用户目录耦合，且在"拒绝"方向上没有任何收益。

## 后果

- **旧前端 + 新后端不会静默放大授权**：旧 bundle 在档位不在允许集时会把档位设成允许集第一档（管理员即
  `global`），等于"打开权限页保存一次就把部门库改写成全局库"。现在前端对新旧形状都做归一、对「拿不到
  档位」的范围判非法并不 emit（校验挡住保存），后端写入路径也拒绝旧键（400）。发布上前后端同镜像同一次
  compose 上线（本仓没有独立部署前端的路径），窗口只剩"用户本地缓存的旧 JS"。
- **读时归一是长期兼容路径**。存量行会一直带着 `access_level` 键，任何绕过归一器直接读该键的新代码都会
  静默错判。归一器是该键唯一的读取入口（另一处是迁移 SQL），全仓搜索已核对。
- **并集让"一次配置覆盖更多主体"变得容易**，等于把授权放大做成一步到位的操作。界面如实显示
  `部门(2) · 用户(3)`，不让人以为还是单选。
- **含两个维度的范围会让"这个库谁能看"不再能一眼判断**，检索/排查时需要同时看两档。这是并集语义的
  固有代价，选择它的理由（用户明确要同时生效）记在问题节。
- **技能依赖判定与范围校验共用同一包含关系**。改错一处会同时影响"技能能否依赖"和"权限页能否保存"。
  包含关系只写一份、由两处调用，并对单维存量组合断言等价。
- **非管理员仍只有 `["user"]`**，所以并集在普通用户路径上不可达。若将来给非管理员开部门档，
  `_ensure_share_scope_within_operator_department`（普通用户只能分享给本部门）那类约束要重新审视——
  并集会让"分享给自己部门 + 指定人"变成合法组合，而"指定人必须在本部门内"的检查是按名单逐个做的，
  届时要明确它在并集下的语义。

## 验证

`Passed` 表示按下面列出的命令实测通过；命令与结果取自 2026-10-10 的实现期运行，`Inspected` 表示本轮
核对过的现状事实。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 结果 |
|---|---|---|---|---|---|
| 并集命中：部门成员可读、指定用户可读 | 只按其中一档判定，另一档的人拿不到 | `scope_matches` | 单元 `test/unit/permissions/test_resource_permission.py`；真实 HTTP `test/integration/api/test_knowledge_router.py::test_union_scope_grants_read_to_department_and_named_user`（含 `accessible` 清单回读 `access_levels`） | 同一用例断言既不在该部门、也不是指定人者得 403 / `NONE` | Passed |
| 旧形状读取归一后判定与今天完全一致 | 归一漏了某条路径，存量行可见性变化 | `normalize_permission_config` + `scope_matches` | 单元对 `{access_level: "department", department_ids:[2]}` 与归一后形状跑同一组断言；真实 HTTP `...::test_legacy_access_level_payload_is_rejected_on_write` 把行直接写成旧形状后详情仍 200 且 `access_levels == ["global"]` | 把归一改成默认 global 时，部门外用户用例变红 | Passed |
| 迁移 SQL 对 `version != 2` 的存量行产出新形状且幂等 | 迁移写回旧键，新写入路径会拒绝自己造出的行；或重复执行改写已合规行 | `storage/postgres/manager.py` | 单元 `test/unit/storage/test_postgres_manager_schema.py::test_share_config_migration_wraps_legacy_scopes_as_read_only`：断言三条 UPDATE 都写 `"access_levels"`、skills 分支用 `jsonb_build_array('user')`/`jsonb_build_array(created_by)`，且 `"access_level"` 字面量不再出现；三条 WHERE 均跳过 `version = 2`（计数 == 3） | 把任一字面量改回旧键时该用例变红（已实测变异） | Passed |
| 显式声明的名单不被操作者身份撑大 | 用户去掉自己所在部门后保存又被加回，等于一次静默放大授权 | `knowledge/manager.py` | 单元 `test/unit/knowledge/test_knowledge_share_config_shape.py::test_explicit_list_is_not_widened_by_operator_identity` 与 `test_union_round_trips_through_normalization` | 改回无条件并入时两条用例变红（已实测 RED） | Passed |
| 个人库含部门档仍不放开 | 个人库被自己的 `share_config` 撑大，陌生人拿到 READ | `resolve_knowledge_base_permission` | `...::test_personal_knowledge_base_with_department_scope_stays_private`：管理员可写入部门档（200），非创建者同部门用户仍 403 | 非创建者同部门用户读到内容即为失败 | Passed |
| 非管理员提交含部门档的范围被拒，不静默降级 | 静默降级成只有 `user` 档，或直接放行 | `knowledge/manager.py` 的 `get_allowed_kb_access_levels` | `...::test_plain_user_cannot_grant_department_level`：400，且断言拒绝原因是授予上限（`无权使用该资源共享范围`） | 放行或降级成 `["user"]` 即为失败 | Passed |
| manage 主体必须落在 read 内 | 管理范围超出读取范围，出现"能管却不能读" | `_validate_manage_scope` / `scope_contains` | 单元：`manage={["department"],[3]}`、`read={["department"],[2]}` → 拒绝；并集下 `read={["department","user"],[2],["u7"]}` 覆盖 `manage={["user"],["u7"]}` → 放行 | 把子集判定改成"档位相同即放行"时用例变红 | Passed |
| 技能依赖的兼容判定对有单维配置的存量数据等价 | 改动让既有技能依赖关系从兼容变成不兼容 | `_scope_contains` | 单元：对 `_scope_contains` 的全部单维组合（同档子集 / 跨档 / global）断言改动前后同值 | 任意一条组合取值变化即为失败 | Passed |
| 表单：切档不再丢已选内容 | 用户切档即丢已选内容（上报现象） | `ShareConfigForm.vue` | `web/test/unit/share_config_form_mount.test.js`：选部门 → 点指定人 → 再点回部门，`department_ids` 仍是原值 | 旧形状下 `normalizeScope` 会在切档时清空另一档名单——这正是上报现象的机制 | Passed |
| 表单：部门 + 指定人可同时保存 | 保存后只剩一档，设置仍被覆盖 | `ShareConfigForm.vue` + 建库/详情页 | 同文件的挂载用例断言 emit 的 `read_scope.access_levels` 同时含两档、两个名单都在；真实 HTTP 侧由并集用例回读 `access_levels` | 保存后只剩 `["department"]` 即为失败 | Passed |
| 老前端 bundle + 新后端不会把范围悄悄放大 | 旧 bundle 读到 `undefined` 后默认成 `global` 并保存 | `ShareConfigForm.normalizeScope` + 后端写入路径 | 挂载用例：既无 `access_level` 也无 `access_levels` 的范围 → 不 emit、`validate()` 判非法；后端 `...::test_legacy_access_level_payload_is_rejected_on_write` 断言旧键 PUT 被 400 且 detail 含 `access_levels` | 默认成 `global` 或静默归一落库即为失败 | Passed |
| 前端不再拿空对象拼标签 | 列表卡片显示假的「只读用户(0)」 | `getShareTagLabel` | 既有 `web/test/unit/database_card_share_tag.test.js` 覆盖（空对象也算缺） | 空对象返回标签即变红 | Passed |
| 改名后没有残留的直接读取点 | 改名后某处静默失效（如内置智能体被允许非全局共享） | 各消费者 | 全仓搜索 `access_level`（排除归一器与迁移 SQL）：后端只剩归一器与 SQL；前端只剩 `toAccessLevels` 的兼容读取 | 把该处改回直接读旧键时，对应用例变红 | Passed |
| 本次改动无外部消费者受损 | CLI / 外部 API 依赖旧形状 | — | 已核对：`packages/`（含 yuxi-cli）不出现 `share_config`/`access_level`；`external_kb_router` 不暴露 `share_config` | 出现仓外消费者时需要另定兼容策略（当前无） | Inspected |
| 回归：既有 KB / Skill / Agent 授权行为不变 | 改动放宽或收紧了任何一处既有可见性 | 全部 | `python3 scripts/verify_engineering_contracts.py` → 0；`pytest test/unit -m "not slow" --ignore test/unit/services/test_run_worker.py` → **2565 passed / 0 failed**；`pytest test/integration/api`（带凭证）→ 229 passed / 7 failed / 6 errors（失败项**全部为既存**，见下）；`ruff check --no-cache package server test`（排除未跟踪的本地数据集目录）→ `All checks passed`；`pnpm run lint:check` → 0；`pnpm run test:unit` → **436 passed / 0 failed**；`pnpm run build` → 0；vitepress build → 0 | 与改动前基线逐项对照 | Passed（既有失败见下） |

**基线对照**：本特性开始前（`e96d166`）单元为 **2533 passed / 0 failed**（`58 skipped`）。当前为
**2566 passed / 0 failed**，净增 33 条：其中 24 条来自本特性的形状/判定/知识库路径用例（Task 1–3），
8 条来自 Agent 与 Skill 写入路径的用例（Task 4），1 条钉住「显式名单不被操作者身份撑大」。并集线合并
进分支后的**中途**快照曾是 `2542 passed / 15 failed`——那 15 条是形状已改而 Agent/Skill 写入路径未跟上
导致的既有用例失败，随 Task 4 修好，因此不构成新增回归。web 侧从 `426 passed` 变为 `436 passed`
（Task 5 新增 4 条、Task 6 新增 6 条）。

**已知既存失败（与本次无关）**：这些失败所在的文件与用例在 `e96d166`（本特性开始前）上**逐字节未变**，
且在同一环境逐文件运行得到**完全相同的失败集合**——不是"总数接近"，是同一批失败项。

- `test/integration/api` 整目录：7 failed + 6 errors，分布在 `test_auth_router`（登录锁定后的 teardown）、
  `test_project_api`（删除用户后 `projects.uid` 外键违反）、`test_task_router`、`test_system_router_api`
  （`legacy-saves/logs` 路径）、`test_chat_router`、`test_checkpoint_state_view`、
  `test_context_compression_router`，以及
  `test_knowledge_router::test_plain_user_personal_notion_database_requires_own_token`
  ——该用例在 **pytest 进程**里设 `NOTION_TOKEN`，而断言打的是**真实 HTTP 服务端**（`httpx.AsyncClient`
  指向 API 地址），服务端看不到该变量，于是先抛出通用的"Notion 参数缺失"。它只能在一个"服务端本身
  配了 `NOTION_TOKEN`"的环境里通过。
- `python3 -m unittest scripts.test_verify_engineering_contracts`：1 个 error
  （`test_decision_owner_symlink_cannot_escape_repository`）——Windows 上创建符号链接需要特权，
  `symlink_to` 直接抛 `OSError`，属环境限制。
- `ruff check` 按计划原命令会因**未跟踪的本地数据集文件**（`test/data/` 下的 HuggingFace 下载物，不在
  仓库内）报错；排除该目录后仓库内代码 `All checks passed`。
