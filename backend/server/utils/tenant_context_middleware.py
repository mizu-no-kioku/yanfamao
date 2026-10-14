"""请求级租户上下文：**唯一**决定"这个请求以哪个租户运行"的地方。

之前租户上下文由 storage 层的隐式回落（未设置 → 租户 1）决定，导致"忘了建立上下文"与
"以租户 1 运行"不可区分。现在回落被去掉（见 `_apply_tenant_context`），改由本中间件在请求
边界**显式**建立——单租户 / 遗留部署显式建立默认租户，多租户部署必须由验签后的宿主 Claim
决定租户（阶段 2 接入）。

多租户开关打开但 Claim 解析尚未接入时**直接拒绝**，而不是默认成租户 1——否则一个多租户部署
会把所有请求都当成租户 1 处理，那是最危险的一种"看起来正常"。

**开关本身也必须 fail-closed**：只接受明确列出的真值 / 假值 / 缺省值，**非空未知值**（例如拼错
的 `ture`）在启动时抛错。若把未知值当成"关闭"，一个以为打开了多租户的部署会静默地以默认租户
运行——与上面要根除的失败模式同源。

**免租户端点**：多租户模式下并非所有 HTTP 请求都该 503。进程存活 / 就绪 / 能力发现 / 文档这类
端点要么纯进程内、要么只做 `SELECT 1` 之类的依赖探测，不读按租户隔离的业务表；它们必须放行，
否则编排器探活失败会把整个部署判死，连"为什么 503"都观察不到。它们**显式清除**租户上下文
（`clear_tenant_context()`，而不是"不设置"）——外层作用域若已设过租户，"不设置"会**继承**它，
"无上下文"就只是名义上的。
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from yuxi.config.multi_tenant import (
    MULTI_TENANT_ENV as MULTI_TENANT_ENV,
    multi_tenant_enabled as multi_tenant_enabled,
    validate_multi_tenant_config as validate_multi_tenant_config,
)
from yuxi.storage.postgres.tenant_context import DEFAULT_TENANT_ID, clear_tenant_context, use_tenant

# 多租户模式下**免租户**的精确路径：进程存活 / 就绪 / 能力发现 / OpenAPI 文档。
# 它们是基础设施端点，不读按租户隔离的业务表；放行它们不影响"业务请求必须带租户"。
TENANT_FREE_PATHS = frozenset(
    {
        "/api/system/health",
        "/api/system/ready",
        "/api/system/discovery",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)


def _is_tenant_free(path: str) -> bool:
    """路径是否属于免租户端点（精确匹配，避免前缀误放行）。"""

    normalized = path.rstrip("/") or "/"
    return normalized in TENANT_FREE_PATHS


class TenantContextMiddleware(BaseHTTPMiddleware):
    """在请求作用域内建立租户上下文。

    中间件在依赖与端点之前执行，所以 `get_current_user` 读 `users` 时租户上下文已经就位
    （认证引导需要读凭证，而凭证按租户隔离）。多租户部署缺 Claim 解析时返回 503——**免租户
    端点除外**。
    """

    async def dispatch(self, request, call_next):
        if multi_tenant_enabled():
            if _is_tenant_free(request.url.path):
                # **显式清除**而不是"不设置"：外层作用域若已设过租户，这里必须真正无上下文。
                with clear_tenant_context():
                    return await call_next(request)
            return JSONResponse(
                status_code=503,
                content={"detail": "多租户部署需要宿主租户声明（Claim 解析尚未接入）"},
            )
        with use_tenant(DEFAULT_TENANT_ID):
            return await call_next(request)
