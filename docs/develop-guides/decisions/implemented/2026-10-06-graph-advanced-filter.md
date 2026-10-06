# 知识图谱起点/终点查询与四类路径筛选

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/graphs/milvus_graph_service.py

## 问题

Milvus 知识库的图谱查询只有一个关键词入口：关键词子串匹配出若干种子实体，再返回这些种子的邻域子图。调用方无法表达「从起点到终点」的路径意图，也无法按实体类型、实体属性、关系类型、关系属性约束返回的路径。

同一条链路上还有三处事实不对调用方可见：

- 查询、统计与标签读取都在 `except Exception` 之后返回空结果，Neo4j 不可用时使用者看到的是「没有数据」而不是失败。
- 路由声明层数上限为 5，service 内部再用 `min(max_depth, 3)` 静默截断，界面显示 1–5 而实际能力只有 3。
- 结果被截断到最大节点数时没有任何字段说明，无法区分「确实没有」与「被上限砍掉」。

根本约束来自数据形态：实体属性在 Neo4j 里是 `properties.attributes` 上的一个 JSON 字符串。Neo4j 的属性只支持标量与标量数组、不能存 map 数组，所以写入时用 `json.dumps` 落成字符串。这让「属性名与属性值同时相等」无法精确表达：Cypher 没有内置 JSON 解析函数，用子串匹配会在属性值包含属性名或分隔符时错配。

## 决策

图谱查询以起点为锚点、终点可选；四类筛选作为**路径约束**，不满足条件的路径整条不返回。

### 实现方案

**查询主链路**：`POST /api/graph/subgraph`（`backend/server/routers/graph_router.py`，请求模型 `GraphSubgraphQuery` 内联在路由文件里，与仓库其他路由一致）→ `MilvusGraphService.query_subgraph` → 纯函数 `_build_subgraph_cypher` 产出 Cypher → Neo4j → 复用 `_process_subgraph_record` / `_normalize_node` / `_normalize_edge` / `_finalize_subgraph_result` 归一化 → `{"nodes", "edges", "truncated"}`。

请求体：`kb_id`、可选的 `start` / `end`（各为 `{entity_id}`）、`max_depth`（1–3，默认 2）、`max_nodes`（默认 100，上限 1000）、可选的 `filters`。`filters` 四组：`intermediate_entities: [str]`、`entity_attributes: [{name, value}]`、`relation_types: [str]`、`relation_attributes: [{name, value}]`。关系属性的 `name` 只接受 `text` 与 `extractor_type`，其他取值返回 422；service 侧对未处理的名字同样显式失败，不静默丢弃条件后返回「看起来成功但没有筛选」的结果。

三种模式：

| `start` | `end` | 返回 |
| --- | --- | --- |
| 有 | 无 | 起点向外不超过 `max_depth` 跳的邻域子图 |
| 有 | 有 | 两点之间不超过 `max_depth` 跳的路径子图 |
| 无 | 无 | 概览：以该知识库全部实体为种子，受 `max_nodes` 约束。这是进入图谱页时的默认视图 |

界面在没有起点时按第三种模式加载，也就是进入图谱页、以及图谱构建完成后看到的「全图」；选中起点后查询才进入前两种。概览模式的语义相对被取代的 `GET /api/graph/subgraph` 已经改变：不再有 Chunk 节点、不再支持关键词种子，但没有任何关系的孤立实体也会作为种子并入结果（旧实现在种子没有邻域时会让整条结果变成空）。

**判定语义**：路径只沿 `:RELATION` 在 `:Entity` 之间走，Chunk 不进入路径。`中间实体` 与 `实体属性` 作用于路径上去掉起点与终点的实体，要求其中每个实体都满足；`实体间关系` 与 `关系属性` 作用于路径上每条 `:RELATION`，要求每条都满足；同类条件之间是 AND。判定集合为空时按真空真处理——只有一跳的路径不因「没有中间实体」被排除。`start == end` 与「只填终点」都在服务层抛 `ValueError`，路由映射为 400。匹配方式由属性名决定：实体属性精确相等；关系属性 `extractor_type` 精确相等、`text` 忽略大小写做子串包含。

**写入侧投影**：`_build_entity_records` 从 `attributes` 派生两个平行字符串数组 `attribute_names` / `attribute_values`，`cypher_merge_entity_mention` 一并写入；`attributes` JSON 字符串保留，详情面板仍在展示它。属性匹配写作 `any(i IN range(0, size(n.attribute_names) - 1) WHERE n.attribute_names[i] = a.name AND n.attribute_values[i] = a.value)`；没有属性或未回填时 `any` 为假，路径被排除。选择平行数组而不是拼成 `"名=值"` 字符串：属性值本身可能含分隔符，拼串会错配。

**枚举读取**：`GET /api/graph/filter-options` 返回四组枚举与 `attributes_ready`；`GET /api/graph/entities` 按名称子串搜索实体，供起点/终点选择器使用。原「搜索实体」的关键词能力迁移到后者，不是删除。

**回填与就绪门**：`POST /api/knowledge/databases/{kb_id}/graph-build/backfill-attributes` 复用 `knowledge_graph_index` 任务类型与 `backfill_attributes` 动作，从 PostgreSQL 镜像 `knowledge_graph_entities.attributes`（JSON 列）分批读、按 `entity_id` 批量 `UNWIND` 写回 Neo4j，幂等且可重复执行。分页在 SQL 里就排除空属性实体（`jsonb_array_length(attributes) > 0`）：空属性实体存成 `'[]'` 而不是 NULL，若只在取回之后过滤，一页全是空属性实体时该页会变成空列表、被调用方误判为「已取完」，后续实体永远回填不到。`attributes_ready` 由「是否存在 `attributes` 非空但 `attribute_names` 为空的实体」判定；为假时带 `entity_attributes` 的查询返回 409 并提示先回填，而不是返回可能为空的成功结果。

**失败边界**：`query_subgraph` 不吞异常。Neo4j 不可用时路由返回 503 与 `detail`，前端展示错误态。`get_labels` / `get_stats` 的吞异常行为本轮不改动。

**授权**：`POST /api/graph/subgraph` 的 `kb_id` 只在请求体里，所以它不能用其他图谱路由那种 `require_knowledge_base_read` 依赖——该依赖自身声明了 `kb_id` 参数，对请求体形态的路由会被 FastAPI 解析成**必填 query 参数**，既让只发请求体的调用方每次都拿到 422，也让授权校验读到的 `kb_id` 与实际执行的 `kb_id` 变成两个值。该路由改为 `Depends(get_admin_user)` 取用户，再以请求体的 `kb_id` 显式调用 `ensure_knowledge_base_permission(..., READ)`，以保证「校验谁」与「查谁」是同一个知识库。

**层数上限**：统一为 3，路由 `ge=1, le=3`，service 内不再二次截断，界面范围 1–3。响应新增 `truncated`，取值来自 Cypher 的 `size(graph_nodes) > $max_nodes OR size(paths) >= $path_limit OR size(edges) > $max_nodes * 2`——节点、路径、边三个上限都要覆盖，否则边被砍掉时用户看到的是一张缺少关系的图却没有任何提示。

**前端**：`KnowledgeGraphSection.vue` 的工具栏由关键词输入框改为起点/终点实体选择器、层数与查询按钮，并新增可折叠的高级筛选面板。请求体由 `web/src/utils/graph_query_payload.js` 的纯函数 `buildSubgraphPayload` 装配，组件只负责把状态喂给它。类型类条件用多选（集合内部 OR），属性类条件可增删行（跨行 AND），已被占用的属性名从其它行的下拉中移除（本行保留自己已选的值）。未选起点时按概览模式加载，因此进入图谱页与构建完成后默认看到全图。

筛选选项在 `watch(() => props.active, …, { immediate: true })` 里加载——它是首次挂载时唯一会执行的 watcher，`watch(kbId)` 与 `watch(isGraphSupported)` 都没有 `immediate`，不能在挂载路径上承担这个职责。某个分组没有可取的值时（例如图谱的实体没有抽取到属性、或图谱还没构建），对应控件禁用并显示原因，避免用户往里输入、失焦后被清空而以为「输入丢了」。

就绪门提示里带一个「立即回填」按钮（仅管理权限可见），它就是解除 `attributes_ready = false` 的入口：少了它，用户会被提示「请先回填」却没有任何地方可以点。回填与构建、向量修复共用同一个任务类型，所以既有的任务中心登记与状态轮询会自动带上它；任务从活动转为不活动时组件重新拉取一次筛选选项，回填完成后不需要手动刷新。

节点详情卡片、展示设置与高级筛选共用同一定位与层级（`top:60px` / `left:10px` / `z-index:100`），详情卡片在 DOM 里更靠前，所以后展开的面板会盖住它。三者必须互斥：任何打开动作都先经过 `closeDockedPanels()`，点击节点或边时也先收起它们。新增占用这个位置的面板时必须走同一个入口。

**分工**：Neo4j 拥有实体、关系与路径事实，路径判定只发生在 Cypher 里；PostgreSQL 拥有 `attributes` 的构建期来源与回填输入，不参与路径判定；前端只表达条件与展示，授权仍由 `require_knowledge_base_read` 在执行处判定。

## 替代方案

- **PostgreSQL 主导，把命中的 `entity_id` 集合作为白名单传给 Neo4j**。PostgreSQL 镜像的 `attributes` 本身就是 JSON 列，`label` 与 `relation_type` 也是列，枚举与匹配都能用 SQL 表达，零改写入、零回填。不采用的原因：路径判定会跨两个存储，`write_chunk_graph` 成功而 `upsert_chunk_graph` 失败时两个存储短暂漂移，白名单会漏掉 Neo4j 里真实存在的节点；而且白名单规模不可控，需要额外的上限与失败分支。
- **启用 APOC，用 `apoc.convert.fromJsonList` 解析 JSON 字符串**。代码最少、无冗余数据、无回填。不采用的原因：给图谱栈引入新的部署依赖与配置表面，现有部署必须改 Compose 才能使用；预设条件不成立时的降级分支本身又是一层需要长期维护的表面。
- **把 `attributes` 从 JSON 字符串改为 Neo4j 原生属性**。最干净的数据形态，但 Neo4j 不支持 map 数组，只能拆成平行数组（即本决策的投影），仍然需要回填；且直接替换会改动详情面板正在展示的属性形态。
- **在 Cypher 里对 JSON 字符串做子串匹配**。无需改写入、无回填、无依赖。不采用的原因：匹配不精确，属性名与属性值会互相误命中，且依赖 `json.dumps` 的默认分隔符格式。
- **结果后置过滤**（先取子图，再在 Python 里按条件筛）。实现最简单。不采用的原因：会留下「边被滤掉、端点成孤儿」的不一致，且 `max_nodes` 截断可能先于过滤发生，导致满足条件的节点因为截断而根本没被取到。
- **先按条件筛种子再向外扩散**。让不连通的实体也能各自展开。不采用的原因：结果不再是「起点到终点的一条路径」，与起点/终点的路径语义脱钩。
- **保留 `GET /api/graph/subgraph` 不变，新能力放新路径**。零破坏。不采用的原因：两套子图查询会让「子图查询是什么」有两个 Owner，而旧接口的项目内消费者会同时消失。
- **扩展抽取管线，让关系产出结构化属性**。让「关系属性」与其他三类对称。不采用的原因：范围包含抽取 prompt/schema、归一化、Neo4j 写入、PostgreSQL 镜像加列与迁移、向量投影与详情面板展示，工作量与本特性主体相当，应当是独立特性。
- **本轮不做「关系属性」**。把范围压到三类筛选。不采用的原因：`:RELATION` 边上的 `text` 与 `extractor_type` 足以支撑一个受限但真实可用的关系属性筛选。
- **类型类条件保留可增删多行**。与属性类形式统一。不采用的原因：`类型 = 项目 AND 类型 = 组织` 恒为空，使用者会误判为「筛选没生效」。
- **类型类条件多行但跨行 OR**。同样避开恒空。不采用的原因：与属性类的跨行 AND 形成两套规则。

## 后果

- `GET /api/graph/subgraph` 不再存在，图谱查询只有 `POST`。该接口未出现在机制文档的 API 表里，项目内唯一消费者是前端与集成测试；这是本次唯一的公开契约破坏。
- 查询契约里不再有 `keyword` 与 `exclude_chunk`：关键词能力迁移到 `/api/graph/entities`，「路径是否可经过 Chunk」被「路径只走实体」取代。图谱展示设置里的「排除 Chunk 节点」开关随之移除，图谱画布不再展示 Chunk 节点；内容溯源仍在详情面板与检索链路里。
- `query_nodes` / `_query_nodes_sync` / `_build_where` / `_build_query` / `_process_query_result` 被取代后删除；`query_seed_subgraph` 与 `_process_subgraph_record`、`_normalize_node`、`_normalize_edge`、`_finalize_subgraph_result` 因检索链路仍在用而保留。
- 已建图谱在回填前无法按实体属性筛选，得到的是显式错误而不是空结果，因此回填是部署义务。
- 关系属性只覆盖 `:RELATION` 边现有的 `text` 与 `extractor_type`。让关系产出结构化属性需要扩展抽取管线，是独立特性。
- 图谱页在未选起点时按概览模式加载：这是构建完成后看到「全图」的原有行为，必须保留；选中起点后才是锚定视图，清空起点再查询即回到全图。
- 概览模式不是旧行为的等价保留（差异见「实现方案」），也不等同于「全部实体」——它仍受 `max_nodes` 约束，`truncated` 会告诉调用方被截断。
- 关系属性的 `text` 是自由描述，其「值」在筛选面板里是可输入的自动补全而非封闭下拉；其余三组是封闭枚举。同一个属性名只能出现在一行里，已被占用的属性名会从下拉中消失。
- 枚举取值的顺序由 Neo4j 的 `ORDER BY name, value` 决定（中文按码位排序），前端下拉的展示顺序依赖它。
- 回填任务的进度分母是知识库实体总数（含无属性实体），进度百分比会保守偏低。
- 回填以 PostgreSQL 镜像为输入而非 Neo4j 里的 JSON 字符串（Cypher 无解析能力），两存储漂移时回填结果跟随 PostgreSQL。
- `POST /api/graph/subgraph` 是图谱路由里唯一不用 `require_knowledge_base_read` 依赖的：它的 `kb_id` 在请求体里，只能显式调用 `ensure_knowledge_base_permission`。新增「kb_id 来自请求体」的路由时应沿用这一写法，不要把权限依赖套在请求体参数上。
- 属性投影只写在 Neo4j：`upsert_chunk_graph` 落 PostgreSQL 镜像时必须剔除 `attribute_names` / `attribute_values`。将来给实体记录增加 Neo4j 专用字段时要同步这两处。

## 验证

`Passed` 表示命令实际执行且结果已核对；`Not run` 表示未执行并说明原因。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 起点与终点同时给出时返回两者之间的路径子图 | 返回的是两个邻域的并集 | `_build_subgraph_cypher` | `test_query_subgraph_returns_path_between_start_and_end`（真实 Neo4j：企业→项目→人员） | `max_depth=1` 时同一请求返回空（`test_query_subgraph_depth_limit_excludes_longer_paths`） | Passed |
| 只给起点时返回向外不超过层数的邻域 | 退化为整图 | `_build_subgraph_cypher` 的 `if start_id:` 分支 | `test_query_subgraph_start_only_expands_neighbourhood`（真实 Neo4j）：层数 1 得 `{企业, 项目, 旧项目}`，层数 2 多出 `人员` | 同一用例里带 `中间实体 = 项目` 时 `人员` 被正确排除 | Passed |
| 不填起点时返回概览子图而不是空结果 | 概览模式返回空 | `_build_subgraph_cypher` 的 `seeds` 收尾 | `test_query_subgraph_overview_without_start_returns_reachable_entities`：`_entity_ids` 等于四个实体，`truncated` 为假 | 把 `overview_tail` 里的 `+ [x IN seeds ...]` 去掉会让孤立实体从结果消失 | Passed |
| 中间实体与实体属性只约束中间实体，起点与终点豁免 | 起点缺该属性导致所有路径被滤掉 | 中间实体谓词的 `exempt` | `test_query_subgraph_intermediate_entity_condition_exempts_start`、`test_query_subgraph_entity_attribute_condition_matches_name_and_value` | 去掉终点豁免后这两个用例 FAIL（实测） | Passed |
| 关系类条件作用于每条关系，任一条不满足则整条路径不返回 | 存在一条满足即保留 | 关系谓词 | `test_query_subgraph_relation_type_condition_filters_whole_path`、`test_query_subgraph_relation_attribute_text_uses_substring_match` | 后者含「企业→项目」边不含关键词时返回空的反向断言 | Passed |
| 实体属性要求属性名与属性值同时相等 | 值相同而名不同被判命中 | `_build_entity_records` 投影 | `test_milvus_graph_service_entity_records_project_attributes_as_parallel_arrays`、`..._keep_attribute_values_with_separators` | `状态=已完成` 的路径不返回 | Passed |
| 属性未回填时就绪门拒绝而不是静默空结果 | 返回 200 与空结果 | `attributes_ready` + 路由 409 分支 | `test_query_subgraph_rejects_entity_attributes_before_backfill`（服务层）、`test_attributes_ready_detects_unprojected_attributes`（真实 Neo4j） | 回填后 `attributes_ready` 由 False 转 True | Passed（服务层）；路由层 409 见下方 `Not run` |
| 回填幂等且不改变图结构 | 重复执行产生重复或改动节点 | `backfill_attribute_projection` | `test_backfill_attribute_projection_is_idempotent`：两次结果相同、实体计数不变 | 把 `SET e.attribute_names` 改名后该用例 FAIL（实测） | Passed |
| 层数上限 3 | 生成超过上限的跳数 | 路由 `le=3` + service 夹取 | `test_query_subgraph_clamps_depth_to_max_subgraph_depth` | 请求 `max_depth=5` 的路由断言见 `Not run` | Passed（service 层） |
| 截断可与「确实为空」区分，且节点与边两个上限都要算 | 边被上限砍掉却报告不截断，用户以为看到了全部关系 | Cypher 的 `truncated` 表达式 | `test_query_subgraph_returns_nodes_edges_and_truncation`；`test_query_subgraph_reports_truncation_when_edges_hit_the_cap`（真实 Neo4j：节点 3 ≤ `max_nodes` 4 而边 10 > 8 → `truncated` 为真且只返回 8 条边）；概览用例断言 `truncated` 为假 | 去掉 `size(edges) > $max_nodes * 2` 后该用例 FAIL（实测） | Passed |
| Neo4j 不可用时抛出而不是返回空图 | 返回 200 与空 `nodes` | `query_subgraph` 的异常传播 | `test_query_subgraph_propagates_neo4j_failure` | 路由 503 映射见 `Not run` | Passed（服务层） |
| 关键词搜索能力迁移到 `GET /api/graph/entities` | 起点选择器无数据可搜 | `search_entities` | `test_search_entities_matches_name_substring`（真实 Neo4j） | 不存在名字返回空列表 | Passed |
| 四类筛选枚举与图谱实际取值一致 | 下拉出现图谱里没有的取值 | `_filter_options_sync` | `test_get_filter_options_merges_attribute_values_by_name`、`test_get_filter_options_reports_graph_enums` | — | Passed |
| 图谱写入链路完整：实体记录能同时落 Neo4j 与 PostgreSQL 镜像 | 投影多出的两个键不是表列，镜像写入整批编译失败，图构建任务全量 `write_failed`、图谱永远建不成 | `KnowledgeGraphRepository.upsert_chunk_graph` | `test_write_chunk_graph_records_can_be_written_to_postgres_mirror`（真实 PostgreSQL + Neo4j，走完 `write_chunk_graph → upsert_chunk_graph`） | 不在落库前剔除 `attribute_names` / `attribute_values` 时，该用例以 `CompileError: Unconsumed column names` 失败（实测） | Passed |
| 检索链路不受本次改动影响 | `query_seed_subgraph` 或 PPR 的输入形态被改 | `query_seed_subgraph` | `test/unit/graphs`、`test/integration/graphs/test_graph_vector_projection_state.py`、`test_milvus_graph_delete.py` 全部通过 | 改动 `_normalize_node` 的返回键会让这些用例变红 | Passed |
| 子图查询路由不额外要求 query 参数 | 权限依赖把请求体里的 `kb_id` 解析成必填 query 参数，只发请求体的调用方全部 422 | `graph_router.post_subgraph` 的依赖形态 | `test_subgraph_route_does_not_require_kb_id_as_query_parameter`（读取应用生成的 OpenAPI 契约） | 用 `require_knowledge_base_read` 作依赖时该用例 FAIL（实测：必填 query 参数为 `{'kb_id'}`） | Passed |
| 未处理的关系属性名显式失败 | 条件被静默丢弃，返回「看起来成功但没有筛选」的结果 | `_build_relation_predicates` | `test_build_subgraph_cypher_rejects_unhandled_relation_attribute_name`、`test_subgraph_route_rejects_unknown_relation_attribute_name` | 去掉 service 侧校验时前者 `DID NOT RAISE`（实测） | Passed |
| 回填分页不被一页空属性实体顶死 | 空属性实体占满一页即被误判取完，后续实体永远回填不到、该库的属性筛选永久不可用 | `list_entities_with_attributes_by_kb_id` | `test_list_entities_with_attributes_does_not_stop_on_attribute_less_page`（真实 PostgreSQL：3 个空属性实体在前、页大小 2） | 只在 Python 侧过滤时该用例返回 `[]` 而不是那行有属性的实体（实测） | Passed |
| 进入图谱页时默认展示全图，构建完成后能看到图 | 默认视图为空，用户以为图谱没建好 | `_build_subgraph_cypher` 的概览分支 + `loadGraph` 的默认请求 | `test_query_subgraph_overview_without_start_returns_reachable_entities`（真实 Neo4j）；并在真实知识库上直接核对：不填起点查询返回 100 节点 / 73–125 边，`truncated` 为真 | — | Passed（服务层）；前端的默认自动加载没有执行证据，见 `Not run` |
| 前端按面板状态装配请求体 | 面板与请求体不一致 | `web/src/utils/graph_query_payload.js` | `web/test/unit/graph_advanced_filter.test.js`（4 例）；全量 web 单测 399/399；lint 无告警；build 成功 | 类型行只提供多选，构造不出恒空条件；已被占用的属性名从其它行下拉移除、本行保留（同一文件的第三、四个用例） | Passed（装配与静态检查） |
| 路由层行为：鉴权、非 Milvus 404、`GET` 返回 405、`max_depth=5` 返回 422、只填终点与同点返回 400、未知关系属性名返回 422 | 旧路由仍可访问或校验缺失 | `graph_router.py` | `test/integration/api/test_unified_graph_router.py` 中相应用例已写入 | 该文件无负向对照 | **Not run** |
| 真实页面交互与视觉 | 面板布局、暗色对比度、下拉开合 | `KnowledgeGraphSection.vue` | 需要浏览器 | — | **Not run** |

`Not run` 的原因说明：

- 路由层集成用例依赖 `TEST_USERNAME` / `TEST_PASSWORD`，本环境没有配置这两个变量（`.env` 与 `.env.template` 里都没有），因此 `backend/test/integration/api/` 整体被 `pytest.skip`，不只是本次新增的用例。服务层的同义约束已由真实 Neo4j 覆盖。
- 这次被跳过的范围并不只是「缺证据」：独立 Review 正是在这里发现 `POST /api/graph/subgraph` 因为权限依赖被解析成必填 query 参数而必然返回 422，旧的路由断言若被执行会直接失败。缺陷已修复，并由「子图查询路由不额外要求 query 参数」这一行的契约用例覆盖——它读应用生成的 OpenAPI、不依赖凭证、真实执行。其余路由层断言（非 Milvus 拒绝、405、层数与参数校验）仍待凭证补齐后执行。
- 真实页面验证需要浏览器，本次执行环境没有浏览器自动化能力。
