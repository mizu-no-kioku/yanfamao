"""资源码到表/字段的映射：YAML 声明，代码上限裁决。

YAML 只能在上限内取子集。越界的表、列或永久拒绝项一律报错，
而不是被静默裁剪——静默裁剪会让配置看起来生效、实际不同。
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from yuxi.nl2sql import table_policy

POLICY_PATH = Path(__file__).with_name("resource_policy.yaml")


class ResourcePolicyError(ValueError):
    """资源策略越界或结构非法。"""


@dataclass(frozen=True)
class ResourceGrant:
    resource_code: str
    tables: frozenset[str]
    columns: Mapping[str, frozenset[str]]
    max_rows: int


def build_policy(payload: dict) -> tuple[dict[str, ResourceGrant], dict[str, tuple[str, ...]], frozenset[int]]:
    """把 YAML 载荷构建为策略；任何越界项直接报错。"""
    if payload.get("version") != 1:
        raise ResourcePolicyError("资源共享配置必须使用 version 1")

    menu_ids = payload.get("menu_ids") or {}
    gate_ids = frozenset(int(value) for value in menu_ids.values())
    if gate_ids != frozenset({253, 254, 238}):
        raise ResourcePolicyError(f"功能门菜单 ID 与约定不一致: {sorted(gate_ids)}")

    menu_binding: dict[str, tuple[str, ...]] = {}
    for menu_code, codes in (payload.get("menu_binding") or {}).items():
        menu_binding[str(menu_code)] = tuple(str(code) for code in codes)

    resources: dict[str, ResourceGrant] = {}
    for resource_code, spec in (payload.get("resources") or {}).items():
        tables: dict[str, frozenset[str]] = {}
        for table, columns in (spec.get("tables") or {}).items():
            if not table_policy.has_complete_row_rule(table):
                raise ResourcePolicyError(f"{resource_code} 引用了不可开放的表: {table}")
            bound = table_policy.TABLE_UPPER_BOUNDS[table]
            requested = frozenset(str(column).upper() for column in columns)
            denied = requested & table_policy.PERMANENTLY_DENIED_COLUMNS
            if denied:
                raise ResourcePolicyError(f"{resource_code}.{table} 含永久拒绝字段: {sorted(denied)}")
            if not requested <= bound.columns:
                raise ResourcePolicyError(
                    f"{resource_code}.{table} 超出代码上限: {sorted(requested - bound.columns)}"
                )
            tables[table] = requested
        max_rows = int(spec.get("max_rows", 200))
        if max_rows > 200:
            raise ResourcePolicyError(f"{resource_code}.max_rows 超过 200")
        resources[str(resource_code)] = ResourceGrant(
            resource_code=str(resource_code),
            tables=frozenset(tables),
            columns=tables,
            max_rows=max_rows,
        )
    return resources, menu_binding, gate_ids


_PAYLOAD = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
RESOURCE_POLICY, MENU_TO_RESOURCES, AI_GATE_MENU_IDS = build_policy(_PAYLOAD)


def resolve_grants(resource_codes: Collection[str]) -> tuple[ResourceGrant, ...]:
    """按资源码取授权；未声明的资源码不产生授权。"""
    return tuple(RESOURCE_POLICY[code] for code in sorted(set(resource_codes)) if code in RESOURCE_POLICY)
