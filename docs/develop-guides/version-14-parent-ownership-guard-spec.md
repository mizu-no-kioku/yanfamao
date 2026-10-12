# 父行侧归属防护：归属链盘点、更新路径与锁顺序（版本 14 数据约束门槛 · 第 1 步）

**本文只完成门槛的第 1 步（盘点 + 锁顺序设计）。第 2 步——按 test-first 实现双向一致性保护、
并用真实 PostgreSQL 的两个并发事务证明顺逆序都不提交失配数据——尚未完成。**
在它完成前，不得宣布"引用一致性约束完整"，也不得对开发库执行迁移。

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
4. 父行不存在时保持现状：`FOR SHARE` 查不到行 → 交给外键报错，避免用"引用不一致"盖掉
   "父行不存在"这个更准确的原因。

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

——以上五条 + 第 2 节的更新路径补完，构成"引用一致性并发门槛"；全部通过前不接线版本 14，
开发库保持版本 13。
