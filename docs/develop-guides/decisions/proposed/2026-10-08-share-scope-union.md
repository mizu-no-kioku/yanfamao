# 共享范围支持「部门 ∪ 指定人」

状态：proposed
类型：architecture
Owner：backend/package/yuxi/permissions/resource_permission.py

## 问题

共享范围目前是**单维**的：一个 `read_scope` / `manage_scope` 只有一个 `access_level`（`global` /
`department` / `user`），`_normalize_scope` 强制只有当前档的名单非空（部门档返回 `user_uids: []`，
用户档返回 `department_ids: []`），判定 `scope_matches` 也只看当前那一档。于是「部门 A 的成员都能读」
与「再额外指定 B、C 两个人能读」在同一个库上无法同时表达。

上报现象：在知识库「权限配置」里保存部门共享（1 个部门）后点「指定人」，之前的部门选择就没了，
切回「部门共享」计数为 0，且校验以「至少需要选择一个部门」挡住保存。根因在前端：
`ShareConfigForm.normalizeScope`（`web/src/components/ShareConfigForm.vue:250-279`）每次调用都清掉
不属于当前档的名单，而 `setAccessLevel`（`:329-343`）切档后立刻调它、`emitConfig`（`:313-319`）也调它；
再叠加 `watch(scopes, …, { deep: true })`（`:441-447`）**每次本地改动都立刻 emit**、父组件的值又经
`watch(() => props.modelValue, initConfig, { deep: true })`（`:436`）回灌派生一次，清空因此不可逆。
**数据库没有被动过**：弹窗只在点「保存」时提交（`DataBaseView` 之外，`DataBaseInfoView.vue:1043-1062`）。

期望（用户 2026-10-08 决定）：部门与指定人是**并集**关系，可以同时生效。这不是知识库独有的语义——
`ShareConfigForm` 被知识库、Skill、Agent 四处复用（`DataBaseInfoView`、`DatabaseCreateFlowModal`、
`SkillDetailView`、`SkillInstallFlowModal`、`AgentEditModal`），后端 `normalize_permission_config` /
`scope_matches` / `_scope_contains` 也是所有可共享资源共用。因此按**平台级范围语义**处理，而不是给
知识库打补丁。

## 提案

范围形状升级为并集，判定与包含关系随之改为「逐个主体」，旧数据靠**读时归一**保持语义不变。

### 实现方案

**形状**（`resource_permission.py` 的范围归一与判定，Owner）

```json
{ "access_levels": ["department", "user"], "department_ids": [2], "user_uids": ["u7"] }
```

- `access_levels` 非空、去重、按 `global → department → user` 规范化排序；含 `global` 时必须是唯一
  元素（global 已是全集，另两档冗余）。
- 只有**一种合法表达**：含 `department` ⇒ `department_ids` 非空；含 `user` ⇒ `user_uids` 非空；
  **不含 `department` ⇒ `department_ids` 必须为空**（要同时含部门就必须写进 `access_levels`）。这条
  规则让"字段与名单互相矛盾"的配置无法存在。
- **读时归一**：入参若有 `access_level` 而无 `access_levels`，归一为 `access_levels: [access_level]`。
  存量行的 `access_level` 键**保留不删**（不做破坏性变更），任何一次保存后落成新形状；写入路径只写
  新形状。迁移语句里的默认值字面量（`storage/postgres/manager.py:592`、`:963`、`:969`、`:1026`）同步
  改成新形状——它们只在 `version != 2` 的行上执行。

**判定与包含关系**

- `scope_matches`：含 `global` ⇒ 命中；否则 `部门 ∈ department_ids` **或** `uid ∈ user_uids`。
- `resolve_resource_permission` 结构不变（超管 → 所有权 → manage 范围 → read 范围 → 角色上限夹回）。
- **个人库 fail-closed 升级**：今天只认 `read_scope.access_level == "user"` 才放宽到 READ；改为
  「`access_levels` 不含 `global`/`department` 的纯定向分享」才放宽，个人库即便被误设部门档也不会
  撑大可见性。
- **包含关系**（`_validate_manage_scope` 与技能依赖的 `_scope_contains`）改为**逐个主体**覆盖：
  manage 的每个部门 id 必须出现在 read 的 `department_ids` 里（或 read 含 `global`），每个 uid 必须
  出现在 read 的 `user_uids` 里（或含 `global`）。**不做**「某用户属于某部门」的推导：那要查用户表、
  把范围判定与目录数据耦合，且保守方向是 fail-closed。对单维存量配置，该规则与今天的
  「同档 + 名单子集」**等价**（档位不同的组合两侧都判否），这是实现时要断言的不变量。

**校验**

- `_normalize_scope` 按上面的形状规则校验；`allowed_access_levels` 的约束从「单档 ∈ 允许集」变成
  `set(access_levels) ⊆ 允许集`。非管理员仍只允许 `["user"]`（`get_allowed_agent_access_levels`、
  `get_allowed_skill_access_levels`、建库与详情页的 `allowedAccessLevels`），所以并集实际上只在管理员
  操作时出现——**本次不放开非管理员使用部门档**。
- 前端 `validate()` 用同一套规则（哪一档缺名单就报哪一档）。

**前端**（`web/src/components/ShareConfigForm.vue` 及其消费者）

- 三张卡片从「单选」改成「全局独占 + 部门/指定人可多选」：点全局 ⇒ `access_levels: ['global']` 并清空
  两名单；点部门/指定人 ⇒ 加/减该档（仍自动补选当前部门、自动补选操作者本人）。**最后一档不能取消**
  （取消会让范围变成无人可见，点击不生效）。两张卡片的计数徽标各自独立显示。
- `getShareConfigLabel`（`web/src/utils/shareConfig.js`）渲染并集：`只读部门(2) · 用户(3)`，global 仍为
  `只读全局`，有 manage 时的 `读… · 管…` 保持。
- `databaseCreateForm.createDerivedShareConfig` 与四处弹窗产出/传递新形状；弹窗的
  `allowedAccessLevels` 语义不变（仍是"可用的档位集合"）。

**不做**：数据迁移（读时归一）、清理存量 `access_level` 键、给非管理员开放部门档、跨档的成员推导。

## 替代方案

- **保留标量 `access_level`，把「部门」档放宽成「部门 ∪ 指定人」**（合法性：`department` 档
  `department_ids` 非空、`user_uids` 可空；判定非 global 时取并集）。拒绝原因：并集之后**授权上唯一
  还有意义的区分只剩 global / 非 global**，`department` 与 `user` 不再影响判定，该字段从"授权档"退化
  成"最高档提示"。仓里已经有一处把它当授权档直接读的代码（`agent_repository.py:209` 的
  `read_scope.get("access_level") != "global"`），留一个会骗人的字段只会继续制造这类误读。本方案的
  代价是改名波及所有消费者（后端 4 处、前端 6 处、文档 3 处），但那些都是仓内消费者，无外部兼容负担。
- **用组织架构表达**（把要指定的人放进部门）。拒绝原因：要求改组织结构，不是用户要的东西，且会让
  部门语义被当作访问控制名单使用。
- **只修前端交互，后端保持互斥**（切档时把各档选择留在草稿里，切回来还在）。拒绝原因：这修的是切换
  体验，不是用户要的语义——用户要的是部门与指定人**同时生效**。
- **包含关系做跨档推导**（`read = {user: [u7]}` 覆盖 `manage = {department: [2]}` 当且仅当 u7 ∈ 部门 2）。
  拒绝原因：需要为用户查部门、把范围判定与用户目录耦合，且在"拒绝"方向上没有任何收益。

## 验收标准

`Not run` 表示尚未实现，命令为实现后应执行的证据；`Inspected` 表示本轮已核对过的现状事实。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 并集命中：部门成员可读、指定用户可读 | 只按其中一档判定，另一档的人拿不到 | `scope_matches` | `test/unit/permissions/test_resource_permission.py` 增例：`{access_levels:["department","user"], department_ids:[2], user_uids:["u7"]}` 对部门 2 的成员与 u7 均判定命中 | 同一用例断言既不在部门 2、也不是 u7 的人得到 NONE | Not run |
| 旧形状读取归一后判定与今天完全一致 | 归一漏了某条路径，存量行可见性变化 | `normalize_permission_config` + `scope_matches` | 对 `{access_level: department, department_ids:[2]}` 与归一后的 `{access_levels:["department"],…}` 跑同一组用户断言结果相同；真实 HTTP 上对现有库（研发知识库等部门档、通用知识库 global）复跑 | 负向：把归一改成默认 global 时，部门外用户用例必须变红 | Not run |
| 个人库含部门档仍不放开 | 个人库被自己的 share_config 撑大，陌生人拿到 READ | `resolve_knowledge_base_permission` | `test/integration/api/test_knowledge_router.py` 断言非创建者的同部门用户对「个人库 + 部门档」得 403；单元层同断言 | 非创建者同部门用户读到内容即为失败 | Not run |
| manage 主体必须落在 read 内 | 管理范围超出读取范围，出现"能管却不能读" | `_validate_manage_scope` | 单元：`manage={["department"],[3]}`、`read={["department"],[2]}` → 拒绝；并集下 `read={["department","user"],[2],["u7"]}` 覆盖 `manage={["user"],["u7"]}` → 放行 | 把子集判定改成"档位相同即放行"时用例变红 | Not run |
| 技能依赖的兼容判定对有单维配置的存量数据等价 | 改动让既有技能依赖关系从兼容变成不兼容 | `_scope_contains` | 单元：对 `_scope_contains` 的全部单维组合（同档子集 / 跨档 / global）断言改动前后同值 | 任意一条组合取值变化即为失败 | Not run |
| 表单：点「指定人」不再清掉部门选择 | 用户切档即丢已选内容（上报现象） | `ShareConfigForm.vue` | `web/test/unit/share_config_form_mount.test.js` 増例：选部门 → 点指定人 → 点回部门，部门计数仍为 1 | 实现前实测该用例变红（计数归 0） | Not run |
| 表单：部门 + 指定人可同时保存 | 保存后只剩一档，设置仍被覆盖 | `ShareConfigForm.vue` + 建库/详情页 | 挂载用例断言保存时 emit 的 `read_scope.access_levels` 同时含两档、两个名单都在；真实 HTTP 断言 PUT 后详情返回并集 | 保存后详情只剩 `["department"]` 即为失败 | Not run |
| 老前端 bundle + 新后端不会把范围悄悄放大 | 旧 bundle 读到 `undefined` 后默认成 `global` 并保存 | `ShareConfigForm.normalizeScope` | 挂载用例：传入既无 `access_level` 也无 `access_levels` 的范围 → 校验不通过、保存被挡住（而不是默认 global） | 默认成 `global` 时用例变红 | Not run |
| 前端不再拿空对象拼标签 | 列表卡片显示假的「只读用户(0)」 | `getShareTagLabel` | 既有 `web/test/unit/database_card_share_tag.test.js` 覆盖（空对象也算缺） | 空对象返回标签即变红 | Passed |
| 改名后没有残留的直接读取点 | 改名后某处静默失效（如内置智能体被允许非全局共享） | 各消费者 | 全仓搜索 `access_level`：只允许出现在归一器与迁移 SQL；现状搜索出唯一一处直接读取（`agent_repository.py:209` 的 `read_scope.get("access_level") != "global"`），随本次改为按归一结果判断，由「内置智能体必须全局共享」的既有用例守住 | 把该处改回直接读旧键时，对应用例变红 | Not run |
| 本次改动无外部消费者受损 | CLI / 外部 API 依赖旧形状 | — | 已核对：`packages/`（含 yuxi-cli）不出现 `share_config`/`access_level`；`external_kb_router` 不暴露 `share_config` | 出现仓外消费者时需要另定兼容策略（当前无） | Inspected |
| 回归：既有 KB / Skill / Agent 授权行为不变 | 改动放宽或收紧了任何一处既有可见性 | 全部 | `pytest test/unit -m "not slow"`、`pytest test/integration/api/test_knowledge_router.py`、`test/integration/api/test_skill*`、`pnpm run test:unit`、`lint:check`、`build`、`verify_engineering_contracts.py`、vitepress build | 与改动前基线逐项对照，出现新增失败即为失败 | Not run |

## 风险

- **旧前端 + 新后端会静默放大授权**（最高风险）。浏览器缓存里的旧 bundle 读 `access_level` 得到
  `undefined`，而 `normalizeScope` 在档位不在允许集时会把 `access_level` 设成
  `normalizedAllowedAccessLevels[0]`——管理员即 `global`。于是用户只是打开权限页保存一次，就可能把
  一个部门库改写成全局库。缓解：`normalizeScope` 对「两个字段都缺」的范围判为**非法**（不默认 global），
  校验挡住保存，并用上表那条挂载用例钉住；发布上前后端同镜像同一次 compose 上线（本仓没有独立部署
  前端的路径），把窗口压到"用户本地缓存的旧 JS"。
- **读时归一是长期兼容路径**。存量行会一直带着 `access_level` 键，任何绕过归一器直接读该键的新代码
  都会静默错判（已发现一处）。缓解：归一器是该键唯一的读取入口，实现时全仓搜索并在文档与 docstring
  里写明；不清理存量（避免破坏性变更）是接受的代价。
- **并集让"一次配置覆盖更多主体"变得容易**，等于把授权放大做成了一步到位的操作。界面必须如实显示
  `部门(2) · 用户(3)`，不能让人以为还是单选；否则会出现"我以为是给部门开，其实还带了几个外部用户"
  的误解。
- **含两个维度的范围会让"这个库谁能看"不再能一眼判断**，检索/排查时需要同时看两档。这是并集语义的
  固有代价，选择它的理由（用户明确要同时生效）已经记在问题节。
- **技能依赖判定与范围校验共用同一包含关系**。改错一处会同时影响"技能能否依赖"和"权限页能否保存"。
  缓解：包含关系只写一份、由两处调用，并对单维存量组合断言等价（上表两行）。
- **非管理员本次仍只有 `["user"]`**，所以并集在普通用户路径上不可达。若将来给非管理员开部门档，
  `_ensure_share_scope_within_operator_department`（普通用户只能分享给本部门）那类约束要重新审视——
  并集会让"分享给自己部门 + 指定人"变成合法组合，而"指定人必须在本部门内"的检查是按名单逐个做的，
  届时要明确它在并集下的语义。
