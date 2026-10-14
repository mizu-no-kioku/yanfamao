# 父行侧归属防护：归属链盘点、更新路径、锁顺序与实现（版本 14 数据约束门槛）

**第 1 步（盘点 + 更新路径 + 锁顺序设计）与第 2 步（test-first 实现双向保护 + 真实 PostgreSQL
并发验收）均已完成**——实现与全部证据见 §6。

仍**不得**对开发库执行迁移：**隔离代码已接线版本 14**（`ensure_tenant_rls()` /
`ensure_tenant_reference_consistency()` 已在迁移入口、`BUSINESS_SCHEMA_VERSION = 14`），
但**开发库仍是 13**、四项验收只在**隔离环境**做过，且**安全门槛未关闭**——
`SECURITY INVOKER` 隐藏行 fail-open、配置三表与 `tenants` 边界未完成（清单见
`version-14-round10-handover.md` §3 / §3.1）。
另有一项**启用 RLS 之前必须做**的补偿性验收：**存量跨租户子行检查**（见 §6.6）——
触发器不校验存量，被 RLS 藏起来的旧数据仍能绕过父侧检查。

方案的选型（人类伙伴裁定）：**数据库父行侧防护，暂不删除现有 repository 的可写白名单。**

## 0. 两处措辞校正（避免把能力说成事实）

- 白名单 + `setattr` 证明的是**可达的 repository 写入能力**，**不是**"已证实的业务迁移需求"。
  我没有追出哪个 HTTP／业务调用方真的把 `kb_id` 放进 `data`，本文不作那个断言。
- **仅把 `kb_id` 从白名单里删掉不构成数据库约束**：裸 SQL、迁移脚本、其他写入路径照样能制造失配，
  而且它**替代不了**并发验证。这正是选择"父行侧防护"而不是"删白名单"的理由。

## 1. 归属链盘点（对着 `manager.py` 的 `TENANT_REFERENCE_RULES` / `TENANT_REFERENCE_ROW_KB` 核对）

子表 → 必须与"行自身 kb"一致的引用（现状已有的**子侧**规则）：

| 子表 | 行自身 kb 来源 | 引用（本表列 → 父表.键） |
|---|---|---|
| `knowledge_files` | `kb_id` | `parent_id` → `knowledge_files.file_id`（自引用，可空） |
| `knowledge_chunks` | `kb_id` | `file_id` → `knowledge_files.file_id`（NOT NULL） |
| `knowledge_graph_entity_mentions` | `kb_id` | `entity_id` → `knowledge_graph_entities.entity_id`；`file_id` → `knowledge_files.file_id`；`chunk_id` → `knowledge_chunks.chunk_id` |
| `knowledge_graph_triples` | `kb_id` | `source_entity_id` / `target_entity_id` → `knowledge_graph_entities.entity_id` |
| `knowledge_graph_triple_mentions` | `kb_id` | `triple_id` → `knowledge_graph_triples.triple_id`；`file_id` → `knowledge_files.file_id`；`chunk_id` → `knowledge_chunks.chunk_id` |
| `evaluation_dataset_items` | `kb_id` | `dataset_id` → `evaluation_datasets.dataset_id` |
| `evaluation_runs` | `kb_id` | `dataset_id` → `evaluation_datasets.dataset_id`（可空） |
| `evaluation_run_items` | **无 `kb_id` 列**，经 `run_id` → `evaluation_runs.kb_id` | `dataset_item_id` → `evaluation_dataset_items.item_id`（可空） |

**反查（父行 `kb_id` 变化时，哪些子行会失配）**——这就是父侧触发器要覆盖的集合：

> 反查表只覆盖**"子行有自己的 `kb_id`"**的情形：这类子行的失配判据是
> `child.kb_id IS DISTINCT FROM NEW.kb_id`。**没有 `kb_id` 列的子行不在本表机械范围内**，
> 见下面"两条必须单独处理"的第 1 条。

| 父行 | 受影响的子行（列） |
|---|---|
| `knowledge_files` | `knowledge_files.parent_id`；`knowledge_chunks.file_id`；`knowledge_graph_entity_mentions.file_id`；`knowledge_graph_triple_mentions.file_id` |
| `knowledge_chunks` | `knowledge_graph_entity_mentions.chunk_id`；`knowledge_graph_triple_mentions.chunk_id` |
| `knowledge_graph_entities` | `knowledge_graph_entity_mentions.entity_id`；`knowledge_graph_triples.source_entity_id` / `target_entity_id` |
| `knowledge_graph_triples` | `knowledge_graph_triple_mentions.triple_id` |
| `evaluation_datasets` | `evaluation_dataset_items.dataset_id`；`evaluation_runs.dataset_id` |
| `evaluation_dataset_items` | `evaluation_run_items.dataset_item_id` |

**两条必须单独处理、不能靠上表机械推导的链**：

1. **`evaluation_runs.kb_id` → `evaluation_run_items`**：这条**不在** `TENANT_REFERENCE_RULES` 里
   （run_items 的规则只写了 `dataset_item_id`）。但 run_items **没有 `kb_id` 列**，它们的归属**由
   run 定义**——所以改 `evaluation_runs.kb_id` 会**隐式**把它们一起搬走。这正是禁止的
   **"静默级联迁移归属"**。

   **这里必须分成两项互相独立、不能互相替代的约束**（先前版本把它写成"只比题目所在库"，
   那是错的——`dataset_item_id` **可以为空**，只校验非空题目会放过所有题目引用为空的结果行，
   让它们随 Run 隐式迁移）：

   - **① Run 改库：只要存在结果行就拒绝。** 判据是 `EXISTS (run_items WHERE run_id = OLD.run_id)`，
     **不看** `dataset_item_id` 是否为空。这是"已有子行禁止隐式搬迁"的独立约束。
   - **② 结果行的题目引用：非空时题目与 Run 同库，为空时合法。** 这一项留在**子侧**
     （`evaluation_run_items` 的 `BEFORE INSERT OR UPDATE`），是原有的引用一致性规则。

   ①通过**不**意味着②成立，②通过也**不**意味着①成立：前者管"归属被搬走"，后者管"引用跨库"。
2. **`knowledge_bases.kb_id`**：它是根，子表以 `ON DELETE CASCADE` 引用它。改它属于改**键**，
   外键（NO ACTION）本身就会挡住——不需要新增防护，但要记名：这条链的保护来自外键而非本机制。

## 2. 更新路径盘点（哪些路径能改到归属字段）

| 路径 | 能否改 `kb_id` | 依据（已逐个核对） |
|---|---|---|
| `KnowledgeFileRepository.update_fields` / `update_fields_if_status` | **能** | `_writable_fields`（:174-175）含 `kb_id`；`_sanitize_data`（:204-208）按白名单过滤后 `update_fields` 逐个 `setattr`（:749-750） |
| `KnowledgeChunkRepository` 的 upsert 路径 | **能** | `_writable_fields`（:16-19）含 `kb_id`；upsert 对已存在行 `setattr`（:122-123） |
| `EvaluationRepository.update_dataset_in_session` | **能** | 同一套**无白名单**的 `setattr`（:63-68）；`EvaluationDataset` **确实有 `kb_id` 列**（`models_knowledge.py:241`）→ 能持久化 |
| `EvaluationRepository` 的 run 更新（:153-157） | **能** | 无白名单 `setattr`；`EvaluationRun` **确实有 `kb_id` 列**（`models_knowledge.py:288`）→ 能持久化 |
| `EvaluationRepository` 的 run_item 更新（:209-216） | **不能** | `setattr` 同样无白名单，但 `EvaluationRunItem` **没有 `kb_id` 列**（`models_knowledge.py:305-321` 只有 `run_id` / `dataset_item_id` 等）→ 设上去只是**非映射的 Python 属性，不落库**；它的归属只能经 `run_id` **间接**变化 |
| `KnowledgeGraphRepository.upsert_chunk_graph` | **不能** | `on_conflict_do_update` 的 `set_` 只列 `name`/`attributes`/`updated_at`（entity，:265-269）与 `content`/`relation_type`/`updated_at`（triple，:297-301），**不含 `kb_id`**；mention 走 `on_conflict_do_nothing`（:285）→ 已存在行的归属改不动 |
| 裸 SQL / 迁移脚本 / 运维脚本 | **能**（白名单管不到） | 这是"删白名单"方案不成立的原因 |

**两条措辞边界（评审校正，都必须守住）**：

1. 以上是**可达的写入能力**，**不是**"已证实的业务迁移需求"——我没有追出哪个调用方真的把
   `kb_id` 放进 `data`。
2. **"更新能力宽"不等于"该表有这个字段"**。`EvaluationRepository` 的通用 `setattr` 只说明
   *输入限制宽*；能不能**持久化**改 `kb_id` 取决于**该表是否真有这个 ORM 列**——所以上表按
   **真实列**逐条分开列，`run_item` 单独标为**不能**（它没有该列）。同理，"父引用字段"
   （如 `run_items.run_id`）与"归属列"是两回事，不能混在一行里。

**仍要注意的两点**：
- 评估域那两处（dataset / run）**没有白名单**，比 file/chunk 那两处更宽；父侧防护对它们是
  **唯一**的数据库边界。
- 图谱写路径目前改不动 `kb_id`，但这依赖 `set_` 清单**保持**不含 `kb_id`——这是一条**约定**，
  不是机制。父侧触发器把这条约定变成硬约束。

## 3. 锁顺序设计（门槛的核心，也是"双方都有触发器"不够用的原因）

### 现状为什么不够

- 子侧触发器对父行只做**不加锁**的 `SELECT`；
- `UPDATE ... SET kb_id` 只改**非键列**，PostgreSQL 因此只取该行的 **`FOR NO KEY UPDATE`** 锁；
- 外键的锁是 **`FOR KEY SHARE`**，它**不与 `FOR NO KEY UPDATE` 冲突**。

所以"外键锁 + 子侧检查"挡不住"父行改 `kb_id` 与子行插入并发"。**"父子双方都有触发器"也不自动
代表并发安全**——两个触发器各自成立，与它们的**锁模式**是否互斥是两回事。

### 设计

1. **子侧**：父行查找改为 `... FOR SHARE`。`FOR SHARE` 与 `FOR NO KEY UPDATE` **冲突**，
   于是"父行改归属"与"子行写入/改引用"互相阻塞。
2. **父侧**：`BEFORE UPDATE` 触发器；`NEW.kb_id IS DISTINCT FROM OLD.kb_id` 时按第 1 节的反查表
   检查既有子行，任一失配即 `RAISE EXCEPTION`（`ERRCODE = '23514'`）。此刻父行已由自身的
   `UPDATE` 持有 `FOR NO KEY UPDATE` 锁，而任何并发的子行写入都因第 1 条阻塞在 `FOR SHARE` 上
   ——检查看到的是一个**冻结**的子集合，不会"查完又冒出一行"。
3. **减少父子反向加锁，并按「实际父行」定一个明确、稳定的锁顺序。** 父侧不锁子行，所以"父锁子"
   这条方向不存在；但这**不等于**死锁面被消除——**子侧一行可以锁多个父行**
   （`knowledge_graph_entity_mentions` 要锁 entity + file + chunk 三个；
   `knowledge_graph_triple_mentions` 要锁 triple + file + chunk；
   `knowledge_graph_triples` 要锁两个 entity），两个并发事务若以不同顺序取这些锁就会死锁。

   **排序键必须是"实际父行"，不是"父表 + 键列名"**：`knowledge_graph_triples` 的两个引用
   （`source_entity_id` / `target_entity_id`）是**同表、同键列**，按表名+列名排序得到的次序是
   固定的，但两次事务里这两个 ID 的角色可以互换（A→B 与 B→A）→ **仍然反向取锁**。
   所以实现必须是两步：

   1. **先收集并去重**本行要锁的父行集合，元素是 `(父表, 实际主键值)`；
   2. 再按 `(父表名, 实际主键值)` 的**统一顺序**逐个 `FOR SHARE`（plpgsql 里即把待锁键值
      取成数组、`ORDER BY` 之后 `FOREACH` 循环），而不是按规则表/列的书写顺序。

   **锁顺序是否真的稳定、是否真的不死锁，必须由真实 PostgreSQL 并发用例验证，不能由本文断言**；
   而且**死锁用例只针对本文定义好的那些并发场景**（同表两引用互换、多父引用交叉），
   **不承诺**任意业务事务都绝无死锁——那既证明不了，也不是本门槛的要求。
4. 父行**不可见**（不存在，或被他租户的 RLS 策略藏起来）时，判据一律按**不一致拒绝**
   （`23514`）。这是 `IS DISTINCT FROM` 的 NULL 语义的**有意**结果，也是子侧在 RLS 下
   **fail-closed** 的来源：父行看不见就拒绝，而不是"看不见就当没有"。**代价如实记名**——
   "父行不存在"这个更准确的原因会被 `reference consistency violated` 盖掉，**不是**交给外键
   （`23503`）。本条曾被本文写成"交给外键报错"，与代码不符，已改正（独立审查 P1）。

### 明确支持的事务隔离级别（证据的作用域）

- **代码里没有任何显式隔离级别设置**（`grep -rn "isolation_level" backend/package backend/server` 无命中）
  → 走 PostgreSQL 服务端默认；隔离实例上实测 `SHOW default_transaction_isolation` = **`read committed`**。
- 因此**本门槛的证据范围就是 READ COMMITTED**，并发验收至少要在该级别下做。
- **不得**把 READ COMMITTED 下的结论推广到其它级别。若将来某个路径改用别的级别（或把应用整体
  切过去），必须**分别**验收；在那之前，**支持级别明确限制为 READ COMMITTED**。

### 必须由真实 PostgreSQL 用例证明的部分

锁模式与检查时机**不能靠推理定稿**。至少这几件事本文不作结论：

- `FOR SHARE` 与 `FOR NO KEY UPDATE` 的冲突关系（本文的推理是设计的依据，但要跑出来才算数）；
- **父侧"只读子行"能否看见并发事务刚提交的子行**——这取决于隔离级别与触发器里查询的实际时机，
  **不能凭锁冲突推定安全**。相应地，验收必须**读事务最终提交的数据**来判断是否失配，
  而不是只看"观察到阻塞"或"抛了异常"；
- 多父引用的锁顺序在并发下是否真的不死锁；
- 自引用链（`knowledge_files.parent_id`）上触发器对同表的可见性；
- **多行 `UPDATE`**（一条语句改多行、每行各触发一次）与事务中其它操作叠加时的行为。

**在这些用例通过之前，本设计只是设计。**

## 4. 明确契约（含需要裁决的一处行为）

- **有既有子行时修改父行归属**：会被父侧触发器**明确拒绝**（`23514`），不做级联改写。
  **限定（不要省略）**：该子行必须在当前租户上下文里**可见**。父侧触发器是 `SECURITY INVOKER`，
  被 RLS 藏起来的子行不在检查视野内。
  **这个限定描述的是当前实现的边界，不是把原要求放宽**——"已有子行不得静默迁移"仍是目标；
  隐藏子行那条路径必须先由存量预检消除，或在运行期改用受控校验（`SECURITY DEFINER` 的取舍
  待裁决，见 §6.9、§6.10）。
  对**没有 `kb_id` 列的子行**（只有 `evaluation_run_items`）这条尤其要说清：判据是
  **"存在结果行"**，与结果行的 `dataset_item_id` 是否为空**无关**——空题目引用的结果行
  同样会被隐式搬走，所以同样必须拒绝。
- **子行插入/改引用 × 父行改归属并发**：两种先后顺序都不得提交失配数据（靠第 3 节的锁）。
- **`evaluation_run_items.dataset_item_id` 置空**与 `ON DELETE SET NULL` 触发的更新**必须仍然合法**
  ——子侧规则里"引用为 NULL 时跳过"这条不能被新增的锁改动破坏。
- **无子行且满足其他约束的归属修改**：按**允许**实现（人类伙伴未反对，本文按此定契约）。
  理由：那是显式的、可审计的单行变更，不产生不一致；**批量迁移归属必须另走显式流程**，
  不允许通过通用 `update_fields` 隐式搬走整条归属链。
- repository 侧继续给出**可读错误**，数据库是**最终强制边界**。

## 5. 验收清单（第 2 步必须覆盖，逐条要能红）

1. 有既有子行时修改父行归属 → 失配必须被拒绝。
2. **Run 改库 × 已有结果行**：拒绝；且**要单独覆盖 `dataset_item_id` 为空的结果行**——
   只测非空题目不够（这正是先前设计的漏洞）。
3. 结果行的题目引用（**子侧**规则）：非空时跨库被拒，**为空时合法**。
4. 子行插入 × 父行归属修改**并发**：两种先后顺序都不能提交失配数据。
5. 子行**修改父引用** × 父行归属修改并发。
6. **多父引用的锁顺序**：两个并发事务以不同顺序引用同一组父行时，不得死锁；
   或在确实会死锁时如实记录并重新设计（不接受"偶尔死锁"）。
7. `evaluation_run_items.dataset_item_id` 置空及 `ON DELETE SET NULL` 触发的更新仍合法。
8. 无子行且满足其他约束的归属修改 → 行为符合第 4 节的契约。

**验收方式（对第 4–6 条尤其重要）**：

- 并发用例用**屏障（barrier/Event）协调两个事务到指定交错点**，**不用固定 `sleep`**——
  sleep 只能靠时序撞运气，撞不到就变成"看起来绿"。
- 必须**读事务最终提交的数据**来判定"是否提交了失配"，不能只看"观察到阻塞"或"抛了异常"。
- 全部并发用例在**上文声明的 READ COMMITTED** 下跑；结论只作用于该级别。
- 第 6 条只针对**本文定义好的并发场景**（`triples` 两引用互换、多父引用交叉），
  **不承诺**任意业务事务都绝无死锁。

——以上五条 + 第 2 节的更新路径补完，构成"引用一致性并发门槛"；8 条验收的覆盖见 §6.3。
**开发库保持版本 13**；版本 14 的接线已在**隔离环境**完成，尚未对开发库执行。

## 6. 第 2 步：实现与验收证据

### 6.1 实现（`backend/package/yuxi/storage/postgres/manager.py`）

- 新增 `TENANT_PARENT_OWNERSHIP_RULES`（父表 → 主键列 + 子行清单）+ `_parent_ownership_statements()`：
  每张父表一个 `BEFORE UPDATE` 触发器，`NEW.kb_id IS DISTINCT FROM OLD.kb_id` 时复核既有子行，
  失配即 `RAISE EXCEPTION ... ERRCODE = 23514`。有 `kb_id` 列的子行按 `IS DISTINCT FROM` 判；
  没有该列的（只有 `evaluation_run_items`）按"存在引用"判——`evaluation_runs` 那条比的是
  **`run_id`**，与 `dataset_item_id` 是否为空无关（空题目结果行同样会被隐式搬走）。
- `_reference_consistency_statements()` 的父行查找改为 `... FOR SHARE`，并按 §3 的
  「实际父行」顺序取锁：同一父表的多个引用先 `array_agg(DISTINCT ... ORDER BY ...)` 去重升序，
  再 `FOREACH` 加锁；父表之间按表名升序（生成期定序）。
- 两个方向合成同一个 `TENANT_REFERENCE_CONSISTENCY_STATEMENTS`，因此
  `ensure_tenant_reference_consistency()` 一个入口装齐——**没有新增公开方法，接线契约不变**。

### 6.2 证据（全部实测）

| 项 | 结果 |
|---|---|
| RED（实现前） | 12 条新用例全红，原因正确：父侧 8 条是"应拒未拒"（`committed`），4 条并发/顺序用例是"没有被挡住" |
| GREEN | `test_tenant_isolation_guardrails.py` **38 passed** |
| 探针 A（只去掉子侧取锁，保留父侧） | **4 条红**，恰为依赖锁的那 4 条（三条并发 + 取锁顺序），其余仍绿 |
| 探针 B（只去掉父侧防护） | **11 条红**（父侧 3 + 反查表 6 + 并发子行在先 1 + **受限角色可见性 1**）；E7/E8 仍绿（由子侧规则拦下，符合预期）；"无租户上下文"那条也仍绿——它测的是 **RLS 挡行**，不是触发器 |
| 探针 C（取锁顺序改回"按列的书写顺序"的朴素写法） | **只有取锁顺序用例红**，证明该断言能判别排序键 |
| 探针 D（夹具的取消失效） | 故障注入用例在 teardown 报错：`收尾没有很快完成（10.0s）——取消挂起任务没有生效` |
| 探针 E（去掉会话级超时设置） | **只有**那条断言用例红，其余全绿——如实记为"纯安全网"，见 §6.5 |
| 探针 G（往 schema 的表上留一把 `ACCESS SHARE` 锁） | 收尾 `DROP SCHEMA` 以 `canceling statement due to lock timeout` 报错、全程 **9 秒**——**有界报错，不是无限等锁**（这类挂死正是 §6.5 记的那次事故） |
| 还原 | 探针 A / B / C / D / E / G 全部还原（B、E 在"会话级超时"的新形态下复跑过），`grep PROBE` 无残留；还原后 38 passed |
| 回归 | unit **2597 passed**（= 基线）；ruff `All checks passed`；六个租户文件 **75 passed**（改前 53 + 新增 22） |
| 全量 `test/integration/services` | **在隔离项目里跑不起来**：91 errors 全部发生在 **setup** 阶段，原因是 `No address associated with hostname`——隔离网络里 `milvus` / `graph` / `minio` / `sandbox-provisioner` 与 `api` / `worker` 容器本就不解析（手册写明的 fail-closed）。该次 94 passed / 2 failed 里含此环境因素，**不能**当作本改动的回归证据 |
| `storage_migration` 轮次 | **该行结论已过期**。当时未重跑的理由是"改动到不了迁移路径"（迁移入口不调用这组语句）。版本 14 接线后迁移走 `install_tenant_reference_consistency()`（同一组语句 + 同事务预检 + `ACCESS EXCLUSIVE` 停写锁），改动**确实**落在迁移路径上；第十轮已重跑 `storage_migration/` 整目录（**43 passed**） |

### 6.3 八条验收 → 用例映射

| # | 验收 | 用例 |
|---|---|---|
| 1 | 有既有子行时改父行归属被拒 | `test_moving_a_parent_file_with_existing_children_is_rejected` + 反查表参数化用例（6 张父表） |
| 2 | Run 改库 × 已有结果行（含**空题目结果行**） | `..._without_a_dataset_item_is_rejected` / `..._linked_to_a_dataset_item_is_rejected` |
| 3 | 结果行题目引用：非空跨库被拒 / 为空合法 | 原有 C 段两条 + `test_evaluation_run_item_allows_a_null_dataset_item` |
| 4 | 子行插入 × 父行改归属并发，**两种先后顺序** | `test_concurrent_child_insert_then_parent_move_commits_no_mismatch` / `..._parent_move_then_child_insert_...` |
| 5 | 子行改父引用 × 父行改归属并发 | `test_concurrent_child_repoint_then_parent_move_commits_no_mismatch` |
| 6 | 多父引用的锁顺序（triples 两引用互换、多父引用交叉） | `test_parent_locks_within_one_row_are_taken_in_a_stable_order`（判别性证据）/ `test_swapped_references_in_two_transactions_do_not_deadlock` |
| 7 | `dataset_item_id` 置空与 `ON DELETE SET NULL` 仍合法 | `test_clearing_a_result_rows_dataset_item_stays_legal` + 原有 SET NULL 用例 |
| 8 | 无子行时归属修改按契约允许 | `test_moving_a_parent_file_without_children_is_allowed` |

### 6.4 实现期发现的偏差与如实记名的边界

- **`FOREACH ... IN ARRAY (SELECT ...)` 不成立**：`FOREACH` 里的 `ARRAY` 是**语句关键字**，后面再跟
  子查询会被解析成**标量子查询**，运行时报 `FOREACH expression must yield an array, not type
  character varying`。改为先 `array_agg(...) INTO parent_keys` 再 `FOREACH`。`ORDER BY` 必须写在
  `array_agg` 里——`DISTINCT` 之后子查询的排序不保证保留。
- **观测取锁顺序不能用 `FOR SHARE` 占位**：`FOR SHARE` 与 `FOR SHARE` **不冲突**，用它占住父行
  根本挡不住插入方。占位必须用 `FOR NO KEY UPDATE`（正是 `UPDATE ... SET kb_id` 取的锁）。
- **反查表上有三条链的子侧规则会先拒**（`chunks` / `triples` / `evaluation_dataset_items`）：单单改
  `kb_id` 会先被子侧规则拒掉，因此那三条的用例**同时改掉引用列**（换到同库的父行上），让子侧放行
  ——这样"被拒"才只可能来自父行侧。这不是绕开子侧规则，而是把它排除掉。
- **第 6 条里"两事务互换引用"那条用例不具判别性**：两种取锁顺序下都难以稳定复现死锁（两个引用
  在同一条语句里取锁，中间没有可控交错点），因此它**只是冒烟**，判别性证据是顺序观测用例
  （探针 C 已证明后者能红）。这一点写在该用例的 docstring 里，不夸大。
- **锁带来的阻塞面变宽，代价未实测**：子侧触发器现在对**每一次子行 INSERT/UPDATE**（含只改
  `content` 的普通更新）都取父行 `FOR SHARE`，与并发的父行 `UPDATE` 互斥。这是设计的固有代价，
  但"每次写入多几次查询/锁"**没有实测**，不得当成已知数据。若实测成为问题，可考虑"只有引用列或
  行 kb 真的变了才取锁"，但那要单独裁决。
### 6.6 受限角色下的父侧检查（RLS 交互）与启用 RLS 前的存量检查

并发用例走 owner 连接，**回答不了**"RLS 开着时父侧检查还看不看得见子行"：父侧触发器是
`SECURITY INVOKER`，以 `yuxi_app` + 租户上下文执行时，子行查询受策略约束——若策略把子行藏起来，
父侧就会误判成"没有子行"而放行。这一项已**现在就补了证据**（夹具本身启用 RLS）：

| 场景 | 用例 | 结果 |
|---|---|---|
| 受限角色 + **正确**租户上下文改父行归属 | `test_the_parent_guard_still_sees_children_under_the_restricted_role` | **被拒**（`parent ownership guard violated`）——同租户子行在策略下可见 |
| 受限角色 + **无**租户上下文 | `test_without_a_tenant_context_the_parent_row_itself_is_invisible` | `UPDATE` **影响 0 行**（父行自己被策略挡住） |

探针 B 的复跑证明第一条**不是空转**（去掉父侧防护后它变红）；第二条在探针 B 下仍绿，因为它测的是
**RLS 挡行**、不是触发器——不要把它当父侧防护的证据。

**仍未覆盖、必须作为版本 14 验收项的残余风险**：守卫上线**之前**写入的跨租户存量子行会被 RLS
藏起来，而触发器**不校验存量**（它只作用于新写入，这是当初选触发器而非复合外键的代价之一）。
因此版本 14 启用 RLS **之前必须查存量**（父行与其子行租户是否一致），否则那条路径依然可被绕过。

**另一处需要人类伙伴裁决的设计选择（本轮不动）**：若要求父侧检查**不受 RLS 影响**，唯一办法是把
触发器改成 `SECURITY DEFINER` —— 那会引入一个绕过 RLS 的执行上下文，必须同时收紧 owner、
`search_path` 与对象引用，是一项**安全决策**，不属于本轮范围。当前实现按 `SECURITY INVOKER`
（默认）保持不变，并把上面的存量检查作为它的补偿性验收项。

### 6.7 仍未做（不要当成已完成）

- 版本 14 迁移接线；空库 / 13→14 / 重放 / **受限 worker 租户隔离**四项验收。
- **启用 RLS 前的存量预检**（见 §6.6、§6.9）：必须同时查**两类不变量**——父行与其子行的
  **租户**一致，以及**同库引用**一致（同租户下 A 库文件与 B 库 chunk 混合引用同样违反引用契约，
  只比 `tenant_id` 会漏掉）。预检要在**安装触发器 / 启用 RLS / 记录版本之前**跑，发现异常即
  **失败**，**不自动修数据**。
- **多行父行 `UPDATE`（一条语句改多行、每行各触发一次）没有用例覆盖**（§3 把它列为必须实测项）。
  那种取锁是行扫描序、不是子侧的统一序，理论上仍可与子行插入成环——**未证即不宣称覆盖**。
- 配置三表（`model_providers` / `mcp_servers` / `config_options`）与 `tenants` 边界（spec「上线条件」）。
- 证据的作用域仍限于 **READ COMMITTED**（已由用例断言，见 §6.2）。

### 6.8 关于用例规模与"形状断言"的自查

评审提出 +958 行是否含重复脚手架或只检查 SQL 形状的断言。对着文件核对的结果：

- **脚手架没有重复定义**：`_transaction` / `_settle` / `_stage` / `_collect` / `_spawn` / `_barrier` /
  `_scalar` / `_wait_until_blocked` / `_open_transaction_states` / `_assert_lock_is_free` /
  `_mismatched_chunk_count` / `_chunk_insert` / `_two_knowledge_bases` 都只定义一次。
  重复的是**用例体**（E6/E7/E8 的"两边 + 屏障"骨架相似），它们各自的语句与预期结局不同，
  保留显式写法；这是**已知的重复**，不是重复脚手架。
- **没有断言 SQL 文本/形状**：所有父侧与并发用例都通过真实 `INSERT`/`UPDATE` 的**行为**判定
  （被拒 / 提交 / 读回的最终数据）。
- **唯一一条结构性断言**是 `test_every_child_side_reference_has_a_parent_side_guard`（规则表覆盖
  关系）。它守的是"将来加一条引用规则却忘了加父侧防护"这种**不会自己报错**的漂移，
  已在 docstring 里写明"它证明的只是规则表对得上，不是行为证据"，不作为验收依据。

### 6.9 RLS 下父侧检查的隐藏行（回应独立审查的 P1，第 2 步新增）

父侧触发器是 `SECURITY INVOKER`，查子行时受 RLS 约束。**"被 RLS 藏起来的子行会不会漏放"**
不能靠推理，必须实测。本节 4 条用例（连同 §6.10 的 4 条，`test_tenant_isolation_guardrails.py`
本文件 38 → **47 passed**）：

| 场景 | 用例 | 结果 |
|---|---|---|
| 子行自身 kb 在本租户、父引用指向别租户 | `test_a_child_in_this_tenant_cannot_reference_another_tenants_parent` | **被拒**，原因是**子侧触发器**（`reference consistency violated`）——RLS 放行、触发器 fail-closed |
| 子行自身 kb 落在别租户 | `test_a_child_declared_for_another_tenant_is_rejected` | **被拒**（RLS `WITH CHECK` 与子侧触发器都会拒） |
| 把父行改到别租户的库 | `test_moving_a_parent_into_another_tenants_kb_is_blocked_by_rls` | **被拒**（RLS `WITH CHECK`） |
| 可见子行 vs 被 RLS 藏起来的存量子行 | `test_the_parent_guard_sees_a_visible_child_but_not_one_hidden_by_rls` | 可见 → **拒**；隐藏 → **放行（fail-open）** |

**判别性**：第 4 条用两个形状完全相同的父行、同一条 `UPDATE`，唯一差别是子行租户；"可见 → 拒"
与"隐藏 → 放行"成对出现，所以"放行"不是"无子行的父行本来就能改"那种假绿。

**结论（作用域要说清）**：在"子行写入都经过子侧触发器"这一前提下，新写入产生不出父侧看不见的
子行（§6.10 a 已实测）——子行要引用某父行，其自身 kb 必须等于该父行的 kb（子侧触发器强制），
于是它与父行同租户、在该租户上下文里可见；跨租户写入则被 RLS `WITH CHECK` 或子侧触发器拒。
**因此在受支持路径上，fail-open 只作用于"守卫上线之前"写入的存量。**

**标注（不要误读）**：上表第 4 条是**已复现的漏洞测试，不是安全验收证据**。修好之后的目标是
**拒绝**，或证明该状态在任何受支持路径下**不可达**。运行期触发器**不校验存量**是它选"触发器
而非复合外键"时就已记名的代价；"存量预检覆盖它"是一个**待人类伙伴裁决**的方案，不是已成立的安全结论。

**探针（两个，各自改**函数体**而不是删触发器对象——删对象会让 setup 失败，那不是"正确原因的红"）**：

| 探针 | 改动 | 变红的断言 | 仍绿 |
|---|---|---|---|
| 1 | 子侧触发器函数体加 `RETURN NEW;`（对象保留） | `..._cannot_reference_another_tenants_parent` 与 `..._different_knowledge_base_of_the_same_tenant` 都红在 **`assert outcome.startswith("rejected")`**（插入成功） | `..._hidden_by_rls`（靠父侧，未动） |
| 2 | 父侧触发器函数体加 `RETURN NEW;`（对象保留） | `test_moving_a_parent_file_with_existing_children_is_rejected` 红在 `assert outcome.startswith("rejected")`；`..._hidden_by_rls` 红在 **`assert visible_outcome.startswith("rejected")`**（"可见子行必须被拒"那条业务断言） | `test_a_root_tables_tenant_is_immutable_on_the_owner_path`（另一套护栏） |

两个探针红的都是**业务断言**，没有一条是 setup 失败。这修正了早先"因 `DISABLE TRIGGER` 找不到
对象而红"的写法——那只证明用例依赖触发器**名字**，证明不了它能辨别安全行为。
两个探针都已还原（`manager.py` 无 `PROBE` 残留；还原后 **47 passed**）。

### 6.10 归属根变更、默认租户兜底与迁移写入窗口（回应第二、三轮独立审查）

**a. 已排除一个**具体**隐藏行构造；INVOKER 的完整安全前提仍待证明。** 反例"改 kb 的
`tenant_id` 会让已有子行变成父侧不可见"已被实测排除（见下），但**这不等于第一条审查被整体
推翻**——它的核心是"完整不变量尚未证明"，而本文自己也列着未证明前提。准确说法是：
**一个具体构造被排除；"已有隐藏存量行的父侧 fail-open"仍被复现（§6.9 第 4 条）；INVOKER 的
完整前提仍待证明。** 它依赖的那条不变量是：子侧触发器比的是
`child.kb == parent.kb`（**不是租户**），所以子行与父行必在同一个 kb。新增两条用例：

| 用例 | 结果 |
|---|---|
| `test_a_child_cannot_be_placed_in_a_different_knowledge_base_of_the_same_tenant` | **同租户、不同库**的父子引用同样被拒 → 不变量成立 |
| `test_changing_a_knowledge_bases_tenant_does_not_leave_a_movable_parent_with_hidden_children` | 改 kb 租户后，旧租户**连父行都改不动**（`UPDATE` 影响 **0 行**）→ "父行可改、子行被藏"无法同时成立 |

因此审查提出的"改 kb 的 `tenant_id` 会把已有子行变成父侧不可见"这一反例**走不通**：父子随 kb
**一起**迁移。**但作用域要收窄**——结论成立的前提是"子行写入都经过子侧触发器"与"没有 owner 级
写入把父子分离"；这两条要在版本 14 验收里**各自有证据**，不能由本节的推理代替。

**b. 归属根 `tenant_id` 已改为显式受控（本轮实现）。** 这不是"可选纵深防御"——改归属根的租户
会把**整棵归属子树**一起搬走，属**数据契约与业务语义**变更（owner 绕过 RLS，所以"owner 能改"
本身不是运行时越权，但把整库搬走是契约问题）。**裁决：普通更新路径上不可变。**

实现：`_root_tenant_immutability_statements()` 给**全部 12 张** `TENANT_ROOT_TABLES` 各装一个
`BEFORE UPDATE` 触发器，`tenant_id` 变化即 `23514`。

**护栏装在独立方法 `ensure_tenant_root_immutability()`，不放进 `ensure_tenant_schema()`（回应本轮第 2 点）。**
理由：版本 13 的语义由 `ensure_tenant_schema()` 代表；把护栏塞进它，等于**未改版本号就悄悄改了
版本 13 的行为**——已记录的版本 13 数据库重跑时行为会与首次不同。所以护栏有独立入口、独立安装点，
留给**版本 14 的装配**显式调用（装配顺序见 §6.10 d 的版本契约）。本轮只保证**方法可用、可重放、
有证据**，不承诺它已在生产路径被调用。

**重放安全（回应本轮第 1 点，修正上一版的说法）。** 上一版说"装在回填之后，所以重放安全"——
这个理由**站不住**：重放的是**语句列表**，回填语句在列表里**每次都会执行**，位置不构成屏障。
真正的重放前提是**上一次迁移已完整完成**（`tenant_id` 列已 `NOT NULL`、**无 NULL 行**）。据此护栏安装前
加一道**显式前置检查** `_stale_null_tenant_tables()`：仍有 `tenant_id IS NULL` 的归属根表就
**抛错并给修复提示**，**不自动修复**（自动回填会掩盖"上次迁移没跑完"这个事实）。首次干净迁移
（回填已把 NULL 写满、列已 `NOT NULL`）重放时该检查为空集，护栏安装幂等通过。

证据（本轮新增/更新）：

| 断言 | 用例 | 覆盖什么 |
|---|---|---|
| owner 路径改 `tenant_id` 被拒、值不变 | `test_a_root_tables_tenant_is_immutable_on_the_owner_path` | 行为证据（护栏真的拦） |
| 12 张归属根都挂上护栏 | `test_every_root_table_carries_the_tenant_immutability_guard` | **覆盖断言**（查 `pg_trigger` 目录），**非行为证据** |
| 重放安装幂等 | `test_the_root_guard_install_is_idempotent_on_replay` | 重复 `ensure_tenant_root_immutability()` 不报错、不重复装 |
| 存量 NULL 行**拒绝**安装并给修复提示 | `test_a_stale_null_tenant_row_blocks_the_guard_install_with_a_repair_hint` | 前置检查 fail-closed、不自动修复 |
| RLS `WITH CHECK` 独立拦住跨租户 `UPDATE` | `test_rls_with_check_still_rejects_a_cross_tenant_update` | **RLS 自身的 UPDATE 证据**（不依赖护栏；见 §6.10 e） |

**边界（不承诺）**：护栏不阻止超级用户主动 `DISABLE TRIGGER`——那属运维信任边界；真正的跨租户
迁移要走**经授权、可审计**的流程（显式停用护栏并留痕）。
**顺带影响**：`test_existing_rows_cannot_be_moved_across_tenants` 现在由护栏先拦（原先由 RLS
`WITH CHECK` 拦），机制换了、断言已同步。**但这条切换不能替代 RLS 自己的 UPDATE 证据**——所以
新增 `test_rls_with_check_still_rejects_a_cross_tenant_update` **单独**证明 RLS `WITH CHECK`
对 `UPDATE` 的拦截仍然成立（护栏存在与否都能验证 RLS 这一层）。

**c. 默认租户兜底与"缺上下文拒绝"冲突（**已修**）**：`tenant_context.current_tenant_id()` 在未
设置时回落 `DEFAULT_TENANT_ID`，于是**应用层永远带着上下文**，"缺上下文"与"租户 1"不可区分。
数据库层是 fail-closed 的——`test_without_a_tenant_context_writes_are_rejected` 实测写入被拒、
`test_without_a_tenant_context_the_parent_row_itself_is_invisible` 实测 0 行——但 ORM 路径靠兜底
把"缺上下文"变成了"租户 1"。**修法与范围（已按此实施，范围未扩大）**：**没有上下文时不自动向数据库注入租户 1**
（`explicit_tenant_id()` 不回落；`_apply_tenant_context` 缺上下文即跳过注入），并把"显式建立租户 1"
移到可信入口（API 中间件、API / worker 启动、worker 消费侧）。**列的
`default`/`server_default` 是否保留要逐表判断**——若无上下文时 RLS `WITH CHECK` 必然拒绝写入，
列默认值并不自动构成绕过；配置表、豁免表与未覆盖表要**单独检查**。先用真实受限角色证明
"读 0 行、写拒绝"，再决定列默认值的去留，避免一次性扩大改动。

**d. 预检到约束安装之间的写入窗口（**已修**）**：`schema_migration_lock()` 是 `pg_advisory_lock`
（`manager.py:948-966`）——只串行化**迁移器**，**不挡业务写入**。所以"预检通过 → 装触发器 /
启用 RLS"之间仍可能写入异常数据。**必须定义迁移的并发控制契约**（停写 / 维护窗口 / 表级锁）并明确事务边界；advisory lock
**只约束配合它的迁移器**，不替代停写或表锁。**版本记录要按"版本契约"分层**（不要笼统说
"等所有步骤完成"）：

- **数据库版本**：数据库结构、策略、约束与**必要的数据变换**全部成功后记录；
- **整体迁移完成状态**：文件 / 技能迁移完成后记录，或由启动闸门另行验证。

当前 `storage_migration.main()` **已按该契约分层**：`record_schema_version("business", 14)` 排在
**全部会写库的步骤之后**——包括 `_converge_database_state()` 与 `migrate_shared_skills()`
（后者会改 `Skill.dir_path` 并 `commit()`）；文件 / 技能侧标记
（`mark_v071_skills_migrated()`、`migrate_runtime_storage_identity()`）排在其后，由各自标记管理、
**不并入版本契约**。失败时**不记录版本**、重跑可续，已有失败注入用例取证
（`test/integration/storage_migration/test_migration_failure_and_replay.py`）。

**e. 标注纠正**：§6.9 第 4 条是**已复现的漏洞测试**，不是安全验收；§4 的"可见"限定描述的是
当前实现的边界，不是把"已有子行不得静默迁移"这条原要求放宽。

**f. 探针改为"改函数体"而不是"删触发器对象"**（见 §6.9）：删对象会让 setup 失败，那只证明用例
依赖触发器**名字**，证明不了它能辨别安全行为。现在两个探针红的都是**业务断言**。

**g. 结论状态（不要读成"已安全"）**：具体反例已排除；**运行时默认租户、迁移原子性、归属变更
契约（本轮已实现护栏，见 b）与 INVOKER 的完整安全证明仍未完成**。`SECURITY DEFINER` 的取舍
**不凭偏好决定**：先补完整不变量测试，若仍需要检查调用者看不见的行，再考虑**最小范围**的受控
DEFINER 函数（专用 owner、权限、固定 `search_path`、对象引用与错误信息泄漏一并审查），
**不得**把超级用户能力开放成通用查询入口。

### 6.5 并发用例的失败路径（一次已发生的事故及其修法）

**事故**：并发用例的断言在"放行"之前失败时，两条挂着未提交事务的任务没人收，夹具收尾的
`DROP SCHEMA` 需要 `ACCESS EXCLUSIVE`，于是被那些事务堵死——整套用例挂了 600 秒才被手动停掉。
**"断言失败"变成了"测试挂死"，那是最坏的一种失败**：看不出是哪条断言红的。

两层修法，缺一层都不够：

1. **每一处等待都有界**。交错点等待一律走 `_barrier()`（上限 10s，超时抛带说明的断言错误）；
   超时设在**会话**上（夹具 `connect_args.server_settings`：`statement_timeout=15s`、
   `idle_in_transaction_session_timeout=30s`），**不是** `SET LOCAL`——`SET LOCAL` 只对当前事务
   有效，用例中途 commit／rollback 之后的下一个事务不会继承。合法的阻塞窗口是**毫秒级**，这些值
   只是安全网：唯一作用是把"无限挂住"换成"以超时错误收场"。**放宽它们不会让任何断言变绿**
   （探针 E：去掉之后只有那条断言用例变红，其余全绿）。
2. **夹具收尾先取消登记的任务**。并发任务一律用 `_spawn()` 启动（登记在 manager 上），夹具
   `finally` 的第一件事是 `_cancel_registered_tasks()`，随后才断言"没有事务还开着"、再
   `DROP SCHEMA`。
   **这条保证的准确范围**（不要放大）：成功用例只覆盖**正常收尾**；**失败路径**由故障注入用例
   覆盖（探针 D 证明它有效）。收尾断言检查的是**本用例登记过的那些后端 PID**（`_transaction_pids`），
   不涉及、也不误操作其他连接——所以它证明的是"这一轮的连接没有停在事务里"，不是"整个实例干净"。
3. **收尾 DDL 有自己的锁等待上限**。`DROP SCHEMA` 要 `ACCESS EXCLUSIVE`，而上面那些
   `statement_timeout` 设在并发用例的连接上、**管不到管理连接**；管理连接（`create_test_admin_engine()`）
   本身不带任何超时。所以 `DROP SCHEMA` 之前显式 `SET LOCAL lock_timeout = '5s'`。
   探针 G 实测：往 schema 的表上留一把 `ACCESS SHARE` 锁，收尾以
   `canceling statement due to lock timeout` 报错、全程 9 秒——**有界报错，而不是无限等锁**。
   报错信息不被包装，因为它本身就是"还有残留事务"的直接线索。

**证据（实测）**：

- 故障注入用例 `test_a_failing_case_that_leaves_tasks_running_is_still_cleaned_up`：故意不清理，
  把残局交给夹具；探针 D 把取消改成空操作后，它在 teardown 报
  `收尾没有很快完成（10.0s）——取消挂起任务没有生效`——**10 秒（有界）而不是无限**，
  这正是第 1 层在兜底、第 2 层失效就会红。
- 超时路径用例 `test_a_blocked_round_cancels_within_its_own_timeout_and_frees_the_lock`：`_collect`
  必须在自己的 0.5s 超时附近收场。两条判据都是**功能性的**：失败之后那条行锁必须立刻能被别人拿到
  （`_assert_lock_is_free`，1s `lock_timeout` 内必须成功），且**本用例登记过的 PID** 没有停在
  `idle in transaction`。"任务被取消了"不算证据。
  **范围**：前者只证明**被检查的那一条锁**已释放，不等于"所有连接都没有未结束的事务"；
  两者合起来覆盖的是"本用例开的那些连接"。
- **收尾 DDL 的锁等待上限**（第 3 层）：探针 G 往 schema 的表上留一把 `ACCESS SHARE` 锁，
  `DROP SCHEMA` 在 5s 后以 `canceling statement due to lock timeout` 报错、全程 9 秒。
  没有这一层时，同样的情形就是**无限等锁**——那正是最初那次 600 秒挂死的机制。
- 探针 E 如实记一条**边界**：把会话级的两条超时去掉之后**只有**那条断言用例变红，其余全绿——
  也就是说这道安全网在当前用例集里**不承担任何断言**，它只是防止将来某次真正卡住的锁把测试挂死。
  不把它说成"网关"。

**并发用例的判定仍然只看最终提交的数据**（`_mismatched_chunk_count` / 读回断言），
`blocked` 之类"观察到阻塞"只是辅助信息——这条纪律没有因为加了超时而放松。
