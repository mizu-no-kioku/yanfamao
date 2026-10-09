# 对外 API 接入参考

本页面向把研发猫接入业务系统、机器人网关、自动化平台或内部服务的开发者。完成接入后，调用方可以用 API Key 发现服务能力、调用 Agent、查询异步结果、消费 Server-Sent Events（SSE）并检索有权限的知识库。

本文记录稳定的对外入口、响应语义和调用约束。部署实例的 `<base-url>/openapi.json` 用于发现当前路由和查询已声明的请求 Schema，可视化版本位于 `<base-url>/docs`；部分动态响应和异常状态没有完整写入 OpenAPI，以本页说明、实际响应和相关路由为准。知识库接口见[文档导入与查询 API](./knowledge-base-api.md)，运行队列的内部状态与恢复语义见[Agent 请求队列与调度设计](../mechanisms/agent-request-queue.md)。

## 接入前提

调用方需要：

- 一个 `/api/system/ready` 返回 `200` 的研发猫实例；
- 一个已绑定部门、能访问目标 Agent 或知识库的专用用户；
- 绑定该用户的 API Key；
- 生产环境中的 HTTPS 地址；
- 对异步调用可持久保存 `request_id`、`run_id` 和 `thread_id` 的存储。

以下示例使用：

```bash
BASE_URL=https://yuxi.example.com
API_KEY=yxkey_<your-secret>
```

`BASE_URL` 不包含结尾 `/api`。业务接口统一使用 `/api` 前缀。

## 协议与认证

普通请求使用 JSON 和 Bearer 认证：

```http
Content-Type: application/json
Accept: application/json
Authorization: Bearer yxkey_<your-secret>
```

SSE 请求使用 `Accept: text/event-stream`。时间使用 ISO 8601 字符串。调用方应把服务端返回的资源 ID 当作不透明字符串。

受保护接口接受 API Key 和 JWT。以 `yxkey_` 开头的 Bearer token 按 API Key 校验，其他 Bearer token 按 JWT 校验。API Key 绑定具体用户，请求继承该用户的角色、部门和资源权限，不绕过 Agent、知识库、线程或文件的可见性检查。

缺少、失效、过期或已撤销的凭证返回 `401`。用户未绑定部门时返回 `400`。角色或资源权限不足时返回 `403`，部分按可见性隐藏资源的接口返回 `404`。

## 服务发现与健康检查

以下接口无需认证：

| 方法 | 路径 | 含义 |
| --- | --- | --- |
| `GET` | `/api/system/health` | API 进程存活，不代表依赖或业务链路可用 |
| `GET` | `/api/system/ready` | API、PostgreSQL、Redis、兼容 worker 和必需组件已就绪 |
| `GET` | `/api/system/discovery` | 服务版本、API 前缀、能力和关键端点 |
| `GET` | `/openapi.json` | 当前路由和已声明请求 Schema；不保证覆盖全部动态响应与异常状态 |
| `GET` | `/docs` | Swagger UI |

上线探针使用：

```bash
curl --fail "$BASE_URL/api/system/ready"
```

成功响应包含 `status: ready`，并在 `checks` 中报告 `startup`、`postgres`、`redis` 和 `worker`。`health=200` 只能用于进程 liveness；`ready=503` 时调用方应暂停提交新 Run。

## 创建和管理 API Key

登录 Web 后，可在“设置 → API Keys”创建凭证。也可以先用 JWT 调用：

```http
POST /api/user/apikey/
Authorization: Bearer <your-jwt>
Content-Type: application/json

{
  "request_id": "crm-integration-2026",
  "name": "外部客服系统",
  "expires_at": "2027-01-01T00:00:00Z"
}
```

`request_id` 是创建意图的幂等标识，长度为 8–64 个字符，只接受字母、数字、`.`、`_`、`:` 和 `-`。同一用户用相同 `request_id` 和相同参数重试时返回同一创建事实；相同 ID 对应不同参数时返回冲突。

```json
{
  "api_key": {
    "id": 12,
    "key_prefix": "yxkey_abcdef",
    "name": "外部客服系统",
    "user_id": 3,
    "is_enabled": true
  },
  "secret": "yxkey_<your-secret>"
}
```

服务端只保存 secret 的哈希和前缀。调用方应立即把 secret 写入密钥管理器，不得写入源码、镜像、URL 或日志。创建响应丢失时，可以用同一用户、同一 `request_id` 和相同参数重试。Key 已撤销、主密钥已轮换或创建意图改变时，服务端拒绝重放。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/user/apikey/` | 列出当前用户可见的 Key |
| `POST` | `/api/user/apikey/` | 创建 Key |
| `GET` | `/api/user/apikey/{api_key_id}` | 读取 Key 元数据 |
| `PUT` | `/api/user/apikey/{api_key_id}` | 修改名称、过期时间或启用状态 |
| `DELETE` | `/api/user/apikey/{api_key_id}` | 撤销 Key |

列表和详情只返回 `key_prefix`，不会再次返回完整 secret。`last_used_at` 表示最近一次成功认证时间。删除用户或撤销 Key 后，旧 secret 立即失效。

## 选择 Agent

调用方使用 Agent 的 `slug`，不使用数据库自增 ID。查询当前用户可见的 Agent：

```http
GET /api/agent
Authorization: Bearer yxkey_<your-secret>
```

实例管理员可能调整默认 Agent 和可见范围，生产集成应显式配置并在启动时校验目标 `agent_slug`。

## Agent Call：推荐入口

Agent Call 适合一次请求获得一次 Agent 输出。它接受 OpenAI 风格的 `messages`，但不是完整的 OpenAI `/v1/chat/completions` 协议。

### 同步调用

```bash
curl --fail "$BASE_URL/api/agent-invocation/agent-call/runs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_slug": "default-chatbot",
    "messages": [{"role": "user", "content": "请总结这段文字：……"}],
    "request_id": "crm-call-2026-0001",
    "async_mode": false
  }'
```

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `agent_slug` | string | 是 | — | 目标 Agent slug |
| `messages` | array | 是 | — | 服务端取最后一条 `user` 消息 |
| `stream` | boolean | 否 | `false` | 当前只能为 `false`，传 `true` 返回 `422` |
| `agent_call_meta` | object | 否 | `{}` | 外部元数据；禁止包含 `context` |
| `thread_id` | string | 否 | 自动生成 | 复用时保持多轮上下文 |
| `request_id` | string | 否 | UUID | 幂等和追踪标识，最长 64 字符 |
| `model_spec` | string | 否 | Agent 配置 | 本次模型覆盖 |
| `tool_approval_mode` | string | 否 | Agent/线程配置 | 本次工具审批模式覆盖 |
| `async_mode` | boolean | 否 | `false` | `true` 时立即返回运行信息 |
| `queue_policy` | string | 否 | 同步 `reject`，异步 `enqueue` | 同步只接受 `reject` |

成功响应包含：

```json
{
  "run_id": "<run-id>",
  "agent_slug": "default-chatbot",
  "thread_id": "<thread-id>",
  "status": "completed",
  "request_id": "crm-call-2026-0001",
  "output": "……",
  "choices": [{
    "index": 0,
    "messages": [{"role": "assistant", "content": "……"}],
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 120,
    "completion_tokens": 40,
    "total_tokens": 160
  }
}
```

`usage` 只在服务端取得完整 token 统计时有效。调用方应同时检查 HTTP 状态和业务 `status`。

### 多模态输入

`messages[].content` 可以是字符串，也可以是文本和图片数组：

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "请描述这张图片"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,<base64-data>"}}
  ]
}
```

纯文本数组同样可用。不支持的 content part、缺少 `user` 消息或无效字段结构返回 `422`。请求边界不保证解码并验证图片字节；损坏图片可能在 worker 或模型提供方处理时失败。生产网关的请求体上限需要覆盖实际图片大小。

### 异步调用

长任务或需要排队的调用设置：

```json
{
  "agent_slug": "default-chatbot",
  "messages": [{"role": "user", "content": "生成一份分析报告"}],
  "request_id": "report-2026-0001",
  "async_mode": true,
  "queue_policy": "enqueue"
}
```

异步响应存在两种形态：

- 线程空闲并立即派发时，响应包含 `run_id`，调用方直接查询 Run 结果；
- 线程繁忙且 `queue_policy=enqueue` 时，响应表示 Request 已排队，`run_id` 可能为空，并提供 `request_id` 和 `request_events_url`。调用方先查询 `/api/agent/requests/{request_id}` 或消费 Request SSE，取得后续创建的 `run_id`，再切换到 Run 结果查询。

取得 `run_id` 后查询：

```http
POST /api/agent-invocation/agent-call/runs/result
Authorization: Bearer yxkey_<your-secret>
Content-Type: application/json

{"run_id":"<run-id>","agent_slug":"default-chatbot"}
```

`agent_slug` 可省略；传入时不匹配会返回 `409`。结果不是终态时，调用方按退避策略继续轮询。

同步等待超时时返回 `504`，错误体的 `detail.run.agent_run_id` 是后续查询所需的 Run ID。`504` 表示 HTTP 等待超时，不表示 Run 失败，也不应创建一个新的重复请求。

## 通用 Run：队列、SSE 和取消

需要排队状态、实时事件、取消或恢复时，使用通用 Run API。

### 创建线程

```bash
curl --fail "$BASE_URL/api/chat/thread" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "thread-create-2026-0001",
    "agent_id": "default-chatbot",
    "title": "外部系统会话",
    "metadata": {}
  }'
```

响应的 `id` 是 `thread_id`。同一 `thread_id` 关联多轮上下文；不同最终用户不能复用同一线程。

### 提交运行

```bash
curl --fail "$BASE_URL/api/agent/runs" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "你好，请介绍一下你自己",
    "agent_slug": "default-chatbot",
    "thread_id": "<thread-id>",
    "meta": {"request_id": "crm-run-2026-0001"},
    "queue_policy": "enqueue"
  }'
```

| 字段 | 说明 |
| --- | --- |
| `query` | 用户文本 |
| `agent_slug` | Agent slug |
| `thread_id` | 当前用户拥有的线程 |
| `meta.request_id` | 请求幂等和追踪标识；省略时生成 UUID |
| `image_content` | 单个 base64 图片或数组；最多 10 张，总量约 80 MB |
| `model_spec` | 本次模型覆盖 |
| `tool_approval_mode` | 本次工具审批模式覆盖 |
| `queue_policy` | `enqueue`、`reject` 或 `steer`，默认 `enqueue` |
| `resume` | LangGraph 恢复载荷；非空时不进入普通 Request 队列 |
| `created_by_run_id` | 恢复时指定被恢复的 Run |

普通请求先持久化为 Request 和用户消息。线程空闲时响应包含 `run_id` 和 `stream_url`；线程忙碌且使用 `enqueue` 时返回排队状态和 `request_events_url`。排队中的 Request 还没有可查询结果的 Run ID。

### Request 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/agent/requests/{request_id}` | 查询 Request |
| `GET` | `/api/agent/thread/{thread_id}/requests?agent_slug=<slug>` | 查询线程队列 |
| `GET` | `/api/agent/requests/{request_id}/events` | 订阅排队 SSE |
| `POST` | `/api/agent/requests/{request_id}/cancel` | 取消排队请求 |
| `POST` | `/api/agent/requests/{request_id}/steer` | 把排队请求用于 steer |

排队事件通知请求何时产生 `run_id`。取得 Run ID 后切换到 Run 查询或 Run SSE。

### Run 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/agent/runs/{run_id}` | 读取 Run 状态快照 |
| `GET` | `/api/agent/runs/{run_id}/result` | 读取该 Run 的最终输出 |
| `GET` | `/api/agent/runs/{run_id}/events` | 订阅 Run SSE |
| `POST` | `/api/agent/runs/{run_id}/cancel` | 请求取消 Run |
| `GET` | `/api/agent/thread/{thread_id}/active_run` | 查询线程活跃 Run |

结果接口只读，不会重复执行任务。最终输出必须从同一 `run_id` 的结果接口读取，不要从线程最近消息或相邻 Run 推断。

### 消费 SSE

```bash
curl --no-buffer --fail "$BASE_URL/api/agent/runs/<run-id>/events?verbose=false" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Accept: text/event-stream'
```

SSE 帧包含 `event` 和 `data`；Redis 持久事件通常还包含 `id`，heartbeat、部分补偿终态和错误帧可以没有 `id`。客户端需要：

- 忽略以 `:` 开头的 heartbeat；
- 只在帧包含 `id` 时更新游标，断线重连时发送最后一个 `Last-Event-ID` 或使用 `after_seq`；
- 收到 `end` 或 `error` 后调用结果接口回读最终事实；
- 容忍没有 `id` 的终态和错误帧；有 ID 的事件按 ID 去重，其他帧按业务对象和终态幂等；
- 为代理关闭响应缓冲并允许长连接。

`verbose=false` 返回面向普通客户端的精简载荷。SSE 是实时事件平面，持久化的 Run 状态和结果才是最终业务事实。

## Channel 消息入口

机器人或消息网关可以提交纯文本信封：

```http
POST /api/agent-invocation/channel/messages
Authorization: Bearer yxkey_<your-secret>
Content-Type: application/json

{
  "channel": "customer-service",
  "account_id": "tenant-a",
  "chat_id": "chat-10001",
  "sender_id": "user-90001",
  "message_id": "message-30001",
  "agent_slug": "default-chatbot",
  "message": {"type": "text", "text": "请查询订单状态"},
  "queue_policy": "steer"
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `channel` | `cli` | 渠道名称，最长 32 字符 |
| `account_id` | `default` | 渠道账号或租户标识 |
| `chat_id` | `null` | 渠道会话标识，用于稳定映射线程 |
| `thread_id` | `null` | 显式指定 Yuxi 线程 |
| `sender_id` | `null` | 渠道发送者标识 |
| `message_id` | `null` | 渠道消息 ID，最长 128 字符 |
| `request_id` | 自动生成 | 幂等 ID，最长 64 字符 |
| `agent_slug` | — | 目标 Agent slug |
| `message` | — | 当前只支持 `type=text` |
| `queue_policy` | `steer` | `enqueue`、`reject` 或 `steer` |

Channel 支持 `/state` 和 `/approve` 控制命令。等待普通用户回答的 `ask_user_question` 中断不受 Channel 支持，返回 `409` 和错误码 `ask_user_question_unsupported`。Channel 不等待最终输出，调用方继续用返回的 Request/Run 标识查询。

## 知识库 API

API Key 可以调用绑定用户有权限的知识库列表、文件浏览、片段检索、全文打开、文件内查找以及文档导入接口。完整路径、请求参数、权限和知识库类型限制由[文档导入与查询 API](./knowledge-base-api.md)维护；本页不复制该领域契约。

## 状态、幂等和重试

| 标识 | 用途 |
| --- | --- |
| `request_id` | 幂等提交、排障和排队状态查询 |
| `run_id` | 一次实际执行、SSE、取消和最终结果 |
| `thread_id` | 多轮上下文和线程级 FIFO |
| SSE `id` | 断线续传游标和事件去重 |

调用方应为每个业务提交生成稳定且唯一的 `request_id`，网络超时后用同一个 ID 重试。不要为同一业务动作的每次 HTTP 重试生成新 ID。

| 状态 | 含义 | 调用方动作 |
| --- | --- | --- |
| `queued` | Request 等待线程 FIFO | 订阅或查询 Request |
| `pending` / `dispatched` | 已创建 Run，等待 worker 或刚投递 | 查询 Run |
| `running` | worker 正在执行 | 继续等待 |
| `interrupted` | 等待审批或人机输入 | 根据中断类型恢复 |
| `completed` | 成功终态 | 读取同一 Run 的结果 |
| `failed` | 失败终态 | 记录错误，按业务规则决定重试 |
| `cancel_requested` | 已请求取消 | 查询直到终态 |
| `cancelled` | 取消终态 | 停止等待 |
| `rejected` | 队列策略拒绝 | 稍后重试或改用异步 `enqueue` |

未知状态按“未完成且需要观察”处理，不能默认成功。

| HTTP 状态 | 常见含义 | 建议处理 |
| --- | --- | --- |
| `400` | 账号缺部门、参数语义错误或能力不支持 | 修正账号或请求 |
| `401` | 凭证无效、过期或已撤销 | 更换凭证，停止当前重试 |
| `403` | 权限不足 | 调整专用用户权限 |
| `404` | 资源不存在或不可见 | 检查 ID 与用户归属 |
| `409` | 幂等、忙碌或状态冲突 | 根据错误体处理 |
| `422` | Schema 或字段组合无效 | 修正请求 |
| `429` | 频率受限 | 遵守 `Retry-After` 并退避 |
| `500` | 服务端异常 | 使用同一 request_id 有界重试 |
| `503` | 服务未 ready | 暂停新请求，轮询 readiness |
| `504` | 同步等待超时 | 使用已有 run_id 继续查询 |

错误体通常使用 FastAPI 的 `detail` 字段；`detail` 可以是字符串或对象，客户端需要兼容两种形态。

## 客户端与生产建议

- 连接超时使用较短值；无法接受长读取时使用异步 Agent Call。
- SSE 不使用短读取超时，通过 heartbeat 判断连接活性。
- `5xx`、连接中断和超时使用指数退避与随机抖动，并限制最大次数。
- `4xx` 默认不重试；`409`、`429` 和同步 `504` 按具体语义处理。
- 日志记录状态码、耗时和三个业务 ID，过滤 `Authorization`、图片 base64、用户敏感输入和工具敏感输出。
- 生产系统只访问 HTTPS 域名，不直接暴露公网 HTTP `5050`。
- 反向代理允许 `Authorization`、`Content-Type`、`Accept` 和 `Last-Event-ID`，为 SSE 关闭缓冲。
- 每个外部系统使用独立、可撤销、带过期时间的 API Key 和最小权限专用用户。
- 监控 `/api/system/ready`、错误率、调用延迟以及长期未终止的 Run。

生产代理、TLS 与端口边界由[生产部署](./deployment.md)维护。

## 最小验收流程

1. 请求 `/api/system/discovery` 和 `/api/system/ready`，确认版本、能力和依赖就绪。
2. 用 API Key 请求 `/api/auth/me`，确认绑定用户、角色和部门符合预期。
3. 请求 `/api/agent`，确认目标 `agent_slug` 对该用户可见。
4. 用唯一 `request_id` 执行异步 Agent Call；已派发时保存 `run_id`，返回 `queued` 时先保存 `request_id` 并查询 Request 或消费 Request SSE，直到取得 `run_id`。
5. 取得 `run_id` 后轮询 `/api/agent-invocation/agent-call/runs/result`，直到 `completed`，核对 `output`。
6. 检查客户端日志，确认没有完整 API Key 或敏感请求内容。

## 契约与验证入口

- 运行实例路由与已声明请求 Schema：`<base-url>/openapi.json`
- 运行实例 Swagger：`<base-url>/docs`
- [API 路由装配](https://github.com/xerrors/Yuxi/blob/main/backend/server/routers/__init__.py)
- [认证依赖](https://github.com/xerrors/Yuxi/blob/main/backend/server/utils/auth_middleware.py)
- [Agent Call 路由](https://github.com/xerrors/Yuxi/blob/main/backend/server/routers/agent_invocation_call_router.py)
- [通用 Run 路由](https://github.com/xerrors/Yuxi/blob/main/backend/server/routers/agent_router.py)
- [Channel 路由](https://github.com/xerrors/Yuxi/blob/main/backend/server/routers/agent_invocation_channel_router.py)
- [外部知识库路由](https://github.com/xerrors/Yuxi/blob/main/backend/server/routers/external_kb_router.py)
- [API Key HTTP integration](https://github.com/xerrors/Yuxi/blob/main/backend/test/integration/api/test_apikey_router.py)
- [Agent Call E2E](https://github.com/xerrors/Yuxi/blob/main/backend/test/e2e/test_agent_call_entrypoints_e2e.py)

生成客户端前需要审查目标接口在 OpenAPI 中是否声明了响应模型和异常状态；当前部分对外路由返回动态对象，不能只依赖自动生成类型。升级实例时同时比较 OpenAPI、本页约束和集成测试结果。
