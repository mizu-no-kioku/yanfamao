"""多租户开关的严格解析，以及多租户模式下免租户端点的放行。

开关解析是 fail-closed 的第一道：拼错的 `ture` 若被当成"关闭"，一个以为打开了多租户的部署会
静默地以默认租户运行。中间件的路径豁免保证编排器探活不会因为 503 而把部署判死。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

from yuxi.config.multi_tenant import MULTI_TENANT_ENV, multi_tenant_enabled, validate_multi_tenant_config
from yuxi.storage.postgres.tenant_context import DEFAULT_TENANT_ID, explicit_tenant_id, use_tenant

from server.utils.tenant_context_middleware import TenantContextMiddleware

pytestmark = pytest.mark.unit


def _set_switch(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(MULTI_TENANT_ENV, raising=False)
    else:
        monkeypatch.setenv(MULTI_TENANT_ENV, value)


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
def test_switch_accepts_truthy_values(monkeypatch, raw):
    _set_switch(monkeypatch, raw)

    assert multi_tenant_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", " no ", "off"])
def test_switch_accepts_falsy_values(monkeypatch, raw):
    _set_switch(monkeypatch, raw)

    assert multi_tenant_enabled() is False


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_switch_treats_unset_or_blank_as_default_off(monkeypatch, raw):
    _set_switch(monkeypatch, raw)

    assert multi_tenant_enabled() is False


@pytest.mark.parametrize("raw", ["ture", "tru", "enable", "2", "yess", "on1"])
def test_switch_rejects_unknown_non_blank_values(monkeypatch, raw):
    """非空未知值必须抛错，而不是静默当成"关闭"——否则多租户部署会静默以默认租户运行。"""
    _set_switch(monkeypatch, raw)

    with pytest.raises(RuntimeError, match=MULTI_TENANT_ENV):
        multi_tenant_enabled()


def test_startup_validation_surfaces_a_typo(monkeypatch):
    """启动校验走同一条解析：拼错的值在启动时就抛错，不等到第一个请求。"""
    _set_switch(monkeypatch, "ture")

    with pytest.raises(RuntimeError, match=MULTI_TENANT_ENV):
        validate_multi_tenant_config()


def _middleware() -> TenantContextMiddleware:
    return TenantContextMiddleware(app=lambda *args, **kwargs: None)


async def _call_with_path(middleware: TenantContextMiddleware, path: str):
    seen: dict[str, object] = {}

    async def call_next(request):
        seen["tenant_during_call"] = explicit_tenant_id()
        return JSONResponse(status_code=200, content={"ok": True})

    request = SimpleNamespace(url=SimpleNamespace(path=path))
    response = await middleware.dispatch(request, call_next)
    return response, seen


async def test_multi_tenant_mode_blocks_business_requests(monkeypatch):
    _set_switch(monkeypatch, "true")

    response, seen = await _call_with_path(_middleware(), "/api/knowledge/list")

    assert response.status_code == 503
    assert seen == {}


@pytest.mark.parametrize(
    "path",
    ["/api/system/health", "/api/system/ready", "/api/system/discovery", "/docs", "/redoc", "/openapi.json"],
)
async def test_multi_tenant_mode_lets_infrastructure_endpoints_through(monkeypatch, path):
    """探活 / 就绪 / 文档端点必须放行，否则编排器会把部署判死，且观察不到原因。"""
    _set_switch(monkeypatch, "true")

    response, seen = await _call_with_path(_middleware(), path)

    assert response.status_code == 200
    # 免租户端点**不带**上下文运行：将来误加业务表读取会 fail-closed。
    assert seen["tenant_during_call"] is None


async def test_single_tenant_mode_establishes_the_default_tenant(monkeypatch):
    _set_switch(monkeypatch, None)

    response, seen = await _call_with_path(_middleware(), "/api/knowledge/list")

    assert response.status_code == 200
    assert seen["tenant_during_call"] == DEFAULT_TENANT_ID


async def test_tenant_free_endpoint_clears_an_inherited_tenant_context(monkeypatch):
    """免租户端点必须**显式清除**上下文，而不是"不设置"。

    外层作用域（更外层中间件、复用的任务 context）若已设过租户，`explicit_tenant_id()` 会继承
    它——那时"不带上下文运行"就是假的，探活端点会在某个租户的上下文里执行。
    """
    _set_switch(monkeypatch, "true")

    with use_tenant(42):
        response, seen = await _call_with_path(_middleware(), "/api/system/health")
        # 端点执行期间确实无上下文；退出后外层上下文被还原（不污染调用方）。
        assert explicit_tenant_id() == 42

    assert response.status_code == 200
    assert seen["tenant_during_call"] is None
