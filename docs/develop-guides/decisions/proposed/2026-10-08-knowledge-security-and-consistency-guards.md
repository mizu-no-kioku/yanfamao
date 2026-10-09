# 知识库、图谱与连接器的安全和一致性边界

状态：proposed
类型：bug-fix
Owner：backend/package/yuxi/knowledge/base.py

## 问题

知识库当前存在几组相互独立但风险相近的缺口：文件身份空间过小且创建路径允许覆盖；MinIO URL 没有验证对象是否属于当前知识库；共享库创建者可绕过角色上限，未知角色又默认获得读权限；普通用户可创建任务却无法访问任务中心。外部连接器可继承服务端凭据或绕过已有 SSRF 防护；配置脱敏只处理顶层；Milvus 在读取时执行破坏性集合迁移，失败索引的部分结果仍可见；查询与图谱抽取没有可靠的服务端资源上限；图谱的检查后执行存在竞态，空路径会丢弃孤立实体；Agent 工具把一次可见性快照当成持续授权；RAG 失败返回 HTTP 200 并暴露内部异常，正则文档查找可被灾难性回溯占满 CPU；未实现的导出接口以内部错误结束。

这些缺口分别破坏租户隔离、凭据边界、派生索引一致性、资源可用性和 API 失败契约。前端限制、日志级别或调用方自律都不能作为这些主张的 Owner。

## 提案

### 实现方案

- 文件身份由上传路径时间哈希改为 UUID，并由 repository 的只创建写入拒绝任何已存在身份；文件与 chunk 的读取继续以 `kb_id` 约束。
- 所有文档 MinIO 路径在入库、下载和删除前校验 bucket、`kb_id` 前缀与路径段；未知角色 fail-closed，共享库创建者受当前角色上限约束，个人库创建者语义保持不变。
- 任务列表、详情、取消与删除以 payload 的 `operator_id` 作为普通用户可见性 Owner，管理员保留全局视图。
- Dify 使用连接阶段阻断私网和 DNS rebinding 的 HTTP transport；所有个人 Notion 库必须提供独立 token，缓存键包含凭据指纹。连接器配置日志只记录非敏感身份字段。
- 敏感参数脱敏递归作用于嵌套对象；图谱状态只返回脱敏后的公开配置。
- Milvus 集合不兼容时显式失败，不在读取或普通索引入口删除旧集合；索引失败清理本次可见的部分 chunk，检索来源只承认 `indexed` 文件。
- 查询参数由知识库实现声明的配置在 service 边界统一校验类型、枚举与上下限；动态 options（如当前可用的重排序模型）允许实现声明的空值或默认值表示「未选择」，只拒绝目录外的具体值。图谱抽取同时限制单任务 worker 数和进程级 LLM 并发。
- 图谱的构建、重置与修复在检查和副作用之间持有 PostgreSQL advisory lock；概览和起点查询用 `OPTIONAL MATCH` 并显式保留空路径的锚点实体。
- RAG 路由用 4xx/5xx 表达失败并只向客户端返回稳定错误；正则查找限制模式数量、长度和整体匹配时间。
- Agent 每次知识库读操作重新查询当前可见资源；未实现的导出能力明确返回 501。

PostgreSQL 继续拥有文件状态、权限和知识库配置；Milvus 仍是可重建的派生索引。此次不引入新表、兼容状态机或自动破坏性迁移。

## 替代方案

- 只扩大短哈希长度并保留 upsert。不采用：即使碰撞概率降低，身份冲突仍会静默改写既有资源。
- 对 Dify 只在保存时解析 DNS。不采用：保存到请求之间可以发生 DNS rebinding，必须在连接时校验并连接同一地址。
- 索引失败时保留已成功批次。不采用：失败文件会形成可查询的半成品；在没有 generation schema 前，清理半成品比伪装成功更安全。
- 自动删除并重建旧 Milvus 集合。不采用：读取路径不能拥有不可逆迁移，且没有完成重建和原子切换的证据。
- 仅依赖 UI 的 min/max 与并发输入框。不采用：API、Agent 和持久化配置都可绕过 UI。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 新文件身份不可通过碰撞覆盖既有记录 | 相同输入或身份冲突改写另一条文件记录 | `kb_utils.py`、`knowledge_file_repository.py` | 相关 unit + PostgreSQL integration | 固定时间连续生成 ID；插入已存在 ID | Not run |
| 文档对象与权限始终归属当前用户和知识库 | 跨库 URL 被下载/删除；降权创建者继续管理；未知角色可读 | MinIO 路径校验、permission resolver | unit + HTTP integration | 外库 URL；普通角色共享库 owner；未知角色 | Not run |
| 普通用户只能看到和操作自己的任务 | 任务中心不可用或泄露他人任务 | task repository/service/router | unit + PostgreSQL integration | 查询他人 task id；列表另一 operator | Not run |
| 连接器不访问私网且普通用户不继承全局 Notion token | 私网连接成功；个人库借用服务端 token | Dify/Notion 实现与 manager | connector unit + API integration | loopback/RFC1918；普通用户空 token | Not run |
| 凭据不出现在日志和只读响应 | 嵌套 api_key 或 token 被返回/记录 | security utility、响应装配、图谱 service | unit + HTTP integration | 嵌套 `model_params.api_key` | Not run |
| 集合不兼容和索引失败不会伪装为可查询成功 | 读取删除集合；错误文件的部分 chunk 被返回 | Milvus executor、file repository | Milvus unit + integration | 旧 schema；第二批写入失败 | Not run |
| 查询和图谱抽取服从后端资源上限 | 超大 top-k/max-nodes/并发进入执行器 | manager、graph extractor/service | unit + HTTP integration | `10**9`、NaN、超限 concurrency | Not run |
| 动态 select 的默认空值不阻断检索 | 默认查询参数被判非法，Milvus 知识库无法检索或重排 | `knowledge/base.py::validate_query_params` | unit + 对现有库回放校验 | `reranker_model` 为默认空值必须通过；目录外具体值仍被拒绝 | Not run |
| 图谱变更串行化且孤立实体可见 | reset/build 竞态；空路径丢节点 | Task advisory lock、graph Cypher builder | unit + PostgreSQL/Neo4j integration | 锁内检查与入队；无关系实体 | Not run |
| RAG 失败不伪装成成功且正则搜索有硬上限 | HTTP 200 带 failed；异常泄露；ReDoS | router、base search helper | unit + HTTP integration | 执行器异常；超时 regex | Not run |
| 权限撤销对下一次 Agent 工具调用立即生效 | context 缓存继续授权 | knowledge Agent backend/toolkit | toolkit unit | 首次可见后撤销再调用 | Not run |
| 导出未实现时返回稳定 501 | `None` 进入文件检查并返回 500 | knowledge base + router | router unit/integration | 调用未实现 executor | Not run |

## 风险

- 历史持久化的非法查询参数会在下次查询时显式失败，需要管理员重新保存合法值。
- 旧 Milvus schema 不再自动重建；部署者必须通过受控重建流程恢复查询，但旧集合数据不会被读请求删除。
- 此前依赖全局 Notion token 的个人库必须通过更新接口补充独立 token 后再查询；资源清理不依赖查询凭据，仍允许删除，创建和更新均要求独立 token；既有配置仍需后续运维审计。
- 索引失败清理是当前 schema 下的最小一致性方案，不能保留旧的成功 generation；要实现无损切换需单独设计版本化索引。


## 当前交付验证范围

验收矩阵的结果表示每行完整证据组合是否收敛；unit 已通过，尚缺的高层验证使这些组合保持 `Not run`。下述命令记录区分已执行层级与未执行范围。

本轮运行 `python scripts/verify_engineering_contracts.py` 与 `python -m unittest scripts.test_verify_engineering_contracts`，契约检查和 63 项脚本测试通过。`docker compose exec api uv run --no-sync --group test pytest test/integration/api/test_knowledge_router.py test/integration/api/test_task_router.py test/integration/services/test_durable_task_repository.py -q` 返回 21 passed、51 skipped；通过项覆盖真实 PostgreSQL repository，HTTP 项因未配置 TEST_USERNAME/TEST_PASSWORD 跳过。尚未执行真实 Neo4j 孤立节点结果检查、advisory lock 并发竞态和外部连接器真实网络验证，矩阵中的高层级命令表示需要的证据，不表示已经执行通过。

API 容器普通用户无法同步全局 site-packages，因此使用 `uv run --no-sync` 运行镜像内已安装的测试依赖。权限修复的 unit 不能替代上述未执行的真实链路验证。


本轮提交前完整回归：`docker compose exec api uv run --no-sync --group test pytest test/unit -m "not slow" -q --timeout=45 --timeout-method=signal` 返回 2582 passed、58 skipped，耗时 70.29 秒。首次未设置单项超时的完整 unit 在 worker 测试处超过五分钟未收敛，已终止该测试进程后执行上述重跑。`--no-sync` 保留镜像已安装依赖，未改变 site-packages 权限。独立 Reviewer 已复核完整 diff 与授权回归，未发现剩余阻断问题。
