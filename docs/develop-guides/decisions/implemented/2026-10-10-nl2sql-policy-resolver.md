# 业务问数权限解析器

状态：implemented
类型：feature
Owner：backend/package/yuxi/nl2sql/policy_resolver.py

## 问题

Yuxi 问数只开放项目与项目成员两张表，而 `QueryPolicy` 的全部字段由调用方传入：业务侧的身份、角色、菜单、资源与行范围没有解析来源。业务库自带 RBAC 表（`t_user`、`t_staff`、`t_enterprise_role`、`t_enterprise_menu`、`tb_user_enterprise_role_tenant`、`t_project`、`t_project_user` 等），但**存在表不等于授权语义完整**：角色—菜单关系可达 38089 行，单个角色平均关联上百个菜单，另有 41 个有效角色完全没有菜单。按角色—菜单 JOIN 结果计数会把一个角色放大成上百个角色，按角色本身计数又会漏掉无菜单角色，两个方向都会算错「这个人有几个角色」。问数因此无法在「谁、在哪个租户、能看到哪些表的哪些行」上给出可信答案。

## 决策

### 实现方案

`nl2sql/policy_resolver.py` 把窄接口 `BusinessPrincipal(user_id, tenant_id | None)` 解析成完整 `QueryPolicy`，链路为身份 → 功能门 → 资源码 → 行范围 → 策略。解析只依赖 `nl2sql/directory.py` 的 `BusinessDirectory` 协议；生产实现 `MySqlBusinessDirectory` 查业务库，集成测试实现查隔离测试库。

- **身份链**：业务用户必须存在且有效；未显式给出租户时按用户归属推导，归属多个租户则拒绝而不猜测；该租户下必须恰有一个有效员工。每个拒绝都带 `nl2sql/errors.py` 的可诊断码（`IDENTITY_USER_MISSING`、`TENANT_AMBIGUOUS`、`IDENTITY_STAFF_AMBIGUOUS`、`ROLE_MISSING`、`FUNCTION_GATE_DENIED`、`NO_PROJECT_ACCESS` 等），便于审计与排障。
- **角色基数**：只对 `tb_user_enterprise_role_tenant` 的**原始关系行**计数，不对角色—菜单 JOIN 结果计数；0 条拒绝，超过 1 条按违反唯一约束的数据异常拒绝。不实现多角色并集。
- **功能门**：菜单 ID **253 / 254 / 238 任意命中一个**即通过，不继承父菜单。判定按 ID 而非 CODE——同码异 ID 的菜单可以冒充 CODE。`resource_policy.yaml` 的 `menu_ids` 承担资源映射与配置一致性守卫，`build_policy` 校验其取值集合必须恰为 `{253, 254, 238}`。
- **资源与字段三层求交**：代码硬上限（`table_policy.py` 的 `TABLE_UPPER_BOUNDS`、`PERMANENTLY_DENIED_TABLES`、`PERMANENTLY_DENIED_COLUMNS`）∩ YAML/RBAC 授权（`resource_policy.yaml` 的资源码到表/列映射）∩ 环境变量层（绑定中的 `allowed_tables`、`allowed_columns`，以及 `YUXI_BUSINESS_DB_MAX_ROWS`）。**环境变量层只能收缩**：越界项被剔除，剔除后没有可用表或字段即按配置错误拒绝，不静默放行。行数上限同样三层求交：`effective_max_rows = min(代码上限, YAML/RBAC 上限, 环境变量上限)`，环境变量大于代码上限时被压回。YAML 越界同样报错而非被裁剪——静默裁剪会让配置看起来生效、实际不同。
- **状态列为守卫内部列**：`TENANT_ID`、`OPERATER_STATUS`、`ENABLE_STATUS` 保留在策略的 `table_columns` 中供守卫注入条件，但不进入模型可见列 `selectable_columns`，模型显式引用即 `COLUMN_NOT_ALLOWED`。业务库用 9999 表示启用，模型按常识猜 `=1` 会把结果清空。
- **行范围**：租户 + 有效状态 + 已授权项目 + 员工本人。项目范围每次由员工在业务库中的真实成员关系推导，不在配置里静态声明；成员表的员工范围由代码固定为当前员工，**不接受任何环境变量取消**——要查看其他项目成员必须走 RBAC/YAML 资源授权。

首期只开放 `t_project` 与 `t_project_user`。其余资源码（`HR_*`、`FINANCE_READ`、`APPLICATION_READ`、`RISK_READ`）在 YAML 中留空位；`table_policy.has_complete_row_rule` 要求租户列、项目列、状态列齐全才允许开放，缺任一项的表默认拒绝。

`services/business_database_service.py` 把解析器接进服务层。`BusinessDatabaseSettings` 从环境变量读取连接参数、`YUXI_BUSINESS_DB_MAX_ROWS` 与 `uid → business_user_id`（+ 可选 `tenant_id`）的可信绑定；**员工编号不静态声明**，由解析器按当前租户动态推导，员工归属变化时无需改配置。绑定中的 `allowed_tables`、`allowed_columns` 构成环境变量收紧层，两者清洗后为空即按配置错误拒绝。`allow_project_member_rows` 曾用于放开成员行范围，现已删除并在配置中出现即报错：环境变量层不得取消 `STAFF_ID` 行范围。未绑定的 uid 直接拒绝，模型、问答参数与显示名都不能推导身份。

`agents/toolkits/business_database/` 暴露三个 `buildin` 工具：`list_business_database_tables`、`describe_business_database_table`、`query_business_database`。当前 Yuxi `uid` 由服务端 `ToolRuntime` 提供，模型与前端不能提交 `uid`、`tenant_id` 或 `staff_id`。模型可见 Schema 由策略的 `selectable_columns` 裁剪，服务层不另推一套口径——否则会出现「守卫按策略校验、Schema 按另一套展示」。

**解析、SQL 校验与最终执行共用同一条只读连接、同一个只读事务**：`mysql_query_scope` 建立连接并 `SET SESSION TRANSACTION READ ONLY` 后 `START TRANSACTION READ ONLY`，`directory_on_connection` 把目录绑到这条连接（目录不关闭它），`SQLSecurityGuard.secure` 校验改写后的语句由仓库在同一连接上执行。策略所依据的权限快照与查询所依据的数据快照因此是同一份。

**每次工具调用都新建连接与事务并重新解析权限，不缓存策略**，所以已提交的撤权在下一次调用必然可见。`list`、`describe`、`query` 三者都走同一条 `_prepared` 路径，都会重新读取当前权限。

功能门的三条菜单规则保留为试运行规则。**238 的业务语义不作推断**：它在业务库中的字面含义是项目列表内的 AI 生成，是否等价于通用问数授权未经业务确认。本记录只主张「238 当前是三条功能门菜单之一」，不主张它是项目资源权限。

## 替代方案

- **继续由调用方传入 `QueryPolicy`**：无法反映业务角色与菜单变化，授权事实没有 Owner。
- **另建一套 SQL 强制层**：会产生两个独立的安全实现，改一处漏一处。因此复用 `business_database_security` 的 `SQLSecurityGuard`，解析器只负责产出策略。
- **在业务库建 AI 数据资源表**：业务库账号只有 `SELECT`、无法建表，方案不可行。
- **用角色—菜单 JOIN 计数做角色基数**：会把单角色放大成上百个角色，使「>1 即拒绝」对每个有菜单角色的用户误触发；41 个无菜单的有效角色又会以相反方向被算成 0 个角色。
- **资源映射落 YAML 并以下限判定**：仅与候选表清单求交集不足以限制字段与行范围。因此上限由代码拥有，YAML 只能在清单内取子集，越界即报错。
- **只在模型提示词中要求加租户/项目条件**：提示词不能阻止错误或越权 SQL。
- **把解析结果缓存在进程内**：撤权会滞后到缓存失效。撤权测试将它作为反证：把解析结果缓存起来后用例报 `DID NOT RAISE`，说明该用例确实能感知「沿用旧策略」。
- **首期扩大数据覆盖面**：需要先为每张表定义字段上限、租户归属、项目关联与状态规则，会推迟权限闭环。不为扩大覆盖面先放表再补权限。

## 后果

部署必须提供只读 MySQL 账号，以及 `uid → business_user_id` 的服务端映射；未配置的工具不注册，未绑定、功能门未命中或无项目可访问的请求一律失败关闭。新增业务表前必须同时确认可见字段、租户列、项目关联列与状态规则——`table_policy.py` 即权限边界，改它必须走决策记录。

当前开放面仍是 `t_project` 与 `t_project_user` 两张表，没有扩大数据覆盖面。

未解决的限制，如实保留：

- **`ty1` 偶发答 0 的根因未定**。稳定重建环境后连续 40 次未复现，因此没有可归因的修复；该现象可能仍然存在。
- **连接池泄漏源头未定位**。全量测试曾在 pytest-asyncio 关闭事件循环的 teardown 阶段挂起约 50%，护栏后连续 6 次全量全部通过。`backend/test/conftest.py` 中的 guard 是**临时 workaround**，只消除泄漏后果、不修源头；定位到泄漏该池的测试后应当移除。
- **238 的业务语义未确认**。扩大用户范围或生产发布前须确认该菜单是否代表问数授权。
- **环境变量收紧层目前只在 `ty2` 上启用**，其余账号按 YAML 授权全量放行。
- **部门、本人、全部授权项目与字段脱敏均未实现**。首期只拒绝字段，不做脱敏；`PROJECT_AUTH_STATUS` 的观测值全为 0，其业务含义待确认，不等于确认未启用。

## 验证

以下均为 2026-10-12 在运行态（`.cache/gitee-integration`）实测。

**同事务重查与撤权生效**（此前的 `Not run` 项已转为已验证）：

- `backend/test/integration/nl2sql/test_query_scope_single_transaction.py` 覆盖「解析与最终 SQL 落在同一条只读连接、同一个只读事务」，并在**隔离测试库**（`yuxi-mysql-test-1` / `nl2sql_test`）用可写控制账号真实撤销角色后验证撤权边界：`test_revocation_is_denied_on_the_next_call` 证明授权时可查询、撤销角色后**下一次查询即被拒绝**（`ROLE_INACTIVE`），`test_restoring_the_role_restores_access` 证明恢复后重新可用。真实业务库 `r_d_test` 是只读的，无法在其中制造撤权，测试库缺失配置时用例 skip 且**不回退连接业务库**，不修改 `r_d_test`。

**RBAC 四场景**（`docs/develop-guides/rbac-business-database-test-matrix.md`）：

| 场景 | 预期 | 实测结果 |
|---|---|---|
| `ty1` | 允许问数，14 个项目 | `14`，项目名与库内逐条一致 |
| `ty2` | 按「任意命中一个 AI 菜单」允许问数，3 个项目 | `3`：`43346`、`测试B`、`测试c`；查 `GENERAL_BUDGET` 与整张 `t_project_user` 均被拒绝，且模型可见结构里没有该列 |
| `ty3` | 功能门拒绝 | `FUNCTION_GATE_DENIED`，可见菜单 ID 中没有 253/254/238，未触碰业务数据 |
| `ty4` | 功能门通过但无授权项目 | `NO_PROJECT_ACCESS` |

守卫内部列同轮验证：`TENANT_ID`、`OPERATER_STATUS` 不在模型可见列内，模型显式引用即 `COLUMN_NOT_ALLOWED`。

**全量测试**：2662 passed、58 skipped、0 failed（skip 来自未启用真实业务库与隔离测试库的集成用例）。

**冷启动与 worker 恢复**：冷启动全栈后三个业务数据库工具均正常注册并回答；worker 启动等待 PostgreSQL 就绪（`backend/test/unit/services/test_worker_database_readiness.py`），不再需要人工重启。

**静态检查**：`uv run ruff check` 与 `uv run ruff format --check` 覆盖本次新增 Python 文件并通过；`python scripts/verify_engineering_contracts.py` 通过。

未覆盖：真实业务库的 Schema 漂移、生产数据规模下的性能，以及 `ty1` 偶发答 0 的复现均仍为 Not run。

### 复审后的加固（2026-10-13，同一运行态）

针对 `gitee/main...b2df1df` 复审发现的问题做测试先行修复，每个缺陷都先有能在修复前失败的用例：

- **行数上限三层求交**：`YUXI_BUSINESS_DB_MAX_ROWS` 此前解析后从未被消费。现在 `effective_max_rows = min(代码上限 200, YAML/RBAC 上限, 环境变量上限)`，环境变量只能收紧。真实业务库实测（`ty1`）：未设置 → `limit=200, rows=14`；`=5` → `limit=5, rows=5`；`=1000` → `limit=200, rows=14`（被压回，未扩权）。
- **worker 异常分类按真实包装链判定**：`DBAPIError.orig` + `__cause__`/`__context__` 逐层展开，永久错误优先。真实 PostgreSQL 实测：密码错误 → `classification=permanent`、`attempts=1`、0.03s 内退出；库不存在 → `classification=permanent`；`postgres` 容器停止 → 启动 → 恢复期间依次记录 `gaierror`、`ConnectionRefusedError` 两次可重试告警，第 4 次尝试 `READY_OK`（8.2s），无需人工重启 worker。
- **配置层只能收紧**：`allowed_tables` 清洗后为空、`allowed_columns` 空表名/空字段名一律按配置错误拒绝，不再回退成「不限制」；`allow_project_member_rows` 已删除，出现即报错（环境变量层不得取消 `STAFF_ID` 行范围）。
- **拒绝文案**：返回给模型的只有稳定错误码 + 通用说明（`FUNCTION_GATE_DENIED: 当前角色没有 AI 问数功能权限…`），表名/列名/菜单 ID 只进服务端日志。四账号回归实测文案已不含内部标识。
- **状态列**：守卫改为按策略声明的 `status_scope_columns`（来自 `TableBound.status_column`）注入，不再硬编码 `OPERATER_STATUS`。
- **死代码**：删除无人调用的 `single_transaction_directory`，并修正指向它的过时注释与文档。

同轮门禁：全量 `test/unit` 2701 passed、58 skipped、0 failed；`test/integration/nl2sql` 20 passed、3 skipped；`ruff check` 与 `ruff format --check`（锁定 0.16.4）对改动文件通过；`scripts/verify_engineering_contracts.py` 通过。四账号回归 `ty1=14`、`ty2=3`（`GENERAL_BUDGET` → `COLUMN_NOT_ALLOWED`、`t_project_user` → `TABLE_NOT_ALLOWED`）、`ty3=FUNCTION_GATE_DENIED`、`ty4=NO_PROJECT_ACCESS`，与上表一致。
