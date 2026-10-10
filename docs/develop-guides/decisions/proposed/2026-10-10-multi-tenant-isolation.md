# 共享实例多租户隔离

状态：proposed
类型：architecture
Owner：backend/package/yuxi/storage/postgres/manager.py、backend/package/yuxi/permissions/resource_permission.py、backend/server/utils/auth_middleware.py

## 问题

Yuxi 当前是单租户实现：全部业务表没有租户维度，隔离只由 `departments` 与资源 `share_config`（`global` / `department` / `user`）在单实例内完成，`superadmin` 是全局万能角色。新的交付形态是 Yuxi 作为能力服务接入一个已有多租户、统一身份与权限体系的宿主平台，一个部署要同时承载多个宿主租户。

宿主平台负责身份认证、租户成员关系与角色声明，但**不能**因此把 Yuxi 的数据隔离外包给网关：网关覆盖不到内部调用、worker、定时任务、数据库误查询和外部存储访问。租户边界必须由 Yuxi 自己在认证、数据库、异步任务、缓存与外部存储上同时落实。

现有实现中已经有若干在单租户下不显眼、在多租户下直接成为越权或缺口的形状：

- 部门列表查询没有任何过滤（`DepartmentRepository.list_with_user_count`、`list_departments`），而 `GET /departments` 的底座是 `get_admin_user`，任何管理员都能看到全部部门名与人数；部门名唯一性判断 `exists_by_name` 同样是全局的。
- 删除部门时把用户迁移到硬编码的默认部门 `id = 1`，并以此判断默认部门不可删。
- `_normalize_scope` 只校验共享范围的形状，不校验 `department_ids` / `user_uids` 是否属于同一租户，而 `scope_matches` 直接按这两个集合匹配。写入路径因此可以把资源定向分享给另一租户的主体。
- `resolve_resource_permission` 与 `resolve_knowledge_base_permission` 对 `superadmin` 直接返回 `MANAGE`，是全局放行而非租户内放行。
- 模型缓存是单个全局 Redis 键 `yuxi:model_cache`，其值序列化后的 `ModelInfo` **包含 provider 的明文 `api_key`**；`resolve_model_spec` 按 `provider_id:model_id` 全局查找。配置项缓存 `yuxi:config_option:*` 同样全局。这两处正是本轮要改成「每租户一份」的对象。
- 应用以 `postgres` 连接数据库，该角色 `rolsuper = t` 且 `rolbypassrls = t`。PostgreSQL 的 RLS 对 superuser 与 `BYPASSRLS` 角色**永远不生效**，`FORCE ROW LEVEL SECURITY` 也管不住 superuser。因此在当前连接方式下，即使给表启用了 RLS 也等于没有。
- LangGraph checkpoint 三表由 `AsyncPostgresSaver.setup()` 自建，不在 ORM 模型中，走独立的 psycopg 连接池，SQLAlchemy 事务钩子覆盖不到。
- `GET /chat/thread/{thread_id}/audits` 挂在 `get_superadmin_user` 上，可读取任意线程的审计。
- MinIO `public` 桶全局匿名可读（桶策略 `Principal: {"AWS": ["*"]}`，nginx 直接暴露 `/minio/public/`），对象路径首段 `{user.id}` 可枚举。

这些缺口的共同点是：它们的 Owner 都在服务端代码与数据库策略里。前端限制、网关过滤或调用方自律都不能作为这些主张的 Owner，因此本记录不把它们列为缓解手段。

## 提案

### 实现方案

**租户模型**：新增 `tenants`（`id`、`external_tenant_id`、`issuer`、`name`、`code`、`default_department_id`、`created_at`），唯一约束 `(issuer, external_tenant_id)`——`issuer` 不可省，否则不同 IdP 的租户标识会碰撞。租户按首次出现幂等创建；默认部门与租户互相引用，必须在同一事务内分步建立并回填，不能依赖裸 ID 约定。

**归属根与身份列**：`departments`、`users`、`api_keys`、`knowledge_bases`、`agents`、`skills` 加 `tenant_id`；`tasks`、`cli_auth_sessions`、`agent_run_requests`、`agent_runs`、`scheduled_agent_jobs`、`operation_logs` 同样视为归属根，因为它们可被独立检索、排队、调度或授权。`model_providers`、`mcp_servers`、`config_options` 加**可空** `tenant_id`（见下）。纯派生表（`knowledge_files`、`messages`、`tool_calls` 等）通过不可为空的外键链归属，不冗余列；一旦某张派生表出现独立查询、批量处理或异步消费路径，则升级为归属根而不是放宽约束。

`users` **不把现有 `uid` 重解释为宿主 subject**：`uid` 参与 Workspace 路径派生（`md5(uid || ':' || owner_thread_id)`）与既有外键、历史数据引用，改语义会破坏这些链路。改为新增 `external_issuer`、`external_subject`、`identity_source`（`local` / `oidc` / `host_sso`），宿主身份的唯一约束建在 `(tenant_id, external_issuer, external_subject)` 上。

**唯一约束**：部门名改为 `(tenant_id, name)`；API Key 等业务唯一性按租户重建；配置表用部分唯一索引（或 `NULLS NOT DISTINCT`）分别约束平台默认与租户配置——否则 `tenant_id IS NULL` 的多个平台默认项在唯一索引下互不相等，可以重复插入。

**认证与 JIT**：嵌入形态信任宿主 SSO Token，`tenant_id`、主体、部门与角色全部取自**验签后**的 Claim；Token 的 `issuer`、`audience`、签名算法与 JWKS 轮换行为接入前固化。客户端通过普通 Header 自报的租户只用于一致性校验，冲突即拒绝，不得覆盖 Token。独立形态保留本地登录，初始化时建立默认租户与本地管理员；两种形态由**服务端固定配置**决定，不允许请求级切换。首次请求在同一事务内按 `(issuer, external_tenant_id)` 与 `(tenant_id, external_issuer, external_subject)` 幂等创建租户与用户，依赖唯一约束与 upsert 而非「先查后插」，并同步部门与角色（可变显示名不作身份键）。

API Key 仍存在「尚未知道租户却需要读取凭证」的引导问题（宿主 SSO 只消掉了交互式登录的那一份）。首选 `SECURITY DEFINER` 函数：只接受 Key 哈希、只返回认证所需的最小主体信息。若保留 `BYPASSRLS` 角色，则必须独立连接池与独立 repository、只按精确哈希查询、只授 `users` / `api_keys` / `cli_auth_sessions` 的必要凭证列、写范围仅限 `last_used_at` 之类的显式字段，并以测试证明普通业务代码无法取得该连接。`get_required_user` 不再强制所有用户必须有部门，需要部门的业务改用独立依赖。

**权限语义**：`tenant_id` 决定资源边界，角色只决定租户内权限。`resource_permission.py` 中两处 `superadmin` 全局放行改为租户内 `MANAGE`；`admin` 即租户管理员。租户管理员的「管理一切」**不**扩展到读取不属于管理目标的私有会话与 Workspace 内容——这要求权限层能区分「业务资源」与「私有内容」，是本记录引入的新维度，与跨租户隔离是两件事，可分阶段落地。`_normalize_scope` 保持纯结构规范化；`department_ids` / `user_uids` 的同租户归属必须由数据库查询验证，并在写入授权关系的同一事务内完成，目标不存在、跨租户或混租户一律拒绝。

**数据库边界**：三类角色——迁移 owner 仅用于 DDL 与运维；`yuxi_app` 为运行时业务连接，非 superuser、无 `BYPASSRLS`、且不是业务表 owner（这一条才是 RLS 生效的真正保证，`FORCE ROW LEVEL SECURITY` 只是防「运行时被跑成 owner」的冗余保险）；认证角色见上。归属根策略按 `tenant_id` 直接比较，派生表用 `EXISTS` 回父表；`USING` 与 `WITH CHECK` 同时定义以阻止跨租户写入或搬移。每个业务事务执行等价于 `SET LOCAL app.tenant_id = '<validated>'` 的设置——`contextvar` 只用于进程内传播，不能代替会话设置；`SET LOCAL` 随事务结束自动失效，连接归还池前不残留上下文。租户上下文解析在未设置、为空或格式错误时必须返回不可匹配结果或明确失败。

`pg_manager` 拆分为迁移、租户运行时、系统/认证三类入口，普通 repository 默认只能取得租户运行时会话。

**异步与缓存**：ARQ 任务与定时任务的 payload 显式携带 `tenant_id`，但 worker 不信任 payload——消费时回查持久化的 Request / Run / Task，校验两者一致后再设置事务上下文。所有幂等键、取消键、事件流与缓存键纳入租户命名空间，其中 `yuxi:model_cache` 与 `yuxi:config_option:*` 优先级最高（前者含明文密钥）。LangGraph checkpoint 三表**不**纳入 RLS：thread 创建时记录所有者与租户，读写、恢复、删除前校验归属，key/namespace 含租户维度，并补跨租户恢复的负向测试。这是明确的应用层安全债务。

**外部存储**：Milvus collection、Neo4j 标签与 MinIO 对象键均已以 `kb_id` 为隔离单位，且全仓没有跨知识库的检索或图查询路径，因此只要 `knowledge_bases` 是归属根且 KB 访问的租户校验严密，这三者是传递性隔离的，**不额外加租户维度**。要正视的是它们自身零权限校验（向量层完全信任入参 `kb_id`，集合不存在时静默创建），所以要求落在「KB 层校验必须严密」而不是「给向量库加租户」。Workspace 路径按 `uid` 分且 `uid` 全局唯一，路径结构不改。MinIO `public` 桶当前只含头像、用户图片与智能体图标（知识库内容在私有 bucket），不构成本轮阻断项，但同时确立硬约束：**多租户生产环境禁止把敏感内容写入 `public` 桶**。

**每租户系统配置**：`model_providers` / `mcp_servers` / `config_options` 解析顺序为「租户配置 → 平台默认 → 不可用」，fail-closed。仅靠缺失记录无法表达「租户显式禁用平台默认」，因此需要 `enabled` / `disabled` 或 tombstone 语义：租户显式禁用后停止回退。RLS 在配置表上读放宽（本租户 + `NULL`）、写收紧（只能写自己的）；平台默认只由独立运维路径修改。Secret 返回必须脱敏。

**落地顺序**：先冻结 Token / 角色 / API Key 契约并盘点全部表、队列键与独立连接（阶段 0）；再 schema 与兼容迁移（阶段 1）；认证与 JIT（阶段 2）；repository 显式租户边界与权限语义（阶段 3）；RLS 与连接角色（阶段 4）；异步与 checkpoint（阶段 5）；外部存储（阶段 6）；灰度与越权验证（阶段 7）。租户内私有内容的可见性细分作为独立需求另行设计，不阻塞阶段 0–4。

## 替代方案

- **每租户独立部署**。隔离最强、改动最少，但运维、升级与资源成本最高。租户数量少或数据高度敏感时反而应优先考虑；本记录面向的是「一套部署承载多个租户」这一明确诉求，因此不采用，但保留为敏感租户的兜底选项。
- **把 `department` 当作 `tenant`**。改动看似小，但会把组织内授权与租户安全边界混为一谈，无法表达租户级配置、管理员与数据生命周期。不采用。
- **只依赖宿主网关传递与过滤**。覆盖不到内部调用、worker、定时任务、数据库误查询与外部存储访问。不采用。
- **只做应用层过滤，不启用 RLS**。实现简单，但 36 张表加后台任务与裸 SQL，漏一处就是静默的跨租户读取且没有兜底。不采用。
- **给 Milvus / Neo4j 额外加租户维度**。两层都以 `kb_id` 为隔离单位且不存在跨库路径，加维度要改 collection 命名、标签体系与全部查询，换来的是防住一个当前不存在的路径。不采用；改为要求 KB 层租户校验严密。
- **给 Workspace 路径加租户根**。路径已按全局唯一的 `uid` 分区，属传递性安全。不采用；改为保留「路径在文件系统边界校验」这一既有要求。
- **把 MinIO `public` 桶列为本轮阻断项**。桶内当前只有头像类内容，知识库内容在私有 bucket，匿名可读的实际暴露面有限；但原则成立，故降级为硬约束「禁止写入敏感内容」并记为已知共享面，待做租户白标时一并处理。
- **保留 `users.uid` 作为宿主 subject 或改写其语义**。会破坏 Workspace 路径派生、既有外键与历史引用。不采用。
- **把 `BYPASSRLS` 角色作为认证引导首选**。可行但比 `SECURITY DEFINER` 函数宽，且一旦泄漏就是通用 RLS 绕过通道。降为次选。
- **两套认证语义用运行时开关并存**。两套权限语义共存意味着两边都得测，漏一边即越权；形态改由服务端部署配置固定，不允许请求级降级。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| `yuxi_app` 未设置租户上下文时受保护表返回 0 行或明确失败 | 未设上下文时返回全表 | RLS 策略与 `pg_manager` 会话设置 | 真实 PostgreSQL 集成测试 | 不执行 `SET LOCAL` 直接查询业务表 | Not run |
| 设置租户 A 后只能读写 A 的数据，且不能把 A 的记录写成 B | 跨租户读、跨租户写、搬移归属 | RLS `USING` + `WITH CHECK` | 集成测试 + 直接 SQL / ORM join / 子查询 / 批量操作四种入口 | 以 A 的上下文更新 B 的行 | Not run |
| 运行时账号不是 superuser、无 `BYPASSRLS`、不是受保护表 owner | 配置退化后 RLS 静默失效 | 连接角色与 compose 配置 | 基础设施护栏测试断言角色属性 | 用 owner 或 superuser 跑运行时 | Not run |
| 宿主 Token 的租户、角色、部门是唯一身份来源 | Header 自报租户生效；错误 issuer/audience/签名被接受 | Token 校验与 JIT | 认证负向测试 | 伪造 `X-Tenant-ID`；错误签名 | Not run |
| 同一 subject 在不同租户下不合并为同一身份，并发首登不产生重复租户或用户 | 身份串租；并发首登建出两条绑定 | `users` 唯一约束与 upsert | 并发集成测试 | 两租户同名用户；同时发起的首次请求 | Not run |
| API Key 绑定自身租户与用户，请求租户只能做一致性校验 | Key 被用于其他租户；Key 覆盖自身租户 | API Key repository 与认证引导 | 集成测试 + 连接隔离测试 | 用 A 的 Key 带 B 的租户 | Not run |
| 跨租户授权被拒：`department_ids` / `user_uids` 必须同租户 | 把资源分享给另一租户的主体 | `_normalize_scope` 写入校验 | 单元 + HTTP 集成 | 跨租户 uid；混租户名单 | Not run |
| 租户管理员在本租户内可管业务资源，但读不到他人私有会话与 Workspace | 越权读取私有内容；或管理员连业务资源也管不了 | `resource_permission.py` | 单元 + HTTP 集成 | 管理员读他人 thread / workspace | Not run |
| worker 与定时任务回查持久化记录校验租户一致 | 伪造 payload 造成串租；任务无上下文而失败 | 任务入队与消费两侧 | 集成测试 | 篡改 payload 租户；缺失租户 | Not run |
| `yuxi:model_cache` 与 `yuxi:config_option:*` 按租户隔离，且不跨租户解析 provider | 租户解析到他人 provider 或密钥 | 缓存键构造与模型解析 | 单元 + Redis 集成 | 两租户同名 `provider_id` | Not run |
| checkpoint 三表在应用层完成 thread 归属校验 | 跨租户恢复 thread | thread 创建与读取路径 | 集成测试 | 以他人 thread_id 读取或恢复 | Not run |
| 部门列表、部门名唯一性、默认部门均按租户收敛 | 跨租户看到部门；重名被拒；删部门迁错租户 | `DepartmentRepository` 与 `auth_dept_router` | 单元 + HTTP 集成 | 另一租户同名部门；删除本租户部门 | Not run |
| 租户配置可表达「显式禁用平台默认」 | 禁用后仍回退到平台默认 | 配置解析与 tombstone | 单元 + 集成 | 租户写入 disabled 后再解析 | Not run |
| 敏感路由的认证、租户与资源依赖不发生重分类退化 | 新增或改动路由用错依赖 | 路由归属测试 | 仿既有依赖归属测试形状 | 把租户路由退回 `get_superadmin_user` | Not run |
| 单租户链路与独立部署形态不回归 | 现有功能因租户改造不可用 | 全部 | 既有 unit / integration 套件 | — | Not run |

只有上表全部通过，才能宣称具备共享实例多租户能力。当前全部为 `Not run`：本记录是提案，上述能力尚未实现。

## 风险

- **历史数据回填错误**导致资源归属到错误租户；回填必须在副本演练并保留前向与回滚脚本。
- **独立连接与后台任务漏设租户上下文**：表现为不可用或泄漏，两者都会在灰度期集中出现；需要缺失上下文的告警而不是静默降级。
- **认证引导通道权限过大**成为通用 RLS 绕过通道；`SECURITY DEFINER` 为首选即为此。
- **只完成 PostgreSQL 隔离而遗漏文件、缓存、向量或图谱**：Milvus / Neo4j 靠 `kb_id` 传递性隔离，一旦 KB 层校验出现缺口，这两层没有任何自防御。
- **checkpoint 三表始终是 RLS 覆盖之外的债务**：它依赖应用层校验，属于已知边界而非已完成项。
- **`yuxi:model_cache` 含明文密钥**：在完成租户隔离前，任何能读 Redis 的主体可获得全部租户的 provider 密钥。
- **租户内私有内容的可见性细分**是本记录引入的新维度，若与跨租户隔离同期实施会显著放大范围；建议按落地顺序分离。
- 若外部存储隔离未完成，回退手段应是退回单租户部署，而不是关闭授权检查维持服务。
