"""多租户部署开关的**单一事实源**。

放在 `yuxi` 包内是必需的：API（`server.utils.tenant_context_middleware`）与 worker
（`yuxi.services.run_worker`）都要读它，而 `package/yuxi` **不依赖** `server`。两边必须用
同一份解析逻辑，否则"开关是开还是关"会有两个答案。

**坏配置 fail-closed**：只接受明确列出的真值 / 假值 / 缺省值，**非空未知值**（例如拼错的
`ture`）抛错。若把未知值当成"关闭"，一个以为打开了多租户的部署会静默地以默认租户运行——
这正是"看起来正常其实没有"的那一类失败。
"""

from __future__ import annotations

import os

MULTI_TENANT_ENV = "YUXI_MULTI_TENANT"

# 明确接受的真值 / 假值。
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def multi_tenant_enabled() -> bool:
    """多租户部署开关（严格解析）。

    - 真值：`1` / `true` / `yes` / `on`（大小写不敏感，去首尾空白）。
    - 假值：`0` / `false` / `no` / `off`。
    - **未设置或空值**：视为未设置，默认关闭（与 `reconciliation_seconds_from_env` 的约定一致）。
    - **非空未知值**：抛 `RuntimeError`。

    坏配置必须 fail-closed：把未知值当成"关闭"会让以为开了多租户的部署静默地以默认租户运行。
    """

    raw = os.getenv(MULTI_TENANT_ENV)
    if raw is None or not raw.strip():
        return False
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeError(
        f"{MULTI_TENANT_ENV}={raw!r} 不是可识别的布尔值。"
        f"接受真值 {sorted(_TRUE_VALUES)}、假值 {sorted(_FALSE_VALUES)}，或留空（=关闭）。"
        "拒绝非空未知值，是为了避免拼写错误让多租户部署静默地以默认租户运行。"
    )


def validate_multi_tenant_config() -> bool:
    """在启动时解析开关一次，坏值立即抛错（不等到第一个请求）。返回解析结果。"""

    return multi_tenant_enabled()
