# 个人知识库的分享选择器数据源

状态：implemented
类型：feature
Owner：backend/server/routers/auth_router.py

## 问题

普通用户（`role = user`）可以拥有个人知识库，后端也承认个人库上的 `user` 级定向分享
（`resolve_knowledge_base_permission`），但创建者**无法从界面**完成分享。详情页「权限配置」→
「指定人」的选择器数据来自 `GET /api/auth/users/access-options`
（`web/src/components/ShareConfigForm.vue` 的 `loadUsers()`），该接口底座是 `get_admin_user`，
普通用户拿到 403；错误只被 `catch`/`console.error` 吞掉，选择器渲染成空（「暂无可选项」）。
同一表单里的 `departmentApi.getDepartments()` 也走管理员传输层（`apiAdminGet`），普通用户在
客户端就被 `checkAdminPermission()` 挡下。

另有一个呈现缺口：`DataBaseInfoView.vue` 渲染 `<ShareConfigForm>` 时不传 `allowed-access-levels`，
默认展示「全局共享 / 部门共享 / 指定人」三档。个人库在后端对非 `user` 级 `read_scope` 是
fail-closed 的（`global` / `department` 会被静默忽略），于是创建者可以选中「全局共享」、
以为分享成功，而实际无人可见。

## 决策

把用户目录接口的底座从 `get_admin_user` 放宽到 `get_required_user`：任何已登录且**已绑定部门**
的用户可见**本部门**的同事；`superadmin` 仍可见全部。可见范围只限部门内，与「非超管 admin 只看
本部门」的既有规则一致。响应里删除 `role` 字段——它在全仓没有消费者，放宽后没有必要把同事的角色
暴露给每个普通用户。

详情页对个人库只提供「指定人」一档，与建库流程 `DatabaseCreateFlowModal` 的
`shareAllowedAccessLevels` 一致。

### 实现方案

- **目录接口**（`backend/server/routers/auth_router.py` 的 `read_user_access_options`）：底座
  `Depends(get_admin_user)` → `Depends(get_required_user)`；分支逻辑不变（`superadmin` → 全部，
  否则 `list_with_department(department_id=current_user.department_id)`）；从 `UserAccessOption`
  模型与返回值里删掉 `role`。
- **闸门**：本接口**没有**处理函数级的 `department_id is None` 守卫。真正的闸门是
  `get_required_user`（`backend/server/utils/auth_middleware.py:115-119`）里的
  `if not user.department_id: raise 400`——它对所有调用者生效且不豁免超管。因此到达处理函数的
  调用者必然有部门，处理函数里再加一个 `None` 分支是**不可达代码**（见「替代方案」）。
- **前端传输层**（`web/src/apis/auth_api.js`）：`getUserAccessOptions` 从 `apiAdminGet` 换成
  `apiGet`（默认带认证头），管理员与普通用户共用一条路径。`DataBaseInfoView.vue` 自己的
  `loadUsers()` 走同一接口，随之对普通用户可用（把分享范围内的 uid 解析成显示名）。
- **详情页范围收敛**（`web/src/views/DataBaseInfoView.vue`）：`<ShareConfigForm>` 传
  `:allowed-access-levels="database.scope === 'personal' ? ['user'] : ['global', 'department', 'user']"`。
- **省掉无效请求**（`web/src/components/ShareConfigForm.vue` 的 `onMounted`）：当
  `normalizedAllowedAccessLevels` 不含 `department` 时不再调用 `loadDepartments()`。选定部门级时
  保持原行为，原有 `catch`/`console.error` 保留。
- **分工**：`get_required_user` 拥有「谁能调用」；`auth_router.read_user_access_options` 拥有
  「调用者能看谁」；前端只做呈现与传输，不是授权边界。

## 替代方案

- **在处理函数里加 `if current_user.role != "superadmin" and current_user.department_id is None: return []`（设计初稿）**。
  不采用的原因：**不可达**。`get_required_user` 已经对无部门调用者抛 400，且 `get_admin_user`
  本就依赖它，所以这个分支在真实链路上永远不会执行。要测它就必须 override 认证依赖、伪造一个
  生产环境到不了的调用者，正是负向案例要求避免的空转测试。它还会让人误以为「无部门账号可枚举
  全部用户」是一个真实威胁，从而在别处继续复制这种无覆盖代码。
  - **同类先例（反例）**：`backend/package/yuxi/services/identity_admin_service.py:48` 有一处同风格的
    `if not is_superadmin and effective_department_id is None: return 空分页`。它同样位于受
    `get_admin_user` 保护的 `read_users_page` 之下、同样不可达、同样没有单测。本记录把它记为
    「这类守卫容易悄悄变成无覆盖代码」的证据，免得后人「好心」把删掉的那个补回来。
- **保留 `role` 字段**。不采用的原因：全仓无消费者（前端只用
  `username/uid/department_id/department_name`），放宽后它只会把同事的角色暴露给每个普通用户。
- **个人库也保留三档，仅在保存时收敛**。不采用的原因：界面会让人选到后端会静默忽略的档位，
  制造「以为分享成功」的错误信任。
- **让普通用户看全部用户**。不采用的原因：与「非超管只看本部门」的既有边界冲突。

## 后果

- 本记录部分取代[个人知识库与共享知识库的可见性边界](./2026-10-06-personal-knowledge-base-scope.md)
  中「个人库仍无法从界面定向分享」的限制说明；该记录的其余结论继续有效。
- **任何已登录且绑定部门的账号都能读到本部门的用户名、uid、部门名**。这是本仓库第一次把用户
  目录开放给普通账号；范围限定在部门内，且不再输出 `role`。
- **本接口的部门过滤依赖一个外部不变量**：`get_required_user` 必须继续对 `department_id`
  为空/零的调用者失败。若该契约放宽为「允许无部门登录」，则
  `list_with_department(department_id=None)` 的语义是**不过滤**
  （`backend/package/yuxi/repositories/user_repository.py:154` 的 `if department_id is not None:`），
  届时处理函数必须补上 `None` 守卫。改动 `get_required_user` 的部门要求时，必须同步检查本接口。
- 个人库详情页不再展示全局/部门档位；共享库不受影响，仍三档。
- **只剩一次注定失败的多余请求，没有用户可见后果**。详情页视图自身另有一处 `loadDepartments()`
  （`web/src/views/DataBaseInfoView.vue` 的 `onMounted`）仍会调用 `departmentApi.getDepartments()`；
  普通用户在客户端就被 `checkAdminPermission()` 挡下、错误被视图自己的 `catch` 吞掉，
  `departments` 因此为空。但它**唯一的消费者**是只读展示分支
  `v-else-if="database.share_config"`（`:406-409`），而该分支**不可达**：编辑弹窗只在
  `canManageDatabase` 为真时打开（`showEditModal()` 的两个调用点 `:823`/`:1032` 都在
  `canManageDatabase` 门内），弹窗内 `canEditShareConfig` 恒等于 `canManageDatabase`（`:939`），
  所以它恒真、后面的只读分支是防御性死分支。结论：多余的失败请求确实存在，但**没有用户可见后果**
  （既不会显示错误的部门名，也不会显示「部门3」，因为那段展示不执行）。不因此放宽
  `departmentApi.getDepartments`（那是又一次授权面扩张，需单独决定）；那个死分支是否该删是另一件事，
  本次不动。
- **超过 1000 人的部门会被静默截断**：`limit` 默认且上限为 1000（`Query(1000, ge=1, le=1000)`），
  部门成员多于 1000 时选择器只显示前 1000 个。这是既有局限，本次不做服务端搜索，仅在
  `skip` / `limit` 上补了取值校验（负 `skip`、`limit<=0`、`limit>1000` 现在是 422）。
- 未认证 → 401；已登录但未绑定部门 → 400（与知识库路由一致）。

## 验证

`Passed` 表示命令实际执行且结果已核对；`Not run` 表示未执行并说明原因。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 普通用户（有部门）只能看到本部门用户，不含其他部门 | 普通用户仍 403，或看到其他部门的人 | `read_user_access_options` | `test/unit/routers/test_auth_router_access_options.py::test_plain_user_sees_only_own_department` | RED（实现前实测）：`403 {"detail":"需要管理员权限"}` | Passed |
| 非超管 `admin` 行为不变：仍只本部门 | 放宽底座时误改 admin 范围 | 同上 | `::test_admin_sees_only_own_department` | —（修改前后该用例均通过，说明改动没有收缩 admin 范围） | Passed |
| `superadmin` 仍可见全部用户 | 放宽时误把超管也收窄到本部门 | 同上 | `::test_superadmin_sees_all_users` | — | Passed |
| 无部门的调用者被拒绝（**这才是真正的闸门**） | 无部门账号可枚举全部用户 | `get_required_user` | `::test_caller_without_department_is_rejected`（断言 400） | 该用例覆盖 `get_current_user` 而非 `get_required_user`，让真实的 400 分支执行；删掉 `get_required_user` 的 `if not user.department_id` 会让它变红 | Passed |
| 未认证的调用者被拒绝 | 匿名可读用户目录 | `get_current_user` | `::test_unauthenticated_caller_is_rejected`（断言 401） | — | Passed |
| 响应不再暴露 `role` | 放宽后把同事角色暴露给所有普通用户 | `UserAccessOption`（响应模型） | **由响应模型保证**：FastAPI 按 `response_model` 序列化时会剥离未声明字段，所以断言恒真、**不构成授权证据**；测试 `::test_access_options_do_not_expose_role` 留作双保险 | RED 只来自实现前模型里声明了 `role` 这一点，不能证明「字典里也删了」 | Passed |
| 前端传输层不再要求管理员 | 普通用户仍被前端 `checkAdminPermission()` 预判挡住 | `web/src/apis/auth_api.js` | `web/test/unit/knowledge_share_picker.test.js` 的源码断言（`apiGet`） | RED（实现前实测）：仍为 `apiAdminGet` | Passed |
| 个人库详情页只提供「指定人」 | 个人库展示全局/部门档，选后被后端静默忽略 | `DataBaseInfoView.vue` | 同上：挂载渲染 1 张卡片 + 源码断言 `allowed-access-levels` 三态表达式 | RED（实现前实测）：无该绑定、渲染 3 张卡片 | Passed |
| 不能提供部门级时表单不请求部门列表 | 表单挂载时发起一次在客户端就注定失败的部门请求 | `ShareConfigForm.vue` | 同上：`__departmentLoadCalls === 0`；选定部门级时为 1 | RED（实现前实测）：实际调用 1 次 | Passed |
| 普通用户（有部门）经**真实 HTTP + PostgreSQL** 只拿到本部门候选 | 单元层换了内存 SQLite 与认证依赖，证明不了真实链路 | `read_user_access_options` + `get_required_user` | `test/integration/api/test_auth_router.py::test_access_options_are_department_scoped_for_plain_users`（真实登录凭证 + 真实库，由控制器执行） | 该用例断言另一部门用户的 uid **不在**结果里、且所有 `department_id` 等于调用者的部门 | Not run（已编写；运行需凭证，本次未执行） |
| `skip` / `limit` 的非法取值被拒 | 负 `skip` 传到 Postgres 变成 `OFFSET -5` → 500 | `read_user_access_options` 的 `Query` 约束 | `test/unit/routers/test_auth_router_access_options.py::test_invalid_pagination_is_rejected` | RED（实现前实测）：`{'skip': -1} -> 200`（而非 422） | Passed |
| 挂载后放宽为共享库时补拉部门列表 | 部门选择器永久「暂无可选项」，`validate()` 以「至少需要选择一个部门」挡住保存 | `ShareConfigForm.vue` 的 `ensureDepartmentsLoaded` | `web/test/unit/knowledge_share_picker.test.js`「挂载后放宽为共享库时补拉部门列表…」 | RED（实现前实测）：`__departmentLoadCalls` 实际为 0 | Passed |
| 详情页传**身份稳定**的 `allowedAccessLevels` | 自动刷新不断重新赋值 `store.database`，computed 重算若产出新数组，就会让 `watch(allowedAccessLevels)` 反复触发、**丢弃尚未保存的本地选择** | `DataBaseInfoView.vue` 的 `shareAllowedAccessLevels` | 同上「详情页与 API 传输层…」的源码断言：两档数组是**模块级常量**，computed 只做选择且体内无数组字面量 | RED（实现前实测）：`const SHARE_ACCESS_LEVELS_PERSONAL = ['user']` 不存在（当时字面量写在 computed 内） | Passed |
| 部门列表加载失败后可重试 | 首次失败即永久「暂无可选项」，`validate()` 挡住保存 | `ShareConfigForm.vue` 的 `departmentsRequested` 复位 | `knowledge_share_picker.test.js`「部门列表加载失败后会重试…」 | RED（实现前实测）：`__departmentLoadCalls` 停在 1（期望 2） | Passed |
| 本次改动没有破坏既有行为 | 全量回归出现新增失败 | 全部 | `pytest test/unit -m "not slow" --ignore test/unit/services/test_run_worker.py` → **2448 passed, 58 skipped, 0 failed**（相对 2441 基线 +7）；`pnpm run test:unit` → **419 passed / 0 failed**（相对 414 基线 +5）；`pnpm run lint:check` 退出 0；`pnpm run build` 成功；`python3 scripts/verify_engineering_contracts.py` 退出 0；`cd docs && node node_modules/vitepress/bin/vitepress.js build` 成功 | 基线对照即负向案例 | Passed |
| 真实浏览器中的可见性 | 端到端界面表现 | 前端 | `Not run` | — | Not run |

`Not run` 的原因说明：

- **真实页面与视觉验证未执行**：本次执行环境没有浏览器自动化能力。未验证的内容是普通账号进入
  知识库详情页后的实际呈现（选择器里出现本部门同事、个人库只显示「指定人」卡片）。
  `pnpm run build` 通过只证明 SFC 能编译，不构成可视断言。
- **带凭证的集成套件未执行**：本次为普通用户新增的
  `test_access_options_are_department_scoped_for_plain_users` 已写入
  `backend/test/integration/api/test_auth_router.py`，但**未运行**（需要真实登录凭证与 PostgreSQL，
  由控制器执行）；因此上表该行记为 `Not run`，**不能当作通过**。该文件既有的
  `access-options` 管理员用例（第 372-378 行）只断言 uid 与 department_id，不涉及被删的 `role`。
