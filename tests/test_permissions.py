"""`can()` against every job title, with an allow and a denial for each.

Section 1's done-when: "can() has a test for every role and a denial test for
every role". The answer must come from the database (membership_roles ->
role_permissions), never from a hard-coded table in the route.
"""

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models.access import (
    MembershipRoleLink,
    Permission,
    PlatformGrant,
    Role,
    RolePermission,
)
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.models.sacco import Sacco
from app.models.user import User
from app.security.permissions import (
    OWNER_PLATFORM,
    PERMISSIONS,
    ROLE_PERMISSIONS,
    can,
    permissions_for,
    sync_catalogue,
)

_counter = iter(range(1, 100_000))


@pytest.fixture(scope="module", autouse=True)
def catalogue(app):
    """Write the published catalogue into the create_all database once."""
    with app.app_context():
        sync_catalogue()
    yield


def make_user(owner=False):
    n = next(_counter)
    user = User(
        email=f"perm{n}@example.com",
        password_hash="x",
        is_platform_owner=owner,
    )
    db.session.add(user)
    db.session.commit()
    return user


def make_sacco():
    sacco = Sacco(name=f"Perm Sacco {next(_counter)}")
    db.session.add(sacco)
    db.session.commit()
    return sacco


def make_membership(user, sacco, job_title=None, status=MembershipStatus.ACTIVE):
    """A membership, optionally holding one job title from `roles`."""
    membership = SaccoMembership(
        user_id=user.id,
        sacco_id=sacco.id,
        role=MembershipRole.MEMBER,  # deliberately not the job title
        status=status,
    )
    db.session.add(membership)
    db.session.commit()
    if job_title is not None:
        role = db.session.query(Role).filter_by(code=job_title).one()
        db.session.add(
            MembershipRoleLink(membership_id=membership.id, role_id=role.id)
        )
        db.session.commit()
    return membership


def holder(job_title, status=MembershipStatus.ACTIVE):
    """(user, sacco) for someone holding `job_title` in a fresh SACCO."""
    user, sacco = make_user(), make_sacco()
    make_membership(user, sacco, job_title=job_title, status=status)
    return user, sacco


ALLOWS = [
    ("sacco_admin", "savings.post"),
    ("secretary", "audit.view"),
    ("finance_officer", "loan.disburse"),
    ("loans_officer", "loan.approve"),
    ("member", "loan.request"),
]

DENIALS = [
    ("sacco_admin", "loan.disburse"),
    ("secretary", "savings.post"),
    ("finance_officer", "member.manage"),
    ("loans_officer", "savings.post"),
    ("member", "audit.view"),
]


@pytest.mark.parametrize("job_title,permission", ALLOWS)
def test_each_role_is_allowed_what_it_holds(job_title, permission):
    user, sacco = holder(job_title)
    assert can(user, sacco, permission) is True


@pytest.mark.parametrize("job_title,permission", DENIALS)
def test_each_role_is_denied_what_it_does_not_hold(job_title, permission):
    user, sacco = holder(job_title)
    assert can(user, sacco, permission) is False


def test_the_answer_comes_from_the_database_not_the_enum():
    """A membership whose enum says member, but whose roles say SACCO admin."""
    user, sacco = holder("sacco_admin")
    membership = db.session.query(SaccoMembership).filter_by(user_id=user.id).one()
    assert membership.role is MembershipRole.MEMBER
    assert can(user, sacco, "savings.post") is True


def test_the_sacco_admin_holds_support_approve_and_a_member_does_not():
    """The Access requests page decides by this name (frontend `lib/perms.js`)."""
    admin_user, sacco = holder("sacco_admin")
    member_user = make_user()
    make_membership(member_user, sacco, job_title="member")

    assert can(admin_user, sacco, "support.approve") is True
    assert can(member_user, sacco, "support.approve") is False
    assert "support.approve" in permissions_for(admin_user, sacco)
    assert "support.approve" not in permissions_for(member_user, sacco)


def test_inactive_membership_is_denied_everything():
    user, sacco = holder("sacco_admin", status=MembershipStatus.INACTIVE)
    assert can(user, sacco, "savings.view") is False
    assert can(user, sacco, "member.view") is False
    assert permissions_for(user, sacco) == frozenset()


def test_a_non_member_is_denied():
    user, sacco = make_user(), make_sacco()
    assert can(user, sacco, "savings.view") is False


def test_another_saccos_role_grants_nothing():
    user, sacco = holder("sacco_admin")
    other = make_sacco()
    assert can(user, other, "savings.view") is False


def test_platform_owner_holds_the_platform_permissions_only_without_consent():
    user = make_user(owner=True)
    sacco = make_sacco()
    assert permissions_for(user, sacco) == OWNER_PLATFORM
    data = PERMISSIONS.keys() - OWNER_PLATFORM
    assert not any(can(user, sacco, code) for code in data)


def test_a_platform_grant_alone_is_enough():
    """platform_grants is the authority, not the legacy boolean."""
    user = make_user(owner=False)
    db.session.add(PlatformGrant(user_id=user.id))
    db.session.commit()
    assert can(user, make_sacco(), "sacco.manage") is True


def test_an_unknown_permission_is_denied():
    user, sacco = holder("sacco_admin")
    assert can(user, sacco, "loan.delete") is False
    assert can(user, sacco, "SAVINGS.POST") is False


def test_no_user_is_denied():
    assert can(None, make_sacco(), "savings.view") is False


def test_a_membership_without_roles_falls_back_to_the_legacy_column():
    """Rows written before membership_roles existed keep working."""
    user, sacco = make_user(), make_sacco()
    membership = SaccoMembership(
        user_id=user.id,
        sacco_id=sacco.id,
        role=MembershipRole.STAFF,
        status=MembershipStatus.ACTIVE,
    )
    db.session.add(membership)
    db.session.commit()

    assert can(user, sacco, "audit.view") is True  # secretary holds it
    assert can(user, sacco, "savings.post") is False


def role_permission_map() -> dict[str, set[str]]:
    """Every role's permissions, read back from the database."""
    rows = db.session.execute(
        select(Role.code, Permission.code)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .join(Permission, Permission.id == RolePermission.permission_id)
    ).all()
    mapping: dict[str, set[str]] = {}
    for role_code, permission_code in rows:
        mapping.setdefault(role_code, set()).add(permission_code)
    return mapping


def test_every_role_in_the_catalogue_is_seeded_with_its_permissions():
    resolved = role_permission_map()
    for code, expected in ROLE_PERMISSIONS.items():
        assert resolved.get(code) == set(expected), code


def test_sync_catalogue_is_idempotent():
    written = sync_catalogue()
    assert (written.roles, written.permissions, written.grants) == (0, 0, 0)
