"""租户上下文。

独立成模块是必需的：`models_business` / `models_knowledge` 的 `tenant_id` 列默认值要读
它，而那两个模块被 `manager` 导入——放在 manager 里会形成循环导入。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

# 默认租户：单租户部署与全部存量数据都归属它。认证计划接入宿主 Claim 之前，所有会话都用它。
DEFAULT_TENANT_ID = 1
DEFAULT_TENANT_EXTERNAL_ID = "default"
DEFAULT_TENANT_ISSUER = "local"

_TENANT_CONTEXT: ContextVar[int | None] = ContextVar("yuxi_tenant_id", default=None)


def current_tenant_id() -> int:
    """当前租户。

    未显式设置时回落默认租户——单租户部署、未接入宿主认证前的所有会话，以及 ORM 在
    没有请求上下文时（脚本、种子数据）的插入，都靠这条回落。**不能**改成返回 None：
    `tenant_id` 已是 NOT NULL，回落成 None 会让所有插入直接失败。
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
