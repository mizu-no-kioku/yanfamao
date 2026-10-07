# 个人知识库与共享知识库的可见性边界

状态：implemented
类型：feature
Owner：backend/package/yuxi/permissions/resource_permission.py

## 问题

知识库的可见性只有一个整体维度：`share_config` 的 v2 scope（`global` / `department` / `user`），而所有知识库路由都挂在管理员底座上。普通用户（`role = user`）无法创建或使用任何知识库：创建路由是 `Depends(get_admin_user)`，知识库的全部读写路由经 `require_knowledge_base_read` / `require_knowledge_base_manage` 也落在同一个管理员底座上，普通角色一律 403；图谱能力（抽取配置、图谱构建、图谱状态与图谱查询）同样如此。

同时，现有知识库的 scope 是 `global`（含义是"人人可见"），新建知识库的默认 scope 也是 `global`。所以即使放开普通用户建库，也没有"这是我的库、别人看不到"的表达方式，也没有"这个库属于某个部门、别的部门看不到"的默认。

本轮不改变可见性的粒度：不做文件级或图谱实体级的可见性，不做部门层级、租户或用户组，也不引入新的授权表——知识库与文件之间的可见性仍然整体生效。

## 决策

知识库增加范围标记 `scope`（`personal` / `shared`，默认 `shared`），个人库的归属继续用已有的 `created_by` 表达，可见范围继续用 `share_config` 的 v2 scope 表达。授权从"按角色"改为"按知识库范围"：普通用户因此可以拥有并完整使用个人知识库，共享库的边界保持不变。

### 实现方案

**范围列与读写暴露**：`knowledge_bases` 新增 `scope VARCHAR(16) NOT NULL DEFAULT 'shared'`（`backend/package/yuxi/storage/postgres/models_knowledge.py`）；迁移是一条幂等的裸 `ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS ...`（`storage/postgres/manager.py` 的 `ensure_knowledge_schema`，不引入 Alembic）。`default` 与 `server_default` 并存，纯 SQL 插入也落在 `shared`。读模型 `KnowledgeBaseSummary` / `KnowledgeBaseDetail` 暴露 `scope`，由 `knowledge/manager.py` 的 `_database_read_fields` 从**列值**透传；`server/utils/knowledge_response.py` 的 `serialize_knowledge_base` 把 `scope` 带进 HTTP 响应（详情与列表共用）。

**权限判定**（`backend/package/yuxi/permissions/resource_permission.py` 的 `resolve_knowledge_base_permission`）：超管放行与 personal 分支都**早于**通用判定。

- `scope == personal` 且 `created_by == user.uid` → `MANAGE`；
- `scope == personal` 且非创建者 → 只有 `read_scope` 的 **`user` 级**命中（`access_level == "user"` 且 `uid ∈ user_uids`）才给 `READ`，其余一律 `NONE`；
- 其余（含 `scope == shared`）交回通用的 `resolve_resource_permission`。

**personal 分支的职责是 fail-closed 护栏，不是"让创建者拿到 MANAGE"。** 创建者的 `MANAGE` 来自通用路径里"所有者早返回"这一既有分支，它本来就早于角色上限夹取，没有 personal 分支创建者也照样是 `MANAGE`。分支真正挡住的是：通用路径会照 `share_config` 的字面值放行，个人库的 `read_scope` 一旦被误设成 `global` / `department`，陌生人就会拿到 `READ`——个人库的边界被它自己的分享配置撑破。所以个人库只承认 user 级定向分享，`global` / `department` 级 `read_scope` 在个人库上被忽略。

**角色上限仍然只约束非创建者。** `KNOWLEDGE_BASE_PERMISSION_POLICY` 把 `user` 夹在 `READ`，但夹取只作用于 scope 命中的分支；所有者的早返回在夹取**之前**，所以**任何**知识库的创建者都直接拿到 `MANAGE`，与角色无关。业务上普通用户只能创建个人库，所以"普通用户在共享库上最多 READ"只对**非创建者**成立；普通用户创建的共享库不可达。

**适配器换底座**（`backend/server/utils/knowledge_permissions.py`）：`require_knowledge_base_read` / `require_knowledge_base_manage` 的 `Depends(get_admin_user)` 换成 `Depends(get_required_user)`（任意已登录且绑定部门的用户），授权完全交给 `resolve_knowledge_base_permission`。这两个函数是知识库读、写、图谱、评估四类路由唯一的管理员底座，换掉后 63 个路由站点一次放开，不需要逐个路由改。`POST /api/graph/subgraph` 的 `kb_id` 只在请求体里、用不了这两个适配器，改为 `get_required_user` + 显式 `ensure_knowledge_base_permission(..., READ)`。

**创建与分享的收敛**（`backend/server/routers/knowledge_router.py` + `knowledge/manager.py`）：创建路由底座换成登录用户，`scope` 是可选请求体字段——未指定时按角色取默认（`admin` / `superadmin` → `shared`，其余 → `personal`），非法取值 400，普通角色显式请求 `shared` 直接 403（**不静默降级**；静默降级会返回 200 而把普通用户建成共享库）。服务层 `_normalize_share_config(..., scope)` 按 scope 生成默认读取范围：`personal` → 仅创建者；`shared` + 有部门 → 本部门；`shared` + 无部门 → fail-closed 仅创建者，**绝不写 `global`**。创建时**不写 `manage_scope`**：创建者的 `MANAGE` 来自所有者早返回，不依赖它，所以 `manage_scope` 保持 `None`。存量行（`share_config` 为 NULL）在读取路径继续解析为 `global`，历史语义不变。`/databases/accessible` 的列表项补 `scope` 与 `can_manage`。

**前端**：知识库页与创建入口对普通用户开放——`ExtensionsView.vue` 的 tab 列表与渲染条件、详情路由的 `meta.requiresAdmin`、`knowledge_api.js` 的建库与知识库类型两个接口。列表按**归属**分为"个人知识库 / 共享知识库"两段，判据是 `scope === 'personal' && created_by === 当前用户 uid`（抽成 `web/src/utils/kb_utils.js` 的 `isOwnPersonalDatabase`），而不是 `can_manage`——否则别人分享给我的个人库会落进"个人知识库"，让人误以为是自己建的。创建表单按角色收敛范围选项，个人库只提供"指定用户"分享。

**第六类调用者是前端 API 传输层本身。** 转换前 `web/src/apis/knowledge_api.js` 有 62 处 `apiAdmin*` 调用点，它们会先执行 `checkAdminPermission()`、在发请求之前就抛"需要管理员权限"——这是一道**客户端授权预判**，也是整个仓库里唯一一处"前端即授权边界"的例外。授权改为按库判定后前端无法自判，因此把 48 处 KB 级调用改成普通传输层（`apiGet` / `apiPost` / `apiPut` / `apiDelete`），由后端判定、前端按 403 处理。规则是：**KB 级调用跟随库权限，管理级调用仍限管理员**。14 处管理级调用（`databaseApi.getDatabases`、`mindmapApi.getDatabases`、`typeApi.getStatistics`、`evaluationApi` 11 处）与后端 `/databases`、`/mindmap/databases`、`/stats`、`/files/markdown` 保持管理员底座；`files/upload` 与 `files/fetch-url` 的 `kb_id` 可缺省，换底座后在"无 `kb_id`"分支上补了管理员闸门——否则它们会变成任意登录用户可写的对象存储入口。

**分工**：`resource_permission.py` 拥有"谁能对某个知识库做什么"的判定；`knowledge_permissions.py` 拥有"HTTP 入口如何应用该判定"；服务层拥有"哪些 scope 取值对哪种角色合法"与默认范围；前端只做呈现与传输，不构成授权边界。

## 替代方案

- **只放开创建路由，不动权限上限**。改动最小。不采用的原因：普通用户被 `{"user": READ}` 夹成只读，连自己建的库都传不了文档，"个人知识库"徒有其名。
- **让创建者路径也受角色上限**（改通用解析器，把所有者早返回也过一遍夹取）。字面上更贴合"普通用户在共享库上最多 READ"。不采用的原因：`resolve_resource_permission` 由 agent / skill / 知识库三类资源共用，改动会把历史上由普通用户创建的知识库从 `MANAGE` 静默降级为 `READ`，远超本需求范围；而创建收敛之后，普通用户建共享库在业务上已不可达。
- **用 `share_config` 的新版本表达"个人库"**（v3）。不采用的原因：`normalize_permission_config` 明确要求 `version == 2`，用版本号同时表达资源类型和权限结构会让两件事耦在一起；一列 `scope` 更直接，也让"个人库"在查询层可索引。
- **个人库不加范围列，靠 `share_config` 反推**。不采用的原因：`access_level = "user"` 也可能是共享库的定向分享，反推不出"这是我的个人资产"，无法回答"普通用户能建什么"。
- **引入 ACL / 授权表**。最通用。不采用的原因：仓库里没有任何这种抽象（权限全部内嵌在资源的 `share_config`），新增一套并行模型会让"谁能看这个知识库"出现两个事实源。
- **让普通用户也能建共享库**。不采用的原因：与"共享库只有管理员能建、默认限本部门"的边界直接冲突。
- **旧库按创建者部门自动收紧**。更安全。不采用的原因：这是破坏性变更，原本能看到某个库的人会突然看不到，需要迁移、通知并处理"创建者没有部门"的库。
- **个人库不开放图谱能力**。改动小。不采用的原因：使用者明确要求个人库包含抽取配置、图谱构建与图谱页签；抽取用的模型列表接口本就是 `get_required_user`，不存在模型可用性的阻塞。
- **把个人库限定为非 Milvus 类型**。不采用的原因：图谱能力只支持 Milvus 知识库，限定类型会让同一张需求里的两条要求互相排斥。
- **保留前端 `apiAdmin*` 传输层，靠后端 403 兜底**。改动最小。不采用的原因：`checkAdminPermission()` 在发请求之前抛错，普通用户会拿到一个前端伪造的"需要管理员权限"、而不是真实的授权结果；留着它等于保留第二套授权事实源。

## 后果

- 按库判定的知识库路由全部对任意已登录且绑定部门的用户可达，安全性完全落在 `resolve_knowledge_base_permission` 上。任何漏判都等于越权读取。
- **现有知识库立即对所有登录用户可见**。`scope` 列的默认值与迁移语句都是 `shared`，这些库的 `share_config` 仍是原有的全局范围，所以本轮明确接受的取舍生效：在管理员逐个收紧到部门之前，它们仍然是公开的。需要一次性通知与操作指引。
- 非创建者的普通用户在共享库上最多 `READ`：即使命中 `manage_scope`，角色上限也把结果夹回 `READ`。
- 任何知识库的创建者（含普通角色）都拿到 `MANAGE`，这是所有者早返回产生的既有行为。业务上普通用户建不出共享库，所以"普通用户在共享库上最多 `READ`"只对**非创建者**成立。但创建者身份不由当前角色决定：从 `admin` 降级下来的用户、以及直接写 `created_by` 的种子/脚本调用者，在它们创建过的共享库上仍是 `MANAGE`。
- 超管在个人库上同样是 `MANAGE`；否则别人的个人库会从超管的库列表里消失、访问被拒。
- 普通用户可以触发 LLM 消耗：图谱抽取与构建、知识库评估，以及 `POST /generate-description` 这种无上限的单次 LLM 调用。本轮不引入配额、限流或按用户计费，记为部署前的运维义务。既有约束仍然生效：同一知识库同时只能有一个图谱任务，失败重试预算由 `GRAPH_EXTRACTION_MAX_ATTEMPTS` 固定。
- 创建者没有部门的共享库回落为"仅创建者 + 超管"，不会静默变成全局可见。
- 个人库不写 `manage_scope`：将来若要支持"委托他人管理自己的知识库"，需要显式写入它，并先想清楚"管理范围必须包含在读取范围内"这条校验的适用面。
- 个人库的**定向分享**（把库分享给指定用户）是已交付的能力：`resolve_knowledge_base_permission` 承认个人库上的 `user` 级 `read_scope`，创建者通过 API 就能写入。但界面上的用户选择器仍要求管理员，创建者**无法从界面**添加这些分享——要让某个用户读到自己的个人库，当前需要用管理员账号在界面上操作，或由创建者直接调用接口。这是当前交付的限制，不是权限判定或后端的缺口。
- 个人库的图谱数据仍会进入检索答案：图谱检索融合链路（`_retrieve_graph_chunks`）对非管理员开放且不做文件级过滤，本轮不改；个人库是单人资产，不构成跨部门泄漏。
- 前端不再是可达性边界：普通用户能进知识库页并调用 KB 级接口，失败的调用以后端 403 呈现。`apiAdmin*` 传输层只剩管理级调用。
- 未指定 `kb_id` 的通用上传与抓取入口、跨库全量视图（知识库列表、导图库列表、全局统计）与评估数据集接口仍限管理员。
- 迁移：`scope` 列默认 `shared`，读取路径把 NULL 视为历史语义，现有行不改变语义。

## 验证

`Passed` 表示命令实际执行且结果已核对；`Not run` 表示未执行并说明原因。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 普通用户可以创建知识库，且创建出来的是个人库 | 普通用户建库仍 403；或建出来的是共享库 | 创建路由 + 服务层范围收敛 | `pytest test/integration/api/test_knowledge_router.py`（带凭证执行）→ **45 passed**，含 `test_plain_user_can_create_personal_knowledge_base`：POST 建库 200、`scope == "personal"`、`can_manage is True`、出现在 `/databases/accessible` | `test_plain_user_cannot_create_shared_knowledge_base`：同一请求带 `scope = "shared"` → 403（不是静默降级成 200） | Passed |
| 个人库创建者拿到 MANAGE，不受角色上限夹取 | 创建者只有 READ，传不了文档、配不了抽取器 | `resolve_knowledge_base_permission` | `test_personal_knowledge_base_owner_gets_manage_even_for_plain_user_role`（`pytest test/unit/permissions test/unit/knowledge` → **228 passed**）；HTTP 层由上一行的 `can_manage is True` 覆盖 | 该断言**不是** personal 分支的守卫：把实现退化成"只走通用路径"时创建者仍得 `MANAGE`（同一次 RED 里另 20 条已通过）——这正是创建者的 MANAGE 不依赖该分支的证据 | Passed |
| 个人库不被自己的 share_config 撑大：非创建者看不到 | 个人库的 `read_scope` 被误设成 `global` / `department` 后，任意登录用户都能读到 | personal 分支的 fail-closed 兜底 | 同一套件内 `test_personal_knowledge_base_stranger_gets_none`；`read_scope` 被**误设**这一失败面只有单元层覆盖——`test_personal_knowledge_base_is_not_widened_by_global_or_department_scope[global\|department]`。HTTP 层另由 `test_personal_knowledge_base_owner_can_use_graph_endpoints` 覆盖"非创建者看不到正常配置的个人库"这一子句：另一个普通用户读同一库（`GET /databases/{kb_id}`）与调 `graph-build/index` 均为 403 | 把实现退化成"只走通用路径"后，参数化护栏用例变红为 `read` vs 期望 `none`（RED 实测：断言失败，不是异常、不是偶发） | Passed |
| 超管在别人的个人库上仍是 MANAGE | 别人的个人库从超管列表里消失、访问被拒 | personal 分支之前的超管放行 | 同一套件内 `test_personal_knowledge_base_superadmin_gets_manage` | 修复前实测 `超管 + 非创建者的个人库 => none`（RED 断言失败 `none` vs `manage`） | Passed |
| 被分享者只读 | 被分享者能上传 / 删除 / 改配置 | `READ` 与 `MANAGE` 的区分 | `test_personal_knowledge_base_sharee_gets_read`（单元：user 级命中 → `READ`）；同一套件里 user 级未列出该用户 → `NONE` | —（未单独执行"把已分享用户从 `read_scope.user_uids` 移除"的对照；负向方向由陌生人与 `global` / `department` 护栏两条用例覆盖） | Passed（单元层）；HTTP 层的"被分享者写被拒"未单独执行，见下方 `Not run` |
| 非创建者的普通用户在共享库上仍无写权限 | 放开底座后普通用户能在共享库里上传 / 删除 / 改配置 | `resolve_resource_permission` 的角色上限 + `require_knowledge_base_manage` | `test_shared_knowledge_base_non_owner_plain_user_with_matching_scope_gets_read`（单元）；HTTP 层 `test_document_search_is_gated_by_knowledge_base_scope`、`test_knowledge_routes_enforce_permissions` 对普通非创建者断言 403（45 passed 内；这些断言在真实链路上 403，且 `get_required_user` 只产生 401/400、不产生 403，所以拒绝只可能来自权限判定。`test_mindmap_permissions` 不在此列：它的 `/mindmap/databases` 断言来自 `get_admin_user`，是**角色** 403，与按库判定的权限解析器无关） | —（"去掉角色上限后该断言变红"的负向对照没有单独执行） | Passed |
| 共享库默认限本部门 | 管理员新建的共享库默认全局可见，别的部门能看 | 创建逻辑的默认 scope | `test_admin_created_shared_knowledge_base_defaults_to_own_department`（带凭证的集成套件，45 passed 内）：`scope == "shared"`、`read_scope.access_level == "department"` 且 `department_ids` 非空；服务层由 `test/unit/knowledge/test_knowledge_share_config_defaults.py` 三条用例覆盖（personal 默认、shared 带部门、shared 无部门 fail-closed） | 无部门时实测 `read_scope = user(创建者)` 而非 `global`（无凭证探针第 6 条：`admin/no-department -> scope=shared, access_level=user`） | Passed |
| 现有知识库按 `global` 语义对普通用户可见 | 放开后旧库对普通用户仍不可见（与决策不符） | 权限判定 + `get_databases_by_user` | `test_legacy_global_knowledge_base_stays_visible_to_plain_user`（带凭证的集成套件，45 passed 内）：显式 `global` `share_config` 的库出现在普通用户的 `/databases/accessible` 内；实测现有库 `scope` 全为 `shared`、`created_by` 为裸 uid 字符串，能正确匹配 | — | Passed |
| 个人库创建者可以使用图谱能力 | 抽取配置 / 构建 / 图谱查询仍要求管理员 | 图谱路由的两个适配器 + `graph_router.post_subgraph` | `test_personal_knowledge_base_owner_can_use_graph_endpoints`（带凭证的集成套件，45 passed 内）：创建者 `POST graph-build/config` 非 403、`POST /api/graph/subgraph` 200 | 同一用例断言另一名非创建者普通用户对该库 `graph-build/index` 与读详情都是 403 | Passed |
| 知识库列表按新边界过滤 | 个人库出现在他人列表里；或普通用户看不到本该可见的共享库 | `get_databases_by_user` | `test_plain_user_can_create_personal_knowledge_base`（个人库出现在自己的列表）、`test_share_config_filters_accessible_databases[department\|user]`（命中范围者可见）、`test_legacy_global_knowledge_base_stays_visible_to_plain_user`；列表项的 `scope` / `can_manage` 由 `/databases/accessible` 返回 | `test_share_config_filters_accessible_databases` 断言另一部门的用户列表里**不含**该库 | Passed |
| 前端按权限而非角色渲染，且不再做客户端授权预判 | 普通用户看不到建库入口 / 用不了自己的库；或前端仍以角色挡住真实有权用户 | `web/src/apis/knowledge_api.js`、`web/src/views/ExtensionsView.vue`、`web/src/router/index.js`、`web/src/views/DataBaseView.vue`、`web/src/components/knowledge/DatabaseCreateFlowModal.vue` | `web/test/unit/knowledge_scope_access.test.js` 5 passed（RED → GREEN + 参数位变异探针）、`web/test/unit/database_list_partition.test.js`、`web/test/unit/database_create_flow.test.js`；`pnpm run test:unit` → **411 passed / 0 failed**（2026-10-07 复核）；`pnpm run lint:check` 退出 0；`pnpm run build` 成功 | 把 `downloadDocument` 退回 `apiGet(url, {}, 'blob')` → 参数位守卫变红（实测：blob 会被当成 JSON 解析）；把 `get_knowledge_chunk_presets` 退回 `Depends(get_admin_user)` → 底座归属单测 `FAILED`（实测） | Passed（静态契约与单元测试）；真实页面 `Not run` |
| 后端按库判定，管理级调用仍限管理员 | 某条管理级路由随底座一并放开 | `require_knowledge_base_read` / `require_knowledge_base_manage` 的底座归属 | `test_knowledge_route_dependency_class`（7 个参数：4 个静态 / 辅助端点必须 `get_required_user`，3 个全量视图必须 `get_admin_user`）、`test_generic_upload_channel_without_kb_id_stays_admin_only`；`grep -c 'apiAdmin' web/src/apis/knowledge_api.js` 66 → 17（= 3 条 import + 14 个管理级调用点），48 处 KB 级已转换 | 变异探针：把 `get_knowledge_chunk_presets` 退回 `get_admin_user` → 归属单测 `FAILED`（实测），随后恢复 | Passed |
| 本次改动没有破坏既有知识库行为 | 全量回归出现新增失败 | 全部 | `pytest test/unit -m "not slow" --ignore test/unit/services/test_run_worker.py` → **2441 passed, 58 skipped, 0 failed**；`pytest test/integration/knowledge` → **3 passed**；整套 `pytest test/integration` → `6 failed, 370 passed, 6 skipped, 6 errors`，失败与错误集合与特性基线 `38aa28b` **逐一相同**（通过数 362 → 370） | 基线对照即负向案例：任何新增失败都会让集合与基线不再逐一相同 | Passed |

`Not run` 的原因说明：

- **真实页面与视觉验证全部未执行**：本次执行环境没有浏览器自动化能力。未验证的具体内容是普通账号进入知识库页后的两段列表布局、创建表单里被禁用的范围单选项、`.extension-section-header` 的 scoped 渲染、以及个人库图谱入口的呈现。`pnpm run build` 通过只证明 SFC 能编译，不构成可视断言。
- **「非创建者看不到个人库」的多账号列表中可见性没有单独断言**：HTTP 层已执行的是"另一个普通用户读该库详情与调图谱写接口都得 403"；没有单独断言"用户 B 的 `/databases/accessible` 里不含用户 A 的个人库"（同类断言只存在于按 `share_config` 部门 / 用户过滤的用例里，那些库是 `shared`）。
- **「被分享者只读」的 HTTP 层写被拒未单独执行**：单元层已覆盖权限解析值为 `READ`，但没有一条真实 HTTP 用例把个人库定向分享给另一个用户后再验证其写入 403。
- **路由层 503 映射不属于本记录**：观察到"Neo4j 不可用 → 503"需要把 Neo4j 停掉，本次未做；该收敛项属于图谱查询特性，已记在 `implemented/2026-10-06-graph-advanced-filter.md` 的 `Not run` 中。
