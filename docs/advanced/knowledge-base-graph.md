# 知识导图与知识图谱

本页说明 Milvus 知识库的导图、示例问题和图谱运维。它面向管理员和需要查看结果的用户；文档导入和 API 见[文档导入与查询 API](./knowledge-base-api.md)，状态和存储边界见[知识库机制](../mechanisms/knowledge-base.md)。

## 知识导图

在知识库详情页的“知识导图”中生成或查看层次化导图。系统根据文件列表和元数据组织分类，最多使用 200 个文件；结果保存到知识库的 `mindmap` 字段，Agent 可以通过 `get_mindmap` 读取。

新增文件时可以执行增量更新；纯删除可以直接移除对应叶子节点，不需要再次调用模型。导图只说明文件的组织关系，不代表系统已经阅读或总结全部正文。要回答内容问题，仍需检索 chunk 或打开原文。

只读用户可以查看已经生成的导图。生成、增量更新和重置属于写操作，需要知识库管理权限。

## 示例问题

知识库详情页可以根据文件列表生成 `sample_questions`，供检索测试选择。示例问题适合快速检查入口是否可用，但不等于经过人工审核的评估基准。需要可比较的检索分数时，请使用[知识库评估](../intro/evaluation.md)并检查参考 chunk 和答案。

## 构建知识图谱

知识图谱只支持 Milvus 知识库。开始前确认：

- 文档已经解析并完成向量索引；
- 已在图谱配置中选择 LLM 抽取模型；
- 当前账号有知识库管理权限；
- Neo4j 和 Milvus 服务可访问。

在知识库详情页配置抽取器并提交构建任务。任务会从已索引的 chunk 中抽取实体和关系，随后写入结构数据和向量索引。详情页可以查看抽取、结构写入、向量索引进度和失败 chunk。

三类存储分别拥有不同事实：

| 存储 | 负责的事实 |
| --- | --- |
| PostgreSQL | chunk、抽取状态、图谱处理状态和任务信息 |
| Neo4j | 实体、关系和 chunk 关联 |
| Milvus | 实体与三元组的语义向量索引 |

检索时，系统可以把图谱实体/关系结果与文本 chunk 结果融合。图谱查看权限只要求知识库读取权限；配置抽取器、开始构建、重置和修复索引要求管理权限。

## 查询知识图谱

图谱页签默认展示该知识库的实体子图，也就是构建完成后看到的「全图」，受最大节点数约束。选定起点后，查询切换成以该起点为锚点；清空起点再查询就回到全图。

| 起点 | 终点 | 返回 |
| --- | --- | --- |
| 未填 | 未填 | 全图概览：以该知识库全部实体为种子，受最大节点数约束。进入图谱页时的默认视图 |
| 已填 | 未填 | 起点向外不超过层数跳的邻域子图 |
| 已填 | 已填 | 起点与终点之间不超过层数跳的路径子图 |

路径只沿实体之间的关系走，Chunk 节点不进入路径。内容溯源仍在详情面板与检索链路里，不受查询方式影响。

「高级筛选」按四类条件约束路径，**只要有任意一处不满足，整条路径就不返回**：

| 筛选 | 作用对象 | 匹配方式 |
| --- | --- | --- |
| 中间实体 | 路径上去掉起点与终点的实体 | 实体类型属于所选集 |
| 实体属性 | 同上 | 属性名与属性值同时相等 |
| 实体间关系 | 路径上的每条关系 | 关系类型属于所选集 |
| 关系属性 | 路径上的每条关系 | `text` 按子串包含（忽略大小写）；`extractor_type` 精确相等 |

起点与终点自身不受实体类条件约束。同一个属性名只能出现在一行里，可选的属性名会在用过之后从下拉中消失；同一属性名的两行是「且」关系，很容易构造出永远为空的组合，所以不提供。

层数上限是 3，超过上限的请求被拒绝，不会被悄悄截断。结果还要受最大节点数限制，发生截断时界面会提示「结果已按上限截断」，以便和「确实没有匹配路径」区分。

筛选的可选项来自图谱里已有的取值。**升级前已经建好、但尚未回填属性的图谱**在按实体属性筛选时会明确报错并提示先回填；在知识库详情页的图谱面板提交「图谱筛选属性回填」任务即可，回填幂等、可重复执行。Neo4j 不可用时接口返回错误而不是空图。

## 失败和修复

图谱任务把可序列化 payload 持久化到 PostgreSQL，再由 ARQ worker 领取。Task 行的 attempt owner 与 lease 拒绝重复执行和迟到写入；所有 Durable Task 在 owner 中断或 lease 过期后都会明确失败，不在未知 Neo4j 或 Milvus 副作用上自动重放。任务失败或取消后：

1. 先保留任务错误和失败 chunk；
2. 检查 PostgreSQL 的 chunk 与处理状态；
3. 检查 Neo4j 的实体、关系和引用；
4. 检查 Milvus 的图向量索引；
5. 再决定重试、修复向量索引或重置图谱。

Durable Task 的 `success` 只表示 worker 已完成编排，不能单独证明三类存储已经一致。

## API 入口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/knowledge/databases/{kb_id}/mindmap` | 读取导图 |
| `GET` | `/api/knowledge/databases/{kb_id}/mindmap/diff` | 检查导图文件变更 |
| `POST` | `/api/knowledge/databases/{kb_id}/mindmap/generate` | 生成或更新导图 |
| `GET` | `/api/knowledge/databases/{kb_id}/graph-build/status` | 查看图谱构建状态 |
| `POST` | `/api/knowledge/databases/{kb_id}/graph-build/config` | 保存抽取配置 |
| `POST` | `/api/knowledge/databases/{kb_id}/graph-build/index` | 提交图谱构建 |
| `POST` | `/api/knowledge/databases/{kb_id}/graph-build/reset` | 重置图谱状态 |
| `POST` | `/api/knowledge/databases/{kb_id}/graph-build/reconcile` | 修复图向量索引 |
| `POST` | `/api/knowledge/databases/{kb_id}/graph-build/backfill-attributes` | 回填筛选属性投影 |
| `POST` | `/api/graph/subgraph` | 按起点、终点与四类筛选查询子图 |
| `GET` | `/api/graph/filter-options` | 读取筛选可用的类型与属性枚举 |
| `GET` | `/api/graph/entities` | 按名称搜索实体 |

图谱查询接口只有 `POST`。查询子图曾经是 `GET /api/graph/subgraph`（按关键词取邻域并返回 Chunk 节点），该接口和「在图谱画布上排除 Chunk 节点」这个开关都已随本页描述的新查询方式移除。

接口字段和权限以部署实例的 Swagger 页面为准。只读用户可以调用读取接口，不能通过改 URL 绕过管理权限。

## 源码和测试

- [知识库路由](https://github.com/xerrors/Yuxi/blob/main/backend/server/routers/knowledge_router.py)
- [导图工具](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/knowledge/utils/mindmap_utils.py)
- [图谱服务](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/knowledge/graphs/milvus_graph_service.py)
- [知识库 unit tests](https://github.com/xerrors/Yuxi/tree/main/backend/test/unit/knowledge)
- [图谱和知识库 integration](https://github.com/xerrors/Yuxi/tree/main/backend/test/integration/api)
