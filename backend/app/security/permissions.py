"""The permission catalogue and `can()` (Section 1, Neo).

This module is the interface the rest of the team builds against:

    can(user, sacco, permission, resource=None) -> bool

It answers from the database, not from a Python table: the membership's rows in
`membership_roles` decide which `roles` the caller holds, `role_permissions`
decides what those roles may do, and `platform_grants` decides platform access.
A membership that is not `active` is denied everything, including the
permissions its roles would otherwise grant.

The `PERMISSIONS` and `ROLE_PERMISSIONS` tables below are the catalogue: the
migration seeds the same data, `sync_catalogue()` writes it into a database that
was built with `create_all` (tests), and `tests/test_access_schema.py` fails if
the two ever disagree.

`resource` is accepted for object-level rules (a single loan, a single member)
that arrive with a later section. Until such a rule exists the answer is the
role-based one; callers may pass it now without changing their call shape.
"""

from dataclasses import dataclass

from sqlalchemy import func, select

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

# One entry per thing that may be allowed. `area.action`, lower case.
PERMISSIONS: dict[str, str] = {
    "sacco.view": "Read this SACCO's details",
    "sacco.manage": "Create, edit, activate or deactivate a SACCO",
    "member.view": "Read the SACCO's member list",
    "member.manage": "Add a member, invite a phone, change a role or status",
    "savings.view": "Read savings accounts and their history",
    "savings.post": "Post deposits, withdrawals, interest and fees",
    "savings.reverse": "Reverse a posted ledger entry",
    # Reverse an entry even when it would leave savings below an open loan's cap.
    # Needs a written reason and is audited.
    "savings.reverse_override": "Override the loan-cap block on a reversal",
    "savings.request": "Send a deposit or withdrawal request for my own account",
    "loan.view": "Read loan requests and their decisions",
    "loan.request": "Request a loan",
    "loan.approve": "Approve or reject one step of a loan",
    "loan.disburse": "Disburse an approved loan",
    "loan.repay": "Repay a loan",
    "audit.view": "Read the audit trail",
    "dashboard.view": "See the dashboard for the caller's role",
    "report.view": "Read the SACCO's reports",
    "sacco.update": "Rename the SACCO",
    # Held by no job title: only the platform owner (who holds every permission)
    # adds, removes or edits a SACCO admin.
    "admin.manage": "Make, change or remove a SACCO admin",
    "savings.view_all": "Read every savings account in the SACCO",
    # The SACCO's consent to a platform owner reading its records: its admin
    # approves, denies or revokes a support-access request.
    "support.approve": "Approve or revoke a platform owner's support access",
    # Where members pay in. Never held by the platform owner: a second officer of
    # the SACCO must approve every change.
    "payin.manage": "Set or approve where members pay in",
}

# The five job titles, each with the permissions it holds (docs/roles-permissions.md):
# a SACCO admin never approves loans and cannot read the audit log; the
# secretary is read-only and the only SACCO role that reads the audit log.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "sacco_admin": frozenset(
        {
            "sacco.view",
            "member.view",
            "member.manage",
            "savings.view",
            "savings.post",
            "savings.reverse",
            "savings.reverse_override",
            "loan.view",
            "report.view",
            "savings.view_all",
            "sacco.update",
            "support.approve",
            "payin.manage",
            "dashboard.view",
        }
    ),
    "secretary": frozenset(
        {
            "sacco.view",
            "member.view",
            "savings.view",
            "loan.view",
            "audit.view",
            "report.view",
            "savings.view_all",
            "dashboard.view",
        }
    ),
    "finance_officer": frozenset(
        {
            "sacco.view",
            "savings.view",
            "savings.post",
            "savings.reverse",
            "loan.view",
            "loan.disburse",
            "loan.repay",
            "report.view",
            "savings.view_all",
            "payin.manage",
            "dashboard.view",
        }
    ),
    "loans_officer": frozenset(
        {
            "sacco.view",
            "member.view",
            "savings.view",
            "loan.view",
            "loan.approve",
            "report.view",
            "dashboard.view",
        }
    ),
    "member": frozenset(
        {
            "sacco.view",
            "savings.view",
            "savings.request",
            "loan.request",
            "loan.repay",
            "dashboard.view",
        }
    ),
}

# Never held by a SACCO admin membership, even through another title.
ADMIN_NEVER = frozenset({"loan.approve", "loan.disburse", "audit.view"})

ROLE_NAMES = {
    "sacco_admin": "SACCO administrator",
    "secretary": "Secretary",
    "finance_officer": "Finance officer",
    "loans_officer": "Loans officer",
    "member": "Member",
}

# Bridge for rows written before `membership_roles` existed (and for databases
# built with create_all, where the migration's backfill never ran).
LEGACY_ROLE_CODES = {
    MembershipRole.SACCO_ADMIN: "sacco_admin",
    MembershipRole.STAFF: "secretary",
    MembershipRole.MEMBER: "member",
}


@dataclass(frozen=True)
class CatalogueWrite:
    roles: int
    permissions: int
    grants: int


def sync_catalogue(commit: bool = True) -> CatalogueWrite:
    """Insert the catalogue above into the database if it is missing.

    Idempotent: existing rows are left alone, which is what makes it safe on a
    database the migration already seeded.
    """
    role_rows = {role.code: role for role in db.session.execute(select(Role)).scalars()}
    permission_rows = {
        permission.code: permission
        for permission in db.session.execute(select(Permission)).scalars()
    }
    new_roles = 0
    new_permissions = 0

    for code, name in ROLE_NAMES.items():
        if code not in role_rows:
            role = Role(code=code, name=name)
            db.session.add(role)
            role_rows[code] = role
            new_roles += 1
    for code, description in PERMISSIONS.items():
        if code not in permission_rows:
            permission = Permission(code=code, description=description)
            db.session.add(permission)
            permission_rows[code] = permission
            new_permissions += 1
    db.session.flush()

    existing = {
        (link.role_id, link.permission_id)
        for link in db.session.execute(select(RolePermission)).scalars()
    }
    new_grants = 0
    for role_code, allowed in ROLE_PERMISSIONS.items():
        for permission_code in sorted(allowed):
            key = (role_rows[role_code].id, permission_rows[permission_code].id)
            if key not in existing:
                db.session.add(
                    RolePermission(role_id=key[0], permission_id=key[1])
                )
                existing.add(key)
                new_grants += 1

    if commit:
        db.session.commit()
    return CatalogueWrite(
        roles=new_roles, permissions=new_permissions, grants=new_grants
    )


def is_platform_owner(user) -> bool:
    """True when the user holds a platform grant (or the legacy owner flag)."""
    if user is None:
        return False
    if db.session.get(PlatformGrant, user.id) is not None:
        return True
    # Compatibility: platform owners that predate the grants table.
    return bool(getattr(user, "is_platform_owner", False))


def _membership_for(user_id: int, sacco_id: int) -> SaccoMembership | None:
    return db.session.execute(
        select(SaccoMembership).where(
            SaccoMembership.user_id == user_id,
            SaccoMembership.sacco_id == sacco_id,
        )
    ).scalar_one_or_none()


def _role_codes(membership: SaccoMembership) -> set[str]:
    codes = set(
        db.session.execute(
            select(Role.code)
            .join(MembershipRoleLink, MembershipRoleLink.role_id == Role.id)
            .where(MembershipRoleLink.membership_id == membership.id)
        ).scalars()
    )
    if codes:
        return codes
    legacy = LEGACY_ROLE_CODES.get(membership.role)
    return {legacy} if legacy else set()


def role_codes_by_membership(
    memberships: list[SaccoMembership],
) -> dict[int, set[str]]:
    """`_role_codes` for many memberships in one query (list endpoints)."""
    ids = [m.id for m in memberships]
    found: dict[int, set[str]] = {i: set() for i in ids}
    if ids:
        for membership_id, code in db.session.execute(
            select(MembershipRoleLink.membership_id, Role.code)
            .join(Role, MembershipRoleLink.role_id == Role.id)
            .where(MembershipRoleLink.membership_id.in_(ids))
        ):
            found[membership_id].add(code)
    for m in memberships:
        if not found[m.id]:
            legacy = LEGACY_ROLE_CODES.get(m.role)
            found[m.id] = {legacy} if legacy else set()
    return found


def is_admin_membership(membership: SaccoMembership) -> bool:
    """Is this membership the SACCO admin? (The role column says so; migration
    0010 and the role routes keep the sacco_admin title in step with it.)"""
    return membership.role is MembershipRole.SACCO_ADMIN


def is_admin_role(role: MembershipRole | None) -> bool:
    """Does a requested role value make someone the SACCO admin?"""
    return role is MembershipRole.SACCO_ADMIN


# What a platform owner holds in every SACCO without asking: running the
# platform (name and status, suspension, settings, SACCO admins). SACCO data
# (members, savings, loans, the audit log) needs the SACCO's consent: an
# approved support-access grant, read-only, for its scope (migration 0023).
OWNER_PLATFORM = frozenset(
    {"sacco.view", "sacco.manage", "sacco.update", "admin.manage", "dashboard.view"}
)
SUPPORT_SCOPE_PERMISSIONS = {
    "audit": frozenset({"audit.view"}),
    "members": frozenset({"member.view"}),
    "savings": frozenset({"savings.view", "savings.view_all"}),
    "loans": frozenset({"loan.view"}),
}


def active_support_grants(user_id: int, sacco_id: int) -> dict[str, int]:
    """{permission: grant id} this owner may read through, right now."""
    from app.models.support import SupportAccessRequest, SupportStatus

    rows = db.session.execute(
        select(SupportAccessRequest.id, SupportAccessRequest.scope).where(
            SupportAccessRequest.sacco_id == sacco_id,
            SupportAccessRequest.requested_by_user_id == user_id,
            SupportAccessRequest.status == SupportStatus.APPROVED,
            SupportAccessRequest.expires_at > func.now(),
        )
    ).all()
    granted: dict[str, int] = {}
    for grant_id, scope in rows:
        for permission in SUPPORT_SCOPE_PERMISSIONS[scope.value]:
            granted.setdefault(permission, grant_id)
    return granted


def _membership_permissions(user, sacco_id: int) -> frozenset[str]:
    membership = _membership_for(user.id, sacco_id)
    if membership is None or membership.status is not MembershipStatus.ACTIVE:
        return frozenset()
    held = frozenset(
        db.session.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .where(Role.code.in_(_role_codes(membership)))
        ).scalars()
    )
    if is_admin_membership(membership):
        # Agreed schema: a SACCO admin never approves loans and never reads the
        # audit log, whatever other titles the membership holds.
        held -= ADMIN_NEVER
    return held


def holders(sacco_id: int, permission: str) -> list[int]:
    """User ids of the active members of one SACCO who hold `permission`.

    Decided the way `_membership_permissions` decides, in three queries instead
    of three per member: title rows first, the legacy role column only for a
    membership with no title rows. Platform owners are not members, so they are
    never listed. Used to tell every officer about a change.
    """
    codes_with = set(
        db.session.execute(
            select(Role.code)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(Permission.code == permission)
        ).scalars()
    )
    members = db.session.execute(
        select(SaccoMembership.id, SaccoMembership.user_id, SaccoMembership.role).where(
            SaccoMembership.sacco_id == sacco_id,
            SaccoMembership.status == MembershipStatus.ACTIVE,
        )
    ).all()
    titles: dict[int, set[str]] = {}
    for membership_id, code in db.session.execute(
        select(MembershipRoleLink.membership_id, Role.code)
        .join(Role, Role.id == MembershipRoleLink.role_id)
        .where(MembershipRoleLink.membership_id.in_([m.id for m in members]))
    ):
        titles.setdefault(membership_id, set()).add(code)

    found = []
    for membership_id, user_id, role in members:
        codes = titles.get(membership_id)
        if not codes:
            legacy = LEGACY_ROLE_CODES.get(role)
            codes = {legacy} if legacy else set()
        if role is MembershipRole.SACCO_ADMIN and permission in ADMIN_NEVER:
            continue
        if codes & codes_with:
            found.append(user_id)
    return found


def holding_since(sacco_id: int, user_id: int, permission: str):
    """When this user began holding `permission` in this SACCO, or None.

    The earliest time a title that carries it was given. A membership with no
    title rows (the legacy fallback: the role column alone grants the power)
    counts from when the membership was created. The API writes a title row on
    every add, invite and role change, so a membership without rows has had the
    same role since it began: a legacy admin is not a new officer. It gets no
    shortcut either: the same comparison with a proposal applies, so a legacy
    membership created after a proposal still cannot approve it.
    """
    membership = _membership_for(user_id, sacco_id)
    if membership is None or membership.status is not MembershipStatus.ACTIVE:
        return None
    given = db.session.execute(
        select(func.min(MembershipRoleLink.granted_at))
        .join(RolePermission, RolePermission.role_id == MembershipRoleLink.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            MembershipRoleLink.membership_id == membership.id,
            Permission.code == permission,
        )
    ).scalar_one()
    if given is not None:
        return given
    if _titles_exist(membership.id):
        return None  # titles exist and none carries it
    legacy = LEGACY_ROLE_CODES.get(membership.role)
    carries = legacy is not None and permission in ROLE_PERMISSIONS.get(
        legacy, frozenset()
    )
    return membership.created_at if carries else None


def _titles_exist(membership_id: int) -> bool:
    return (
        db.session.execute(
            select(MembershipRoleLink.membership_id)
            .where(MembershipRoleLink.membership_id == membership_id)
            .limit(1)
        ).first()
        is not None
    )


def permissions_for(user, sacco) -> frozenset[str]:
    """Every permission the user holds in this SACCO, computed the same way.

    A member holds what their job titles give. A platform owner also holds the
    platform permissions, plus read access to the scopes of their own active
    support grants for this SACCO. Nobody holds anything in a SACCO they are
    not part of.
    """
    if user is None:
        return frozenset()
    sacco_id = sacco.id if isinstance(sacco, Sacco) else int(sacco)
    held = set(_membership_permissions(user, sacco_id))
    if is_platform_owner(user):
        held |= OWNER_PLATFORM
        held |= set(active_support_grants(user.id, sacco_id))
    return frozenset(held)


def can(user, sacco, permission: str, resource=None) -> bool:  # noqa: ARG001
    """May `user` do `permission` in `sacco`?

    False for: no user, an unknown permission name, no membership, an inactive
    membership, a role that does not hold the permission, or a SACCO that does
    not match the membership. A platform owner holds the platform permissions
    everywhere and SACCO data only through an active support grant (read-only).
    """
    if user is None or permission not in PERMISSIONS:
        # An unknown name is a typo or a new permission nobody granted: deny.
        return False
    sacco_id = sacco.id if isinstance(sacco, Sacco) else int(sacco)
    if permission in _membership_permissions(user, sacco_id):
        return True
    if not is_platform_owner(user):
        return False
    if permission in OWNER_PLATFORM:
        return True
    grant_id = active_support_grants(user.id, sacco_id).get(permission)
    if grant_id is None:
        return False
    # A read under consent: the request writes an audit row naming the grant.
    note_support_read(grant_id, sacco_id)
    return True


def note_support_read(grant_id: int, sacco_id: int) -> None:
    """Remember a grant this request read through (see app.support.audit)."""
    from flask import g, has_request_context

    if has_request_context():
        used = g.setdefault("support_reads", set())
        used.add((grant_id, sacco_id))
