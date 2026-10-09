# 对外 API 参考文档归属

状态：implemented
类型：process
Owner：docs/advanced/api-key-integration.md

## 问题

外部集成开发者需要在认证、Agent 调用、异步队列、SSE 和生产接入之间建立一条完整路径。只依赖 Swagger 无法解释幂等、排队、终态回读和权限边界；另建一份与 API Key 页面并列的完整清单又会复制现有事实。

## 决策

### 实现方案

`docs/advanced/api-key-integration.md` 是对外接入的入口与跨领域约束 Owner，统一说明服务发现、认证、Agent Call、通用 Run、Channel、状态、幂等、错误处理和上线检查。运行实例的 OpenAPI 只拥有当前路由和其中已经声明的 Schema；动态响应、异常与恢复语义由对应路由、服务、专题文档和测试拥有。

知识库导入与外部查询的完整接口继续由 `docs/advanced/knowledge-base-api.md` 拥有，对外接入页只保留能力摘要和链接。导航沿用 `/advanced/api-key-integration` 路径，避免破坏既有入站链接。

## 替代方案

- 新建 `external-api.md` 并保留原 API Key 页面：会重复认证、Agent Call 和错误处理。
- 把全部 OpenAPI 路径手写进 Markdown：覆盖面更大，但形成易漂移的并行契约。
- 只提供 Swagger：字段查找直接，但不能表达排队、SSE、权限、超时和生产边界。

## 后果

集成开发者从一个导航入口获得端到端调用路径，领域细节仍由各自 Owner 维护。页面明确指出 OpenAPI 未必包含动态响应和全部异常状态，自动生成客户端前需要审查目标接口的声明完整性。

文档需要随对外路由、认证、Request/Run 状态或 SSE 恢复语义同步更新。新增领域 API 时，优先更新领域 Owner，再判断是否需要在本页增加入口摘要。

## 验证

- `GET /api/system/health`、`ready`、`discovery`、`/docs` 和 `/openapi.json` 在运行实例返回 `200`；readiness 显示 PostgreSQL、Redis、worker 与必需组件正常。
- 运行实例 OpenAPI 包含 Agent Call、通用 Run、结果、SSE 和 API Key 管理入口，共发现 226 条路径。
- 未认证访问 `/api/auth/me`、Agent Call 和 API Key 列表均返回 `401`。
- `pnpm --dir docs run build`：首次正文版本 Passed。Reviewer 修正后的最终版本复跑时，沙箱禁止 VitePress 创建 `docs/node_modules/vue` 符号链接；宿主权限请求未获批准，因此最终版本的完整构建为 Not run。修正仅涉及 Markdown 内容，没有改动 VitePress 配置结构。
- `git diff --check`：Passed。
- `scripts/verify_engineering_contracts.py` 及其 unittest：Not run。宿主 `uv` 安装 Python 时发生跨盘 rename 错误；API 容器没有挂载仓库根 `scripts/`，且 `uv run` 无权修改容器锁文件或 site-packages。
- 独立 Reviewer 对照路由和 diff 审查后发现的异步排队、OpenAPI 边界、无 ID SSE、图片校验和知识库 Owner 重复问题均已修正。
