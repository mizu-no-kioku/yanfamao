# 业务数据库接入

本文面向部署管理员，说明如何为已运行的研发猫配置外部业务 MySQL，并让 Agent 查询授权项目。前置条件是能够管理 Docker Compose、确认 Yuxi 用户 uid 与业务系统身份，并维护 MySQL 只读账号。当前接入范围是 `t_project` 和 `t_project_user`；数据库写入、任意表接入和自动同步业务登录身份不在支持范围内。

## 配置连接与身份

由数据库管理员准备只读账号，允许读取 `t_staff`、`t_project`、`t_project_user` 及这些基础表的 `information_schema` 元数据。`t_staff` 用于内部员工身份验证，模型可查询的表由服务端授权配置确定。数据库需要支持 repository 使用的 `SET SESSION TRANSACTION READ ONLY` 和 `MAX_EXECUTION_TIME`。外部 MySQL 由部署方提供，默认 Compose 不创建该业务数据库。

在部署使用的 `.env` 中填写以下配置。示例身份编号均为假数据，必须替换成经过管理员核验的真实绑定；uid 应使用平台用户的实际 uid。

```dotenv
YUXI_BUSINESS_DB_HOST=host.docker.internal
YUXI_BUSINESS_DB_PORT=3306
YUXI_BUSINESS_DB_USER=readonly_user
YUXI_BUSINESS_DB_PASSWORD=<readonly-password>
YUXI_BUSINESS_DB_NAME=business_database
YUXI_BUSINESS_DB_QUERY_TIMEOUT_MS=10000
YUXI_BUSINESS_DB_MAX_ROWS=200
YUXI_BUSINESS_DB_IDENTITIES_JSON={"<yuxi-uid>":{"business_user_id":17,"tenant_id":1}}
```

Docker Desktop 访问宿主机数据库可使用 `host.docker.internal`；远程数据库填写 worker 可访问的主机名。连接凭据和身份映射只由服务端维护，不放入 Agent 提示词、工具参数或沙盒，也不提交到 Git。

端口默认 `3306`，允许 `1–65535`；查询超时默认 `10000` 毫秒，允许 `100–60000`；`YUXI_BUSINESS_DB_MAX_ROWS` 默认 `200`，配置范围 `1–1000`，但**只能收紧不能扩权**：实际生效值为 `min(代码上限 200, YAML/RBAC 授权上限, 本变量)`，填大于代码上限的值不会放宽返回行数。repository 将超时用于连接和读写等待，并设置 MySQL 会话查询时间上限。行数上限限制返回结果，不限制聚合查询扫描的数据量。

身份配置只声明「这个 Yuxi 用户是谁」：`business_user_id` 必填，`tenant_id` 可省略——省略时由业务库按用户归属推导，用户归属多个租户时必须显式指定，不猜测。**员工编号（`staff_id`）不在此声明**，由解析器按当前租户从业务库动态推导，员工归属变化时无需改配置。租户、有效状态、授权项目与员工本人四条行范围全部由代码与 RBAC/YAML 资源授权裁决，**没有**环境变量可以取消它们。

`allowed_tables` 与 `allowed_columns` 是可选收紧项，只能缩小授权：越界项被剔除，清洗后为空或剔除后无可用表/字段时按配置错误拒绝，绝不回退成「不限制」。历史配置键 `staff_id`、`allow_all_projects`、`allow_project_member_rows` 已废弃；`allow_project_member_rows` 出现即报错，因为环境变量层不得取消 `STAFF_ID` 行范围——如需查看其他项目成员，必须由 RBAC/YAML 资源授权表达。

可选的 `allowed_columns` 按表进一步缩小字段范围，例如 `"allowed_columns":{"t_project":["ID","NAME"]}`；仅在真实 Schema 中存在这些字段时使用。未指定的表使用经过敏感字段过滤的 Schema。账号、密码、盐、身份证号、`PHONES`、指定附件 URL 以及以 `_URL`、`_ANNEX_ID` 结尾的字段被排除，字段白名单不能重新开放这些字段。

## 生效与验证

工具注册在进程启动时检查主机、用户、密码、数据库名和身份 JSON 是否均非空；缺少任一项时三个工具不注册。参数合法性、身份和数据库连接在工具调用时验证。更新环境变量需要重新创建 API 和 worker；新增依赖或首次部署包含该功能的代码时同时构建镜像：

```bash
docker compose up --build -d api worker
docker compose ps
```

按照[快速开始](../intro/quick-start.md)核对部署实际 API 端口和就绪接口。`ready` 表示平台就绪，业务 MySQL 的可用性需要以下真实工具查询确认。

1. 在[智能体配置](../agents/agents-config.md)中选择“列出授权业务表”“查看授权业务表结构”“查询业务数据库”，保存配置。
2. 使用有身份映射的用户在该 Agent 中提问：“列出我有权限查询的业务表，查看项目表结构，然后列出我可以访问的项目编号。”
3. 确认工具返回表清单、过滤后的字段和查询结果；查询结果包含 `fields`、`rows`、`row_count`、`limit`、`tables` 和 `scope`。将返回项目编号逐一核对为数据库管理员独立读取的当前员工有效项目集合中的成员；列表受行数上限截断。需要核对总量时另查询 `COUNT(DISTINCT ID)`，与独立读取的授权项目总数比较。
4. 用另一名绑定用户重复查询，独立核对其项目范围；使用未绑定用户查询应失败，查询未授权表或敏感字段也应被拒绝。确认同一个 Run 的最终回答与工具结果一致。

工具运行于既有 Agent worker，SQL 生成与结果解释由当前 Agent 完成；工具审计、SSE 和模型用量沿用平台链路。日志中的行数和耗时用于排障，授权结果以数据库记录和实际工具返回为准。

## 查询限制与排障

执行器使用 `sqlglot` 解析一条只读查询，校验表、字段和函数，注入租户、有效记录、项目与员工条件，并限制最外层返回行数。必须明确列名；`COUNT(*)` 可用，`SELECT *`、多语句、写入、跨库表名、会话变量、递归 CTE、外连接（LEFT/RIGHT/FULL JOIN）和最外层 OFFSET 被拒绝。函数支持范围以安全策略源码为准。MySQL 账号只读权限与只读会话共同约束实际执行。

- 工具列表没有业务数据库工具：检查必填变量是否完整，并重新创建 API 和 worker。
- “当前用户尚未绑定业务数据库身份”：核对映射键是否为当前 Yuxi uid。
- 员工身份无效：核对 `t_staff` 中租户、员工编号和 `OPERATER_STATUS > 0`。
- 没有可访问项目：核对有效项目及成员关联；不要靠放宽配置来掩盖错误绑定——行范围不接受环境变量放宽。
- 表或安全字段缺失：核对真实基础表、`TENANT_ID`、项目关联字段，以及员工范围需要的 `STAFF_ID`。
- 连接失败或超时：从 worker 的网络环境检查主机、端口、只读账号和 MySQL 会话能力；检查 `docker compose logs --tail 100 worker`，避免对外分享含用户数据的日志。

## 实现位置与验证范围

工具入口由[业务数据库工具包](https://github.com/mizu-no-kioku/yanfamao/blob/main/backend/package/yuxi/agents/toolkits/business_database/tools.py)拥有；身份与授权用例属于[业务数据库 service](https://github.com/mizu-no-kioku/yanfamao/blob/main/backend/package/yuxi/services/business_database_service.py)，SQL 校验与重写属于[安全策略](https://github.com/mizu-no-kioku/yanfamao/blob/main/backend/package/yuxi/services/business_database_security.py)，MySQL 查询属于[业务数据库 repository](https://github.com/mizu-no-kioku/yanfamao/blob/main/backend/package/yuxi/repositories/business_database_repository.py)。完整职责见仓库根 `ARCHITECTURE.md` 的“业务数据库问数边界”一节。配置信息由[环境变量模板](https://github.com/mizu-no-kioku/yanfamao/blob/main/.env.template)维护。源码链接指向本仓库的 GitHub 远程 `main`，需在包含这些新增文件的提交推送后可用；本地核对使用同名文件。

相关 unit 覆盖 repository、service、安全策略和工具入口，运行方式见[测试规范](../develop-guides/testing-guidelines.md)。已有实现决策中的验证记录见[业务数据库工具决策](../develop-guides/decisions/implemented/2026-10-09-business-database-tools.md)：记录包括模拟 MySQL 和 Agent 链路验证，也明确远程业务库连接失败、真实 Schema 与生产性能未验证的范围。接入自己的数据库时仍需执行上述独立结果核对。
