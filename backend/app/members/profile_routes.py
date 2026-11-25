"""A member's own record and their consent to keep personal data.

GET   /saccos/<id>/members/me/data-consent           the wording, and my consent
POST  /saccos/<id>/members/me/data-consent           I agree (send the version I read)
POST  /saccos/<id>/members/me/data-consent/withdraw  I withdraw: my details are erased
GET   /saccos/<id>/members/<mid>/profile             the member, or `member.manage`
PATCH /saccos/<id>/members/<mid>/profile             same; managers set ID, capital

Personal details need the member's own active consent (only they can give it).
Audit rows name the fields, never the values.
"""

from flask import g, jsonify
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import saccos_bp
from app.audit import log as audit_log
from app.auth.limits import read_limit, write_limit
from app.auth.routes import _body
from app.errors import CONSENT_REQUIRED, error_response, interface_error
from app.extensions import db
from app.members import profile
from app.models.consent import MemberDataConsent
from app.models.membership import MembershipStatus, SaccoMembership
from app.security.permissions import can
from app.security.tenant import AccessDenied, current_user, permission_scoped

NEEDS_CONSENT = (
    "This member has not agreed to NEXORA keeping their personal details. They "
    "can agree on their own profile first."
)


def _my_membership():
    membership = g.access.membership
    if membership is None or membership.status is not MembershipStatus.ACTIVE:
        return None
    return membership


def _locked(sacco_id: int, membership_id: int) -> SaccoMembership | None:
    return db.session.execute(
        select(SaccoMembership)
        .where(
            SaccoMembership.id == membership_id,
            SaccoMembership.sacco_id == sacco_id,
        )
        .with_for_update()
        # Refresh a row loaded before the wait: it is stale once the lock is ours.
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


@saccos_bp.get("/<int:sacco_id>/members/me/data-consent")
@read_limit
@permission_scoped("sacco.view")
def get_my_consent(sacco_id):
    me = _my_membership()
    if me is None:
        return error_response(403, "Only members of this SACCO give consent.")
    return jsonify(
        notice=profile.NOTICE,
        consent=profile.consent_json(profile.active_consent(me.id)),
    )


@saccos_bp.post("/<int:sacco_id>/members/me/data-consent")
@write_limit
@permission_scoped("sacco.view")
def give_consent(sacco_id):
    me = _my_membership()
    if me is None:
        return error_response(403, "Only members of this SACCO give consent.")
    version = _body().get("notice_version")
    if type(version) is not int:
        return error_response(
            400, "Send notice_version: the number of the wording you read."
        )
    if version != profile.NOTICE["version"]:
        return error_response(
            409,
            "The wording has changed. Read it again, then agree to the new version.",
        )
    locked = _locked(sacco_id, me.id)  # one request at a time per member
    existing = profile.active_consent(locked.id)
    if existing is not None:
        body = profile.consent_json(existing)
        db.session.rollback()
        return jsonify(consent=body)
    row = MemberDataConsent(
        membership_id=locked.id, purpose=profile.PURPOSE, notice_version=version
    )
    db.session.add(row)
    db.session.flush()
    audit_log.record(
        sacco_id,
        g.access.user.id,
        "consent.grant",
        f"membership:{locked.id} v{version}",
    )
    body = profile.consent_json(row)
    db.session.commit()
    return jsonify(consent=body), 201


@saccos_bp.post("/<int:sacco_id>/members/me/data-consent/withdraw")
@write_limit
def withdraw_consent(sacco_id):
    # Not permission_scoped: erasure must work after the member was deactivated
    # or the SACCO was suspended, so it needs only their own membership.
    try:
        user = current_user()
    except AccessDenied as exc:
        return error_response(exc.status, exc.message)
    mine = db.session.execute(
        select(SaccoMembership.id).where(
            SaccoMembership.user_id == user.id, SaccoMembership.sacco_id == sacco_id
        )
    ).scalar_one_or_none()
    if mine is None:
        return error_response(404, "Not found.")
    locked = _locked(sacco_id, mine)
    consent = profile.active_consent(locked.id)
    if consent is None:
        db.session.rollback()
        return error_response(
            409, "You have not given consent, so there is nothing to withdraw."
        )
    erased = profile.withdraw(locked, consent)
    audit_log.record(
        sacco_id,
        user.id,
        "consent.withdraw",
        f"membership:{locked.id} erased:{','.join(erased) or 'none'}",
    )
    db.session.commit()
    return jsonify(consent=None, erased=erased)


def _allowed(sacco_id: int, membership_id: int):
    """(membership, manager) for the caller, or (None, error response)."""
    manager = can(g.access.user, g.access.sacco, "member.manage")
    me = g.access.membership
    is_self = me is not None and me.id == membership_id
    if not (manager or is_self):
        return None, error_response(403, "You are not allowed to do this.")
    return manager, None


@saccos_bp.get("/<int:sacco_id>/members/<int:membership_id>/profile")
@read_limit
@permission_scoped("sacco.view")
def get_profile(sacco_id, membership_id):
    _, denied = _allowed(sacco_id, membership_id)
    if denied:
        return denied
    membership = db.session.execute(
        select(SaccoMembership).where(
            SaccoMembership.id == membership_id, SaccoMembership.sacco_id == sacco_id
        )
    ).scalar_one_or_none()
    if membership is None:
        return error_response(404, "Member not found.")
    return jsonify(profile.profile_json(membership))


@saccos_bp.patch("/<int:sacco_id>/members/<int:membership_id>/profile")
@write_limit
@permission_scoped("sacco.view")
def update_profile(sacco_id, membership_id):
    manager, denied = _allowed(sacco_id, membership_id)
    if denied:
        return denied
    values, errors = profile.validate(_body())
    if errors or not values:
        return (
            jsonify(
                error={
                    "code": "validation_error",
                    "message": "Send at least one valid field. See fields.",
                    "fields": errors or {"_": "Send at least one field."},
                }
            ),
            400,
        )
    if not manager and {"share_capital", "national_id"} & values.keys():
        # A member editing their own national ID could probe which numbers other
        # members hold (409 against 200), so an officer records it, and share
        # capital.
        return error_response(
            403,
            "Only an officer who manages members records national IDs and "
            "share capital.",
        )

    membership = _locked(sacco_id, membership_id)
    if membership is None:
        db.session.rollback()
        return error_response(404, "Member not found.")
    giving = [f for f in profile.PERSONAL if values.get(f) is not None]
    if giving and profile.active_consent(membership.id) is None:
        db.session.rollback()
        return interface_error(CONSENT_REQUIRED, NEEDS_CONSENT)

    changed = [f for f, v in values.items() if getattr(membership, f) != v]
    if not changed:
        body = profile.profile_json(membership)
        db.session.rollback()
        return jsonify(body)
    for field in changed:
        setattr(membership, field, values[field])
    audit_log.record(
        sacco_id,
        g.access.user.id,
        "member.profile_update",
        f"membership:{membership.id} fields:{','.join(changed)}",
    )
    try:
        db.session.flush()
    except IntegrityError:  # unique (sacco, national_id)
        db.session.rollback()
        return error_response(
            409,
            "This national ID cannot be used. Ask a SACCO admin if this is a mistake.",
        )
    body = profile.profile_json(membership)
    db.session.commit()
    return jsonify(body)
