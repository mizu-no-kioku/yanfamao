# 可访问知识库列表项与卡片字段同源

状态：implemented
类型：bug-fix
Owner：backend/server/utils/knowledge_response.py

## 问题

普通用户登录后，共享知识库里明明有文件，卡片却显示「0 文件」、没有创建时间前缀，共享范围标签
恒为「只读用户(0)」。

三处现象同一个原因。普通用户的知识库列表来自 `GET /api/knowledge/databases/accessible`（跨库全量
列表 `GET /api/knowledge/databases` 以管理员为底座），该处理函数当时手工拼 8 个字段，不含
`stats` / `row_count` / `created_at` / `share_config`；而卡片读的是
`database.row_count || 0`（`web/src/views/DataBaseView.vue` 的 `cardSubtitle`）与
`getShareConfigLabel(database.share_config)`（同文件 `cardTags`），于是分别落到 `0` 和
`getShareConfigLabel(undefined)` 的 `用户(0)` 兜底分支。管理员路径走
`serialize_knowledge_base_list`，字段齐全，所以只有普通用户看到假数据。

事实层有数，缺的只是投影。本轮对真实 PostgreSQL 的只读查询：研发知识库
`stats = {row_count: 5, file_count: 5}`、通用知识库 `stats = {row_count: 9, file_count: 9}`；
两者的 `share_config` 分别是 `department [1, 2]` 与 `global`。该接口原本是给智能体配置选库用的窄
投影，被知识库列表页复用后没有跟着补字段——列表项此前已经为前端补过 `scope` 与 `can_manage` 一轮。

## 决策

列表项改为复用 `serialize_knowledge_base`，与管理员列表同源，并补上由知识库类型决定的
`supports_documents`；**不按权限裁剪字段**。前端在拿不到共享范围时不渲染共享标签，而不是拿空对象
拼一个假标签。

### 实现方案

- **列表项构造**（`backend/server/utils/knowledge_response.py` 的
  `serialize_accessible_knowledge_base`）：调用既有序列化器并带 `row_count_fallback=True`，于是
  `row_count` 在 `stats.row_count` 为空时退回 `file_count`——与管理员列表同一条规则；随后补上
  `supports_documents`。该字段来自知识库类型而不是权限，所以由调用方注入，序列化器不依赖 manager。
  该函数的 docstring 明确记下「不要在这里按权限裁剪字段」及其原因，避免后来者重新引入。
- **路由**（`get_accessible_databases`）：用列表推导替换原来手写的 8 字段字典，`try/except` 与
  状态码行为不变；docstring 从「用于智能体配置」改为「知识库列表页与智能体配置共用」，因为它
  本来就是普通用户的列表路径。
- **前端判据**（`web/src/utils/shareConfig.js` 的 `getShareTagLabel`）：`share_config` 存在且非空时
  给出 `getShareConfigLabel` 的结果，否则返回 `null`；`DataBaseView.vue` 的 `cardTags` 据此决定是否
  push 该标签，不再无条件渲染。

## 替代方案

- **把 `GET /api/knowledge/databases` 的底座从 `get_admin_user` 放宽到 `get_required_user`，让前端
  永远走一条接口**。不采用：与已实现的边界冲突——
  [知识库 API 文档](../../../advanced/knowledge-base-api.md)明确「跨库的全量视图（知识库列表、导图库
  列表、全局统计）仍是管理员专属；普通用户可见的知识库列表走 `/databases/accessible`」，
  `web/test/unit/knowledge_scope_access.test.js` 还有断言把 `getDatabases` 钉在管理员通道。
  改这条是一次授权面扩张，需要单独决定，不该顺路做。
- **按权限收窄：`share_config` 只发给能管理该库的调用者（本分支先按此实现，实测后放弃）**。
  放弃的原因：**它买不到保密性**。同一份 `share_config`（含 `user_uids` / `department_ids`）在
  `GET /api/knowledge/databases/{kb_id}` 对只读调用者照样返回——那个处理函数传的
  `redact_secrets` 只作用于 `additional_params`，`share_config` 是无条件输出的
  （`knowledge_response.py` 的 `serialize_knowledge_base`）。于是收窄只剩两个代价：让两个暴露同一
  资源的接口对同一份数据给出不同契约，以及让卡片对只读库干脆不显示共享范围（比修复前信息更少）。
  真正的边界要收就得连详情接口一起收，那是另一次授权面决定。**本记录把「按权限裁剪字段」记为
  已知的诱人错误**：它看起来像最小暴露原则，实际只制造接口分歧。
- **保留手写投影，只补 `row_count` 与 `created_at`**。不采用：同一张卡片继续吃两套形状，这次漏的是
  统计与时间，下次还会漂；而且 `stats` 的九个字段要在两处各维护一遍。
- **前端只在 `can_manage` 时渲染标签**。不采用：这样普通用户自己建的库与只读库都要靠权限判断，
  而共享范围本身不按权限裁剪；判据应当是「有没有这份数据」，这样它也顺带挡住缺字段的旧响应。

## 后果

- 普通用户列表项的字段集合与管理员列表一致（多一个 `supports_documents`），两处不再各自维护。
- **`/databases/accessible` 的响应变大**：除统计外还带上了 `share_config`、`query_params`、
  `metadata`、`embedding_model_spec` 等既有序列化字段。该接口的两个既有消费者（智能体配置
  `stores/agent.js`、工作区文件树 `WorkspaceView.vue`）只用 `kb_id` / `name` /
  `supports_documents`，多字段无影响。只读调用者收到的 `additional_params` 已在 manager 层脱敏。
- **只读调用者现在拿得到库的共享范围，界面也据此显示真实范围**（如「只读部门(2)」）。相比修复前
  的假「只读用户(0)」，这是信息更多且不再有假信息；与详情接口的现状一致。定向分享名单本身由
  `share_config` 携带，本次**没有**收紧它，也不声称收紧了。
- **面比「与详情接口一致」这句话更宽，这一条要按它的实际形状评估**：修复前，普通用户要拿到某个库的
  名录得为那个库单独发一次 `GET /databases/{kb_id}`；现在**一次 `/databases/accessible` 响应就带上
  他可见的每个库的 `share_config`**（`user_uids` / `department_ids`），每次打开列表都会带上。
  可见的那批库没有变，变的是「一次拿全部」而不是「逐个拿」。这是接受本次方向时应当被明确称量的
  代价；若要收，收的应是 `share_config` 本身的下发范围（详情接口一起收），而不是只在列表这一侧裁。
- 卡片不显示共享标签的情形只剩一种：响应里没有可用的共享范围（旧客户端缓存、字段缺失）。
- **管理员列表路径未改**：仍对所有库（包括管理员自己只读的库）返回 `share_config`。

## 验证

`Passed` 表示命令实际执行且结果已核对；`Not run` 表示未执行并说明原因。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 列表项带上卡片要的统计与创建时间 | 有文件的库继续显示「0 文件」 | `serialize_accessible_knowledge_base` | `test/unit/routers/test_knowledge_accessible_list.py::test_accessible_item_carries_card_fields` | RED（实现前实测）：`KeyError: 'row_count'` | Passed |
| 共享范围不按权限裁剪（只读与可管理同契约） | 列表与详情对同一资源给出不同契约；卡片对只读库丢信息 | 同上（无 `can_manage` 分支） | `::test_accessible_item_carries_share_config_regardless_of_permission[read\|manage]` | RED（收窄实现下实测）：`[read]` 失败于 `assert None == 2`；`[manage]` 通过——正是被裁掉的那一侧 | Passed |
| 真实 HTTP 上普通用户拿到的字段与管理员列表同源 | 单元层换了内存对象，证明不了真实链路与真实 stats 聚合 | `get_accessible_databases` + manager 可见性查询 | `test/integration/api/test_knowledge_router.py::test_accessible_list_item_carries_database_card_fields`（真实登录凭证 + 真实 PostgreSQL） | RED（恢复旧投影后重跑）：`assert None == 1`——`item.get("row_count")` 为 `None`，同一时刻管理员列表对该库给出 `1` | Passed |
| 真实 HTTP 上列表与详情给出同一份共享范围 | 两个接口对同一资源分歧 | 同上 | `::test_accessible_list_share_config_matches_database_detail`（断言只读调用者拿到的 `share_config` 与详情响应逐字段相等） | RED（收窄实现下实测）：`KeyError: 'share_config'` | Passed |
| 真实 HTTP 上自己建的库带上共享范围 | 普通用户的个人库丢标签 | 同上 | `::test_accessible_list_keeps_share_config_for_owned_database` | RED（原始未修版本实测）：`KeyError: 'share_config'` | Passed |
| 卡片不再从缺失数据拼出共享标签 | 缺字段时显示「只读用户(0)」 | `getShareTagLabel` | `web/test/unit/database_card_share_tag.test.js`（有范围给真标签；缺字段与空对象都返回 `null`；视图只由该判据决定） | RED（实现前实测）：`SyntaxError: does not provide an export named 'getShareTagLabel'` | Passed |
| 真实接口对本次上报的两个库给出非零文件数 | 修复只在测试夹具上成立 | `get_accessible_databases` + stats 持久化 | 本地真实 HTTP 实测（见下节「上报现场的实测」） | —（实现前该接口对该用户不返回该字段） | Passed |
| 文档与导航的链接与构建 | 决策记录里的相对链接指错层级 | 本记录 + `docs/` | `cd docs && node node_modules/vitepress/bin/vitepress.js build` → `build complete`，退出 0（重跑仍退出 0） | RED（首次实测）：`1 dead link(s) found`——`../../advanced/knowledge-base-api` 少一层，构建退出 1 | Passed |
| 本次改动没有破坏既有行为 | 全量回归出现新增失败 | 全部 | `pytest test/unit -m "not slow" --ignore test/unit/services/test_run_worker.py` → 结果见下；`pytest test/integration/api/test_knowledge_router.py` → **49 passed**；`pnpm run test:unit` → **422 passed / 0 failed**；`pnpm run lint:check` 退出 0；`pnpm run build` 退出 0；`python3 scripts/verify_engineering_contracts.py` 退出 0 | 基线对照即负向案例 | 见下 |
| 真实浏览器中的卡片呈现 | 端到端界面表现 | 前端 | `Not run` | — | Not run |

全量 unit 回归（含本次新增用例 / 去掉本次新增文件后的基线）：

- `pytest test/unit -m "not slow" --ignore test/unit/services/test_run_worker.py` →
  **2460 passed, 58 skipped**；同一条命令再加
  `--ignore test/unit/routers/test_knowledge_accessible_list.py` → **2457 passed, 58 skipped**。
  差值 3 即本次新增文件收集到的 3 条用例（两个测试函数 + 一条按权限参数化两例），全量套件没有其他差异。

`Not run` 的原因说明：

- **真实页面与视觉验证未执行**：本仓库没有浏览器自动化能力（`backend/test/e2e` 是 HTTP/agent 级
  用例，仓库内无 Playwright 等依赖），本次执行环境也没有浏览器。未验证的内容是普通账号进入知识库
  列表页后的实际像素呈现。`pnpm run build` 通过只证明 SFC 能编译，不构成可视断言。界面结论目前由
  三段证据拼出：真实接口现在返回 `row_count` / `created_at` / 真实共享范围（下节实测）、卡片读的是
  `database.row_count`、共享标签只在有共享范围时渲染（单元用例覆盖后两者）。
- **全量 unit 回归排除了 `test/unit/services/test_run_worker.py`**：与
  [上一份记录](./2026-10-07-personal-kb-share-picker.md)同一口径。本环境直接跑 `pytest test/unit -m
  "not slow"` 会在这条用例上停滞：两次起跑都超过 25 分钟仍无输出、没走到断言（其中一次由我中止），
  加 `--ignore` 后 50 秒跑完。该文件与本次改动无关（未触碰 run/worker），但**它在本环境的停滞
  没有结论**，只说明「本次回归未覆盖它」。
- **`python3 -m unittest scripts.test_verify_engineering_contracts` 在本次环境失败 1 例**，报错是
  `OSError: [WinError 1314] 客户端没有所需的特权`，位置在用例自己 `os.symlink` 造越界目录时。
  这是 Windows 需要开发者模式/管理员权限的环境限制，与本次改动无关（本次未触碰 `scripts/`）；
  同一脚本的 `python3 scripts/verify_engineering_contracts.py` 退出 0。
- **`ruff` 在 api 容器里需临时装**（镜像只装运行期依赖，无 dev 组），本次用
  `uv run --no-sync --with ruff ruff check --no-cache` 执行，本次改动涉及的四个文件全部 `All checks
  passed`。该命令另外报出 4 处**改动前已存在**、本次未触碰的文件（`package/yuxi/storage/postgres/manager.py:602`
  与 `test/integration/api/test_model_provider_uid_header.py:89` 的 `E501`、未跟踪数据脚本
  `test/data/公共财政政策结构化标注数据集.py` 的两条 `UP0xx`）；按「不清理与本次无关的既有问题」的约定未处理。

上报现场的实测（真实 HTTP + 真实数据，用管理员凭证临时创建一个研发部门普通账号，查完即删）：

- `GET /api/knowledge/databases/accessible` 对该账号返回（只摘卡片用的字段）
  `{"name": "研发知识库", "row_count": 5, "stats.file_count": 5, "created_at": "2026-09-24T06:05:32.705762Z", "can_manage": false, "share_config.read_scope.access_level": "department"}`
  与
  `{"name": "通用知识库", "row_count": 9, "stats.file_count": 9, "created_at": "2026-09-21T09:01:25.999549Z", "can_manage": false, "share_config.read_scope.access_level": "global"}`
  ——正是截图中显示「0 文件」的两个库。两者的卡片标签现在来自真实的 `read_scope`（部门与全局），
  不再是拼出来的「只读用户(0)」；`can_manage` 均为 `false`，即普通用户在这两个共享库上最多只读。
- 临时账号已删除（`DELETE /api/auth/users/{id}` → 200）。凭证经环境变量注入，不入库、不入文档。

执行方式上的教训（本次踩到，记下来免得重复）：

- **命令的退出码要单独取，别从管道尾读**：本次一次 docs build 用 `... | tail` 执行，看到的是
  `[exited with code 0]`，实际是 `tail` 的退出码；去掉管道后同一条命令真实退出 1，就是上表那条死链。
