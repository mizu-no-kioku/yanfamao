"""身份链：用户、租户消歧、唯一员工，任一不成立即拒绝。"""

import pytest

from yuxi.nl2sql import errors
from yuxi.nl2sql.policy_resolver import resolve_business_subject
from yuxi.nl2sql.principal import BusinessPrincipal


class FakeDirectory:
    def __init__(self, *, active_users=(), tenants_by_user=None, staff_by_key=None):
        self.active_users = set(active_users)
        self.tenants_by_user = tenants_by_user or {}
        self.staff_by_key = staff_by_key or {}

    def find_active_user(self, user_id):
        return user_id in self.active_users

    def find_tenants_for_user(self, user_id):
        return tuple(self.tenants_by_user.get(user_id, ()))

    def find_active_staff_ids(self, user_id, tenant_id):
        return tuple(self.staff_by_key.get((user_id, tenant_id), ()))


def deny_code(exc_info):
    return exc_info.value.code


def test_missing_user_is_denied():
    directory = FakeDirectory(active_users=())
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_business_subject(BusinessPrincipal(user_id=1001), directory)
    assert deny_code(exc) == errors.IDENTITY_USER_MISSING


def test_user_without_tenant_relation_is_denied():
    directory = FakeDirectory(active_users={1001}, tenants_by_user={})
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_business_subject(BusinessPrincipal(user_id=1001), directory)
    assert deny_code(exc) == errors.IDENTITY_USER_MISSING


def test_multi_tenant_user_without_explicit_tenant_is_denied():
    directory = FakeDirectory(active_users={1001}, tenants_by_user={1001: (10, 20)})
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_business_subject(BusinessPrincipal(user_id=1001), directory)
    assert deny_code(exc) == errors.TENANT_AMBIGUOUS


def test_multi_tenant_user_with_explicit_tenant_resolves():
    directory = FakeDirectory(
        active_users={1001}, tenants_by_user={1001: (10, 20)}, staff_by_key={(1001, 20): (2005,)}
    )
    subject = resolve_business_subject(BusinessPrincipal(user_id=1001, tenant_id=20), directory)
    assert (subject.user_id, subject.tenant_id, subject.staff_id) == (1001, 20, 2005)


def test_requested_tenant_not_owned_by_user_is_denied():
    directory = FakeDirectory(active_users={1001}, tenants_by_user={1001: (10,)})
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_business_subject(BusinessPrincipal(user_id=1001, tenant_id=20), directory)
    assert deny_code(exc) == errors.IDENTITY_USER_MISSING


def test_single_tenant_is_inferred_without_explicit_tenant():
    directory = FakeDirectory(active_users={1001}, tenants_by_user={1001: (10,)}, staff_by_key={(1001, 10): (2001,)})
    subject = resolve_business_subject(BusinessPrincipal(user_id=1001), directory)
    assert subject.tenant_id == 10


def test_missing_staff_is_denied():
    directory = FakeDirectory(active_users={1001}, tenants_by_user={1001: (10,)})
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_business_subject(BusinessPrincipal(user_id=1001), directory)
    assert deny_code(exc) == errors.IDENTITY_STAFF_MISSING


def test_multiple_staff_in_tenant_is_ambiguous():
    directory = FakeDirectory(
        active_users={1001}, tenants_by_user={1001: (10,)}, staff_by_key={(1001, 10): (2001, 2002)}
    )
    with pytest.raises(errors.PolicyDenial) as exc:
        resolve_business_subject(BusinessPrincipal(user_id=1001), directory)
    assert deny_code(exc) == errors.IDENTITY_STAFF_AMBIGUOUS
