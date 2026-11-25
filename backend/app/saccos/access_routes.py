"""Job titles and SACCO settings.

Job titles: GET/PUT /saccos/<id>/members/<membership_id>/roles. The SACCO admin
(or the platform owner) sets which job titles a membership holds, for example
loans_officer or finance_officer. Only the platform owner gives or takes
sacco_admin, nobody changes their own titles, and a title can only be held by
an active membership.

Settings: GET/PATCH /saccos/<id>/settings. Every active member may read the loan
rules their SACCO runs by; only the platform owner changes them.
"""

from flask import g, jsonify
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import saccos_bp
from app.auth.guards import platform_owner_required
from app.auth.limits import read_limit, write_limit
from app.auth.routes import _body
from app.errors import error_response
from app.extensions import db
from app.models.access import MembershipRoleLink, Role
from app.models.audit import AuditLog
from app.models.loan import SaccoSettings
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.sacco import Sacco
from app.saccos import payment_details as payin
from app.security.permissions import (
    ROLE_NAMES,
    _role_codes,
    can,
    is_admin_membership,
)
from app.security.tenant import permission_scoped

OWNER_ONLY_TITLE = "sacco_admin"
ADMIN_TITLES = {OWNER_ONLY_TITLE, "member"}
SETTING_FIELDS = (
    "loan_cap",
    "savings_multiple",
    "join_code_ttl_minutes",
    "required_approvals",
)
# (lowest, highest) per field. Two approvals plus a third officer to disburse is
# the most a small SACCO can staff.
LIMITS = {
    "savings_multiple": (0, 1000),  # an int32 column; no SACCO lends 1000x savings
    "join_code_ttl_minutes": (1, 24 * 60),
    "required_approvals": (1, 2),
}
MAX_SETTING = 10**15  # fits a bigint with room; no SACCO lends more than this


def _audit(sacco_id: int, actor_id: int, action: str, target: str):
    db.session.add(
        AuditLog(
            sacco_id=sacco_id,
            actor_id=actor_id,
            action=action,
            target=target[:255],
        )
    )


def _titles(membership_id: int) -> list[str]:
    return sorted(
        db.session.execute(
            select(Role.code)
            .join(MembershipRoleLink, MembershipRoleLink.role_id == Role.id)
            .where(MembershipRoleLink.membership_id == membership_id)
        ).scalars()
    )


def _membership(
    sacco_id: int, membership_id: int, lock: bool = False
) -> SaccoMembership | None:
    stmt = select(SaccoMembership).where(
        SaccoMembership.id == membership_id,
        SaccoMembership.sacco_id == sacco_id,
    )
    return db.session.execute(
        stmt.with_for_update() if lock else stmt
    ).scalar_one_or_none()


@saccos_bp.get("/<int:sacco_id>/members/<int:membership_id>/roles")
@read_limit
@permission_scoped("member.manage", "member.view")
def get_roles(sacco_id, membership_id):
    membership = _membership(sacco_id, membership_id)
    if membership is None:
        return error_response(404, "Member not found.")
    # A membership with no title rows yet holds what its legacy role implies.
    roles = _titles(membership.id) or sorted(_role_codes(membership))
    return jsonify(membership_id=membership.id, roles=roles)


@saccos_bp.put("/<int:sacco_id>/members/<int:membership_id>/roles")
@write_limit
@permission_scoped("member.manage", "admin.manage")
def set_roles(sacco_id, membership_id):
    raw = _body().get("roles")
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(code, str) for code in raw)
    ):
        return error_response(400, "roles must be a non-empty list of role codes.")
    wanted = set(raw)
    unknown = wanted - set(ROLE_NAMES)
    if unknown:
        return error_response(400, f"Unknown role: {sorted(unknown)[0]}.")

    membership = _membership(sacco_id, membership_id, lock=True)
    if membership is None:
        return error_response(404, "Member not found.")
    owner = can(g.access.user, sacco_id, "admin.manage")
    if membership.user_id == g.access.user.id:
        db.session.rollback()
        return error_response(403, "You cannot change your own roles.")
    if membership.status is not MembershipStatus.ACTIVE:
        db.session.rollback()
        return error_response(409, "Roles can only be given to an active member.")

    current = set(_titles(membership.id))
    is_admin = is_admin_membership(membership)
    touches_admin = is_admin or OWNER_ONLY_TITLE in (current | wanted)
    if not touches_admin and not can(g.access.user, sacco_id, "member.manage"):
        db.session.rollback()
        return error_response(
            403, "Setting job titles needs member.manage (the SACCO admin)."
        )
    if touches_admin and not owner:
        db.session.rollback()
        return error_response(
            403, "Only the platform owner can change SACCO administrators."
        )
    if is_admin and wanted - ADMIN_TITLES:
        db.session.rollback()
        return error_response(
            409,
            "A SACCO admin holds no other job title: an admin never approves loans "
            "or reads the audit log.",
        )
    # The admin routes check the membership's role, `can()` checks these titles.
    # They must agree, so sacco_admin moves only with the role (PATCH /members).
    if (OWNER_ONLY_TITLE in wanted) != is_admin:
        db.session.rollback()
        return error_response(
            409,
            "sacco_admin follows the SACCO admin role. Make or remove an admin "
            "with PATCH /saccos/<id>/members/<membership_id> instead.",
        )

    officers_before = payin.officer_ids(sacco_id)
    if wanted != current:
        role_ids = dict(
            db.session.execute(
                select(Role.code, Role.id).where(Role.code.in_(wanted | current))
            ).all()
        )
        # Only titles that actually change are touched: a kept title keeps its
        # granted_at, which decides who may approve a waiting pay-in change.
        if current - wanted:
            db.session.execute(
                delete(MembershipRoleLink).where(
                    MembershipRoleLink.membership_id == membership.id,
                    MembershipRoleLink.role_id.in_(
                        [role_ids[c] for c in current - wanted]
                    ),
                )
            )
        for code in sorted(wanted - current):
            db.session.add(
                MembershipRoleLink(
                    membership_id=membership.id,
                    role_id=role_ids[code],
                    granted_by_user_id=g.access.user.id,
                )
            )
        _audit(
            sacco_id,
            g.access.user.id,
            "member.roles",
            f"membership:{membership.id} {','.join(sorted(current))}"
            f" -> {','.join(sorted(wanted))}",
        )
    db.session.commit()
    payin.announce_new_officers(sacco_id, officers_before, g.access.user.email)
    return jsonify(membership_id=membership.id, roles=sorted(wanted))


def _default_values(sacco_id: int) -> dict:
    """The one place the settings defaults live (the loan service reads the same
    numbers when a SACCO has no row)."""
    return {
        "sacco_id": sacco_id,
        "loan_cap": 0,
        "savings_multiple": 3,
        "join_code_ttl_minutes": 15,
        "required_approvals": 2,
    }


def _defaults(sacco_id: int) -> SaccoSettings:
    return SaccoSettings(**_default_values(sacco_id))


def _settings_json(sacco_id: int, row: SaccoSettings | None) -> dict:
    row = row or _defaults(sacco_id)
    return {
        "sacco_id": sacco_id,
        "loan_cap": row.loan_cap,
        "savings_multiple": row.savings_multiple,
        "join_code_ttl_minutes": row.join_code_ttl_minutes,
        "required_approvals": row.required_approvals,
    }


@saccos_bp.get("/<int:sacco_id>/settings")
@read_limit
@permission_scoped("sacco.view")
def get_settings(sacco_id):
    return jsonify(_settings_json(sacco_id, db.session.get(SaccoSettings, sacco_id)))


@saccos_bp.patch("/<int:sacco_id>/settings")
@write_limit
@platform_owner_required
def update_settings(sacco_id):
    body = _body()
    changes = {}
    for field in SETTING_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if isinstance(value, bool) or not isinstance(value, int):
            return error_response(400, f"{field} must be a whole number.")
        low, high = LIMITS.get(field, (0, MAX_SETTING))
        if not low <= value <= high:
            return error_response(400, f"{field} must be between {low} and {high}.")
        changes[field] = value
    if not changes:
        return error_response(
            400, f"Send at least one of: {', '.join(SETTING_FIELDS)}."
        )
    if db.session.get(Sacco, sacco_id) is None:
        return error_response(404, "Not found.")

    # Create the row if missing without racing a concurrent first write.
    db.session.execute(
        pg_insert(SaccoSettings)
        .values(_default_values(sacco_id))
        .on_conflict_do_nothing(index_elements=["sacco_id"])
    )
    row = db.session.get(
        SaccoSettings, sacco_id, with_for_update=True, populate_existing=True
    )
    for field, value in changes.items():
        setattr(row, field, value)
    _audit(
        sacco_id,
        g.user.id,
        "sacco.settings",
        "settings " + " ".join(f"{k}={v}" for k, v in sorted(changes.items())),
    )
    db.session.commit()
    return jsonify(_settings_json(sacco_id, row))
