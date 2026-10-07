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

- **任何已登录且绑定部门的账号都能读到本部门的用户名、uid、部门名**。这是本仓库第一次把用户
  目录开放给普通账号；范围限定在部门内，且不再输出 `role`。
- **本接口的部门过滤依赖一个外部不变量**：`get_required_user` 必须继续对 `department_id`
  为空/零的调用者失败。若该契约放宽为「允许无部门登录」，则
  `list_with_department(department_id=None)` 的语义是**不过滤**
  （`backend/package/yuxi/repositories/user_repository.py:154` 的 `if department_id is not None:`），
  届时处理函数必须补上 `None` 守卫。改动 `get_required_user` 的部门要求时，必须同步检查本接口。
- 个人库详情页不再展示全局/部门档位；共享库不受影响，仍三档。
- **只有表单这一处**不再请求部门列表。详情页视图自身另有一处
  `loadDepartments()`（`web/src/views/DataBaseInfoView.vue` 的 `onMounted`，用于只读展示部门名）
  仍会调用 `departmentApi.getDepartments()`；普通用户在客户端就被 `checkAdminPermission()` 挡下、
  错误被视图自己的 `catch` 吞掉。本次未收敛这一处（不在本次范围内），记为已知残留。
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
| 响应不再暴露 `role` | 放宽后把同事角色暴露给所有普通用户 | `UserAccessOption` | `::test_access_options_do_not_expose_role` | RED（实现前实测）：响应含 `role` | Passed |
| 前端传输层不再要求管理员 | 普通用户仍被前端 `checkAdminPermission()` 预判挡住 | `web/src/apis/auth_api.js` | `web/test/unit/knowledge_share_picker.test.js` 的源码断言（`apiGet`） | RED（实现前实测）：仍为 `apiAdminGet` | Passed |
| 个人库详情页只提供「指定人」 | 个人库展示全局/部门档，选后被后端静默忽略 | `DataBaseInfoView.vue` | 同上：挂载渲染 1 张卡片 + 源码断言 `allowed-access-levels` 三态表达式 | RED（实现前实测）：无该绑定、渲染 3 张卡片 | Passed |
| 不能提供部门级时表单不请求部门列表 | 表单挂载时发起一次在客户端就注定失败的部门请求 | `ShareConfigForm.vue` | 同上：`__departmentLoadCalls === 0`；选定部门级时为 1 | RED（实现前实测）：实际调用 1 次 | Passed |
| 本次改动没有破坏既有行为 | 全量回归出现新增失败 | 全部 | `pytest test/unit -m "not slow" --ignore test/unit/services/test_run_worker.py` → **2447 passed, 58 skipped, 0 failed**（基线 2441 + 新增 6）；`pnpm run test:unit` → **416 passed / 0 failed**（基线 414 + 新增 2）；`pnpm run lint:check` 退出 0；`pnpm run build` 成功；`python3 scripts/verify_engineering_contracts.py` 退出 0 | 基线对照即负向案例 | Passed |
| 真实浏览器中的可见性 | 端到端界面表现 | 前端 | `Not run` | — | Not run |

`Not run` 的原因说明：

- **真实页面与视觉验证未执行**：本次执行环境没有浏览器自动化能力。未验证的内容是普通账号进入
  知识库详情页后的实际呈现（选择器里出现本部门同事、个人库只显示「指定人」卡片）。
  `pnpm run build` 通过只证明 SFC 能编译，不构成可视断言。
- **带凭证的集成套件未执行**：`backend/test/integration/api/test_auth_router.py` 里已有
  `access-options` 的管理员用例（第 372-378 行，只断言 uid 与 department_id，不涉及被删的
  `role`），由控制器单独运行。
