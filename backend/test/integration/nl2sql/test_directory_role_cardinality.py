"""针对隔离测试库：角色基数按原始关系表计数，不被菜单 JOIN 放大。"""

import pytest

from yuxi.nl2sql import errors
from yuxi.nl2sql.policy_resolver import BusinessSubject, resolve_function_gate
from yuxi.nl2sql.test_database import require_test_database


@pytest.fixture
def directory():
    from yuxi.nl2sql.mysql_directory import MySqlBusinessDirectory, test_database_connection_factory

    config = require_test_database()
    return MySqlBusinessDirectory(test_database_connection_factory(config))


def test_role_with_many_menus_still_counts_as_one_role(directory):
    """用户 1001 的角色 3001 关联全部菜单，仍必须解析为一个角色。"""
    subject = BusinessSubject(user_id=1001, tenant_id=10, staff_id=2001)
    assert resolve_function_gate(subject, directory) == (3001,)


def test_user_without_gate_menu_is_denied(directory):
    """用户 1002 的角色只有非 AI 菜单。"""
    subject = BusinessSubject(user_id=1002, tenant_id=10, staff_id=2002)
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_function_gate(subject, directory)
    assert exc.value.code == errors.FUNCTION_GATE_DENIED


def test_user_with_exactly_one_gate_menu_passes_under_any_match(directory):
    """用户 1003 的角色只关联 QA(254)。任意命中规则下必须通过。"""
    subject = BusinessSubject(user_id=1003, tenant_id=10, staff_id=2003)
    assert resolve_function_gate(subject, directory) == (3003,)


def test_relation_pointing_to_inactive_role_is_denied(directory):
    """用户 1007 的关系指向已失效角色 3004。"""
    subject = BusinessSubject(user_id=1007, tenant_id=10, staff_id=2009)
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_function_gate(subject, directory)
    assert exc.value.code == errors.ROLE_INACTIVE
