# 业务问数权限解析器

状态：proposed
类型：feature
Owner：backend/package/yuxi/nl2sql/policy_resolver.py

## 问题

业务问数需要从业务用户、租户、员工、角色和有效菜单推导查询策略。首期只开放项目与项目成员两表，表、字段和行权限上限由代码维护，YAML 越界配置显式失败。

## 提案

本阶段交付解析器及隔离 MySQL 测试设施，复用现有 `business_database_security.QueryPolicy` 和 `BusinessDatabaseSettings`，使用现有 `YUXI_BUSINESS_DB_*` 配置，不引入第二套 SQL 强制层或部署配置。

角色基数按原始关系表计算；同用户同租户必须恰有一个有效角色。三个指定 AI 菜单任意命中的规则仅作为试运行规则，生产菜单语义仍待业务确认。本模块尚未接入运行工具，不能作为完成问数功能的声明。

## 替代方案

继续使用静态绑定生成 QueryPolicy 无法反映业务角色与菜单变化。移入另一份 SQL 强制层会产生两个独立安全实现的维护成本，因此采用业务权限解析器并复用现有强制层。

## 风险

Yuxi 授权与业务授权双点装配、执行前在同一新短只读事务中重新解析权限、引擎内嵌、单一问数工具及权限 trace 留待下一阶段。当前 MySQL directory 每次查询独立连接，多步解析不保证共同快照，不能直接用作生产执行前授权边界。

隔离测试库只使用合成数据；应用账号只读，控制账号用于准备数据。真实业务库验收需要显式设置 `YUXI_NL2SQL_BUSINESS_TEST=1`，配置非法时失败，未启用时跳过，测试不修改业务数据。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 业务身份与菜单推导受限策略 | 身份歧义或资源越权 | nl2sql/policy_resolver.py | pytest test/unit/nl2sql test/integration/nl2sql | 无角色、无菜单、越界字段、跨租户项目 | Passed |
| 现有 SQL 强制层与配置接口仍可用 | 移植引入第二套实现 | services/business_database_security.py | 相关 services 与 repository 单元回归 | 写 SQL、未授权字段、非法配置 | Passed |
| 执行前同事务重查与完整问答 | 权限撤销窗口 | 后续引擎执行边界 | Plan 2 真实事务与 HTTP/worker 验收 | 并发撤权 | Not run |

相关测试位于 `backend/test/unit/nl2sql` 与 `backend/test/integration/nl2sql`，覆盖身份缺失及歧义、菜单功能门、角色 JOIN 放大、代码上限、跨租户项目隔离和配置失败。真实业务库只读对账不替代后续 HTTP/worker 问答及并发撤权验收。

隔离分支源码的上述测试与现有业务查询强制层、服务和 repository 回归合计 102 项通过，含显式启用的真实业务库只读对账。Ruff 检查与格式检查通过。未运行全量后端测试和完整问答验收。
