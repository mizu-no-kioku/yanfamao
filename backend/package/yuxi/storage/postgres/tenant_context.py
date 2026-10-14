"""租户上下文。

独立成模块是必需的：`models_business` / `models_knowledge` 的 `tenant_id` 列默认值要读
它，而那两个模块被 `manager` 导入——放在 manager 里会形成循环导入。

**两个访问器，别混用**：

- `explicit_tenant_id()`：**显式**设置的租户，未设置返回 `None`。运行时数据库会话用它决定
  是否注入 `app.tenant_id`——缺上下文**不**注入，RLS 因此按"无租户"处理（读 0 行、写被拒）。
- `current_tenant_id()`：ORM **列默认值**用的租户，未设置回落默认租户。只服务"建表 / SQLite
  单测 / 迁移"这类没有会话上下文、又需要一个合法非空值的插入路径。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

# 默认租户：单租户部署与全部存量数据都归属它。认证计划接入宿主 Claim 之前，所有会话都用它。
DEFAULT_TENANT_ID = 1
DEFAULT_TENANT_EXTERNAL_ID = "default"
DEFAULT_TENANT_ISSUER = "local"

_TENANT_CONTEXT: ContextVar[int | None] = ContextVar("yuxi_tenant_id", default=None)


def explicit_tenant_id() -> int | None:
    """**显式**设置的租户；未设置返回 `None`（**不**回落默认租户）。

    运行时数据库会话的唯一租户来源（`_apply_tenant_context`）。缺上下文与"租户 1"必须可区分，
    否则"忘了建立上下文"会静默变成"以租户 1 运行"。显式建立租户的入口只有：API 请求中间件、
    API / worker 启动的全局初始化，以及 worker 消费任务时**由消息携带的路由提示**。
    """

    return _TENANT_CONTEXT.get()


def current_tenant_id() -> int:
    """ORM 列默认值用的租户：未显式设置时回落默认租户。

    **这不是运行时数据库会话的租户来源**（那个用 `explicit_tenant_id()`）。它只服务
    "建表 / SQLite 单测 / 迁移"这类没有会话上下文、又需要一个合法非空值的插入路径——
    `tenant_id` 已是 NOT NULL，回落成 `None` 会让这些插入直接失败。真正的跨租户写入由
    RLS `WITH CHECK` 拒绝，不靠这里的回落。
    """

    return _TENANT_CONTEXT.get() or DEFAULT_TENANT_ID


@contextmanager
def use_tenant(tenant_id: int):
    """在作用域内切换租户。认证计划用它注入宿主 Claim 解析出的租户。"""

    token = _TENANT_CONTEXT.set(tenant_id)
    try:
        yield
    finally:
        _TENANT_CONTEXT.reset(token)


@contextmanager
def clear_tenant_context():
    """在作用域内**显式清除**租户上下文（而不是"不设置"）。

    "不设置"只在"本来就没有"时才等于"无租户"；若外层作用域（更外层中间件、复用的任务
    context）已经设过租户，`explicit_tenant_id()` 会**继承**它。免租户端点（探活 / 就绪 /
    文档）必须真正无上下文，所以这里显式置空，并在退出时还原外层值。
    """

    token = _TENANT_CONTEXT.set(None)
    try:
        yield
    finally:
        _TENANT_CONTEXT.reset(token)
