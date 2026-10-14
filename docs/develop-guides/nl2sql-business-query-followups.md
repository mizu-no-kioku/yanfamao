# 业务问数权限解析：后续事项与已知限制

本文追踪业务问数权限解析（`nl2sql`）尚未关闭的事项、每项的当前状态、语义 Owner 与关闭条件，供维护者在扩大数据覆盖面或生产发布前评估。

事实分工：[业务问数权限解析器](./decisions/implemented/2026-10-10-nl2sql-policy-resolver.md)拥有当前决定、替代方案与验证证据，本文不重复这些内容；[RBAC 业务问数测试账号矩阵](./rbac-business-database-test-matrix.md)拥有账号映射与问答口径。本文拥有的是「还剩什么、由谁负责、什么条件才算关闭」。事项关闭后从本文删除，并在对应决策记录或代码中落定结论。

## 当前边界

- 开放面只有 `t_project` 与 `t_project_user` 两张表，其余资源码在 `resource_policy.yaml` 中留空位。
- 行范围支持「当前租户 + 有效状态 + 本人项目 + 本人成员关系」。**查看其他项目成员需要由 RBAC/YAML 资源授权表达，当前未提供该授权**：`allow_project_member_rows` 已删除，配置中出现即报错，环境变量层无权取消 `STAFF_ID` 行范围。
- 部门范围、全部项目范围与字段脱敏均未实现。
- 环境变量层（绑定中的 `allowed_tables`、`allowed_columns` 与 `YUXI_BUSINESS_DB_MAX_ROWS`）只能收缩代码上限与 YAML 授权的交集，越界项按配置错误拒绝。

## 后续事项

| # | 事项 | 现象与影响 | 语义 Owner | 关闭条件 |
|---|---|---|---|---|
| 1 | `FORBIDDEN_TEXT` 按字面量匹配 | `_parse_single_query` 在归一化后的整条 SQL 上做子串匹配（`into outfile`、`for update`、`lock in share mode` 等）。字符串字面量含相同片段时被判 `FORBIDDEN_CLAUSE`，合法查询被拒 | `backend/package/yuxi/services/business_database_security.py` 的 `SQLSecurityGuard` | 判定改到 AST 层（`exp.Into`、锁定子句节点）；有一条用例证明字面量中的同名字符串不再触发拒绝 |
| 2 | `_verify_enforcement` 依赖序列化文本 | 二次解析后按 WHERE 文本做包含判断（如 `alias.tenant_id = 10`）。sqlglot 序列化格式变化会造成误报，且它核对的是自己的改写结果 | 同上 | 改为在 AST 上断言谓词节点；负向案例能恢复目标缺陷并在正确原因上失败 |
| 3 | 连接池泄漏源头未定位，测试护栏是临时措施 | 全量测试曾在 pytest-asyncio 关闭事件循环的 teardown 阶段挂起约 50%。`backend/test/conftest.py` 的护栏替换全局 `asyncio.runners.Runner.close`，只消除后果；它逐项防御式读取 `pg_manager` 的可选属性，并对同步用例一并生效，属全局副作用。加护栏后连续 6 次全量通过 | `backend/test/conftest.py` 的 `_close_leaked_pg_pool_before_loop_close` | 定位到泄漏该池的测试并修掉，随后移除护栏。护栏失效时测试显式失败，禁止改成静默降级 |
| 4 | `ty1` 偶发答 0 的根因未定 | 历史上观察到 `ty1` 偶发返回 0 个项目，会表现为「有权限却答 0」。稳定重建环境后连续 40 次未复现，因此没有可归因的修复；该现象可能仍然存在 | `backend/package/yuxi/nl2sql/policy_resolver.py` | 取得可重复的复现，定位到解析、连接或数据可见性中的具体环节 |
| 5 | `list_project_ids` 是无调用方的死代码 | `backend/package/yuxi/repositories/business_database_repository.py` 的 `list_project_ids(..., allow_all_projects)` 无调用方，仍带已删除的 `allow_all_projects` 形参，与「环境变量只能收紧」的当前语义冲突 | 同文件 | 删除该方法，或接入真实调用方并把参数改为当前语义 |
| 6 | 菜单 238 的业务语义未确认 | 238 在业务库中的字面含义是项目列表内的 AI 生成，它作为功能门菜单之一会让这部分用户获得问数权限 | `resource_policy.yaml` 的 `menu_ids` 与 `policy_resolver.resolve_function_gate` | 业务侧确认 238 是否等价于问数授权，确认后调整 `menu_ids` |
| 7 | 环境变量收紧层只在 `ty2` 上启用 | 其余账号按 YAML 授权全量放行，收紧路径在真实环境只被单账号覆盖 | `services/business_database_service.py` 的绑定解析 | 按部署需要为更多绑定声明 `allowed_tables`/`allowed_columns`，或明确记录不需要 |
| 8 | 字段脱敏与更宽行范围未实现 | 首期只拒绝字段，不做脱敏；部门范围与全部项目范围未实现；`PROJECT_AUTH_STATUS` 观测值全为 0，业务含义待确认 | `nl2sql/table_policy.py` 与 `resource_policy.yaml` | 明确脱敏口径与授权模型后实现 |

## 维护规则

- 每项必须写明语义 Owner 与可判定的关闭条件；没有关闭条件的事项不进本文。
- 关闭事项时同步更新对应决策记录或代码，并从本文删除，避免两处同时声明状态。
- 状态描述只写实际执行过的证据；未复现、未定位、未验证的结论按原样保留。
