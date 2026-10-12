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
   **"静默级联迁移归属"**。该链的父侧检查不能去比 run_items 的"行自身 kb"（那是个会跟着变的
   子查询，恒等），必须落在它的**另一个引用**上：某个 run_item 指向的 `dataset_item` 的 kb
   ≠ `NEW.kb_id` 时拒绝。
2. **`knowledge_bases.kb_id`**：它是根，子表以 `ON DELETE CASCADE` 引用它。改它属于改**键**，
   外键（NO ACTION）本身就会挡住——不需要新增防护，但要记名：这条链的保护来自外键而非本机制。

## 2. 更新路径盘点（哪些路径能改到归属字段）

| 路径 | 能否改 `kb_id` | 依据 |
|---|---|---|
| `KnowledgeFileRepository.update_fields` / `update_fields_if_status` | **能** | `_writable_fields`（:174-175）含 `kb_id`；`_sanitize_data`（:204-208）按白名单过滤后 `update_fields` 逐个 `setattr`（:749-750） |
| `KnowledgeChunkRepository` 的 upsert 路径 | **能** | `_writable_fields`（:16-19）含 `kb_id`；upsert 对已存在行执行更新 |
| 裸 SQL / 迁移脚本 / 运维脚本 | **能**（白名单管不到） | 这是"删白名单"方案不成立的原因 |
| 图谱 / 评估域各 repository 的更新路径 | **未核对** | 全仓只有上面两处 `_writable_fields` 定义，这两域的更新走的是别的机制，本轮**没有**逐个追完 |

**未核对就是未核对**：上面第 4 行是缺口，实施父侧防护前必须补完（否则会漏掉一条更新路径）。

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
3. **锁顺序只有一种：父行本身。** 子侧拿父行的 `FOR SHARE`；父侧由 `UPDATE` 自身拿本行锁，
   对子行**只做不加锁的读**。不存在"父侧反过来锁子行"的路径，所以**死锁面是被结构消除的**，
   而不是靠"约定按某个顺序加锁"——后者在跨表链（如 files→chunks→mentions）上很容易失守。
4. 父行不存在时保持现状：`FOR SHARE` 查不到行 → 交给外键报错，避免用"引用不一致"盖掉
   "父行不存在"这个更准确的原因。

### 必须由真实 PostgreSQL 用例证明的部分

锁模式与检查时机**不能靠推理定稿**：`FOR SHARE` 与 `FOR NO KEY UPDATE` 的冲突关系、
`READ COMMITTED` 下并发事务各自的可见性、以及自引用链（`knowledge_files.parent_id`）上
触发器对同表的递归可见性，都要在隔离实例上跑出来。**在这些用例通过之前，本设计只是设计。**

## 4. 明确契约（含需要裁决的一处行为）

- **有既有子行时修改父行归属**：会被父侧触发器**明确拒绝**（`23514`），不做级联改写。
- **子行插入/改引用 × 父行改归属并发**：两种先后顺序都不得提交失配数据（靠第 3 节的锁）。
- **`evaluation_run_items.dataset_item_id` 置空**与 `ON DELETE SET NULL` 触发的更新**必须仍然合法**
  ——子侧规则里"引用为 NULL 时跳过"这条不能被新增的锁改动破坏。
- **无子行且满足其他约束的归属修改**：按**允许**实现（人类伙伴未反对，本文按此定契约）。
  理由：那是显式的、可审计的单行变更，不产生不一致；**批量迁移归属必须另走显式流程**，
  不允许通过通用 `update_fields` 隐式搬走整条归属链。
- repository 侧继续给出**可读错误**，数据库是**最终强制边界**。

## 5. 验收清单（第 2 步必须覆盖，逐条要能红）

1. 有既有子行时修改父行归属 → 失配必须被拒绝。
2. 子行插入 × 父行归属修改**并发**，两种先后顺序都不能提交失配数据。
3. 子行**修改父引用** × 父行归属修改并发。
4. `evaluation_run_items.dataset_item_id` 置空及 `ON DELETE SET NULL` 仍合法。
5. 无子行且满足其他约束的归属修改 → 行为符合第 4 节的契约。

——以上五条 + 第 2 节的更新路径补完，构成"引用一致性并发门槛"；全部通过前不接线版本 14，
开发库保持版本 13。
