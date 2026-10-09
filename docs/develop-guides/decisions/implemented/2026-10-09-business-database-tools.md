# Agent 通过受控工具查询业务数据库

状态：implemented
类型：architecture
Owner：backend/package/yuxi/services/business_database_service.py

## 问题

Yuxi1 提供知识库、知识图谱、Agent、SSE 和模型用量统计。通用 `mysql-reporter` Skill 允许模型直接提交 SQL，但没有绑定当前 Yuxi 用户与原业务系统租户、员工和项目范围；把旧 NL2SQL 类整体嵌入 Agent 还会重复调用模型，使 Token、提示词、重试和权限形成两套链路。

## 决策

### 实现方案

`business_database` 工具包在完整服务端配置存在时注册三个 `buildin` 工具：列出授权业务表、读取过滤后的单表 Schema、执行一条业务查询。工具只接受占位参数、表名或 SQL；当前 Yuxi `uid` 由 `ToolRuntime` 提供，模型和前端不能提交 `uid`、`tenant_id` 或 `staff_id`。

`business_database_service` 通过服务端 JSON 映射把 `uid` 解析为旧系统租户、员工和授权开关。MySQL repository 校验有效员工、读取基础表 Schema、解析项目成员范围并使用只读会话执行最终 SQL。`business_database_security` 使用 `sqlglot` 限制单条只读查询、表、列和函数，拒绝通配符、跨库名、会话变量、递归 CTE、外连接与最外层 OFFSET，注入租户、有效状态、项目 ID、普通用户员工 ID 和行数上限，再重新解析并验证最终 SQL。

首版只支持 `t_project` 与 `t_project_user`。`allow_all_projects` 控制是否从员工参与项目扩大到租户有效项目；`allow_project_member_rows` 控制是否允许读取授权项目中其他成员的关联行，两个开关都只存在于服务端映射。附件 URL、附件 ID、账号凭据、身份证号和电话字段不进入模型 Schema。SQL 生成和答案归纳由当前 Agent 完成，既有 TokenUsageMiddleware 和 Tool audit 继续拥有用量及工具审计事实。

## 替代方案

- 继续使用 `mysql-reporter` Skill：保留通用数据分析能力，但它不是原业务系统的权限边界。
- 为每个业务角色创建数据库账号或安全视图：数据库约束更强，当前缺少完整角色到表、列、行的可信映射，维护范围也更大。
- 把旧 NL2SQL 类整体作为一个工具：会产生主 Agent 与内部 SQLCoder 两次模型调用，分散 Token 统计、提示词和错误处理。
- 只在模型提示词中要求添加 `TENANT_ID`：提示词不能阻止错误或恶意 SQL 越权。

## 后果

部署必须提供只读 MySQL 账号和 Yuxi `uid` 到旧系统身份的服务端映射；未配置工具不注册，未映射用户或无有效项目的请求失败关闭。新增业务表前必须同时确认可见字段、租户列、项目关联列和人员范围，不能只扩充表名。接入原系统登录态后，经过服务端验证的 JWT claims 或持久身份绑定可以替换 JSON 映射，工具参数协议保持不变。

每次工具调用重新校验员工、Schema 和项目范围并创建短连接，优先保证授权实时性和失败关闭；高并发性能需要在真实环境压测后再决定是否加入带失效机制的缓存或连接复用。

## 验证

- `uv run --group test pytest test/unit/repositories/test_business_database_repository.py test/unit/services/test_business_database_security.py test/unit/services/test_business_database_service.py test/unit/toolkits/test_business_database_tools.py -q`：Windows 下 26 passed、工具模块 1 skipped；重建镜像后 Docker Linux 环境 28 passed。
- `uv run ruff check` 与 `uv run ruff format --check` 覆盖本次新增 Python 文件并通过。
- `uv lock --check` 通过，锁定 `sqlglot 30.21.0`。
- `python scripts/verify_engineering_contracts.py` 通过。
- 真实只读 MySQL 验证在握手阶段返回 `2013 Lost connection to MySQL server during query`，没有执行到身份或业务查询；数据库链路结果为 Not run。
- Docker `api` 与 `worker` 健康启动，三个业务数据库工具均成功注册。
- 本地 MySQL 8.4 模拟库完成真实 PyMySQL 验证：`ty1` 与 `ty2` 分别只返回本人项目，跨租户查询返回 0 行；写入、未授权表、敏感字段和 OFFSET 分别被数据库只读账号或应用策略拒绝。
- 远程只读 MySQL 当前不可连接，因此真实 Schema 漂移、真实身份映射和生产数据性能仍为 Not run。
- 前端登录态全链路已通过：Agent 使用 `ty1` 调用 `query_business_database` 返回本人项目，Worker 日志记录 `tenant_id=1`、`staff_id=13`、1 行结果和约 61ms 数据库耗时；跨租户查询返回 0 行，手机号、身份证号和工资请求未进入授权 Schema。
- SSE 流式回答与页面上下文用量更新正常，三轮测试后页面显示累计约 78.4K；正式 Token 计费仍按版本规划后续实现。


### 提交前授权回归

字段校验按实际物理字段和派生表投影解析，投影别名只在排序、分组和 HAVING 位置开放。负向测试先复现了同名别名与混合派生表绕过字段白名单，再验证执行器以 COLUMN_NOT_ALLOWED 拒绝。当前限制查询为内连接，外连接显式以 OUTER_JOIN_NOT_ALLOWED 拒绝，避免把权限条件追加到 WHERE 后改变外连接结果语义。此次提交未重跑远程 MySQL 或浏览器 Agent 链路；上述历史验证记录不作为本轮通过结果。


本轮提交前完整回归：`docker compose exec api uv run --no-sync --group test pytest test/unit -m "not slow" -q --timeout=45 --timeout-method=signal` 返回 2582 passed、58 skipped，耗时 70.29 秒。首次未设置单项超时的完整 unit 在 worker 测试处超过五分钟未收敛，已终止该测试进程后执行上述重跑。`--no-sync` 保留镜像已安装依赖，未改变 site-packages 权限。独立 Reviewer 已复核完整 diff 与授权回归，未发现剩余阻断问题。
