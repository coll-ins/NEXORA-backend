"""Consent-based support access: request, approve, deny, revoke, list.

A platform owner asks to read one scope (audit, members, savings, loans) of one
SACCO, with a reason. An active admin of that SACCO approves (for 1 to 24 hours)
or denies; the admin can revoke an approval at any time. While a grant is
active `can()` lets that owner read the scope, never write. Every read under a
grant writes an audit row naming the grant. The database (migration 0023)
refuses anything else, whoever connects.
"""

from datetime import UTC, datetime, timedelta

from flask import g, jsonify, request
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from app import saccos_bp, support_bp
from app.auth.limits import read_limit, write_limit
from app.auth.routes import _body
from app.errors import error_response
from app.extensions import db
from app.models.audit import AuditLog
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.sacco import Sacco
from app.models.support import SupportAccessRequest, SupportScope, SupportStatus
from app.security.permissions import can, is_admin_membership, is_platform_owner
from app.security.tenant import AccessDenied, current_user

MAX_HOURS = 24
DEFAULT_HOURS = 4
MAX_REASON = 500


def _audit(sacco_id: int, actor_id: int, action: str, grant_id: int, note=""):
    db.session.add(
        AuditLog(
            sacco_id=sacco_id,
            actor_id=actor_id,
            action=action,
            target=f"grant:{grant_id} {note}".strip()[:255],
        )
    )


def _json(row: SupportAccessRequest) -> dict:
    def iso(value):
        return value.isoformat() if value else None

    return {
        "id": row.id,
        "sacco_id": row.sacco_id,
        "requested_by_user_id": row.requested_by_user_id,
        "reason": row.reason,
        "scope": row.scope.value,
        "status": row.status.value,
        "decided_by_membership_id": row.decided_by_membership_id,
        "decided_at": iso(row.decided_at),
        "expires_at": iso(row.expires_at),
        "revoked_by_membership_id": row.revoked_by_membership_id,
        "revoked_at": iso(row.revoked_at),
        "created_at": iso(row.created_at),
    }


def _expire_due(sacco_id: int) -> None:
    """Mark approvals whose time is up as expired (can() ignores them anyway)."""
    db.session.execute(
        update(SupportAccessRequest)
        .where(
            SupportAccessRequest.sacco_id == sacco_id,
            SupportAccessRequest.status == SupportStatus.APPROVED,
            SupportAccessRequest.expires_at <= datetime.now(UTC),
        )
        .values(status=SupportStatus.EXPIRED)
    )


def _signed_in():
    try:
        return current_user(), None
    except AccessDenied as exc:
        return None, error_response(exc.status, exc.message)


def _admin_membership(user, sacco_id: int) -> SaccoMembership | None:
    membership = db.session.execute(
        select(SaccoMembership).where(
            SaccoMembership.user_id == user.id, SaccoMembership.sacco_id == sacco_id
        )
    ).scalar_one_or_none()
    if (
        membership is not None
        and membership.status is MembershipStatus.ACTIVE
        and is_admin_membership(membership)
    ):
        return membership
    return None


@saccos_bp.post("/<int:sacco_id>/support-access")
@write_limit
def request_access(sacco_id):
    user, error = _signed_in()
    if error:
        return error
    if not is_platform_owner(user):
        return error_response(403, "Only a platform owner can request support access.")
    if db.session.get(Sacco, sacco_id) is None:
        return error_response(404, "Not found.")
    body = _body()
    reason, scope = body.get("reason"), body.get("scope")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > MAX_REASON
        or "\x00" in reason
    ):
        return error_response(
            400, f"reason is required (at most {MAX_REASON} characters)."
        )
    if scope not in {s.value for s in SupportScope}:
        return error_response(
            400, "scope must be one of: audit, members, savings, loans."
        )
    row = SupportAccessRequest(
        sacco_id=sacco_id,
        requested_by_user_id=user.id,
        reason=reason.strip(),
        scope=SupportScope(scope),
    )
    db.session.add(row)
    db.session.flush()
    _audit(sacco_id, user.id, "support.request", row.id, scope)
    db.session.commit()
    return jsonify(_json(row)), 201


@saccos_bp.get("/<int:sacco_id>/support-access")
@read_limit
def list_access(sacco_id):
    user, error = _signed_in()
    if error:
        return error
    if db.session.get(Sacco, sacco_id) is None:
        return error_response(404, "Not found.")
    if not is_platform_owner(user) and _admin_membership(user, sacco_id) is None:
        # Members of the SACCO learn nothing; outsiders get the usual 404.
        member = db.session.execute(
            select(SaccoMembership.id).where(
                SaccoMembership.user_id == user.id,
                SaccoMembership.sacco_id == sacco_id,
            )
        ).first()
        return error_response(
            403 if member else 404, "Not allowed." if member else "Not found."
        )
    _expire_due(sacco_id)
    db.session.commit()
    rows = db.session.execute(
        select(SupportAccessRequest)
        .where(SupportAccessRequest.sacco_id == sacco_id)
        .order_by(SupportAccessRequest.id.desc())
    ).scalars()
    return jsonify(requests=[_json(row) for row in rows])


def _decide(request_id: int, action: str):
    user, error = _signed_in()
    if error:
        return error
    row = db.session.execute(
        select(SupportAccessRequest)
        .where(SupportAccessRequest.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        return error_response(404, "Not found.")
    # Decided by the documented permission, so the screens that hide the
    # buttons and the API that enforces them read the same rule.
    if not can(user, row.sacco_id, "support.approve"):
        db.session.rollback()
        return error_response(
            403,
            "Only an active admin of this SACCO can decide or revoke support access.",
        )
    # The permission says who may decide; the row says who to name as the decider.
    admin = db.session.execute(
        select(SaccoMembership).where(
            SaccoMembership.user_id == user.id,
            SaccoMembership.sacco_id == row.sacco_id,
        )
    ).scalar_one_or_none()
    if admin is None:
        db.session.rollback()
        return error_response(
            403,
            "Only an active admin of this SACCO can decide or revoke support access.",
        )
    if admin.user_id == row.requested_by_user_id:
        db.session.rollback()
        return error_response(403, "The requester cannot decide their own request.")
    _expire_due(row.sacco_id)
    db.session.refresh(row)
    if action in ("approve", "deny") and row.status is not SupportStatus.REQUESTED:
        db.session.rollback()
        return error_response(409, f"This request is already {row.status.value}.")
    if action == "revoke" and row.status is not SupportStatus.APPROVED:
        db.session.rollback()
        return error_response(
            409, f"Only an approved grant can be revoked (it is {row.status.value})."
        )
    if action == "approve":
        hours = _body().get("hours", DEFAULT_HOURS)
        if (
            isinstance(hours, bool)
            or not isinstance(hours, int)
            or not 1 <= hours <= MAX_HOURS
        ):
            db.session.rollback()
            return error_response(
                400, f"hours must be a whole number from 1 to {MAX_HOURS}."
            )
        row.status = SupportStatus.APPROVED
        row.decided_by_membership_id = admin.id
        # The database's clock: the trigger measures 24 hours from its now().
        row.expires_at = func.now() + timedelta(hours=hours)
    elif action == "deny":
        row.status = SupportStatus.DENIED
        row.decided_by_membership_id = admin.id
    else:
        row.status = SupportStatus.REVOKED
        row.revoked_by_membership_id = admin.id
    try:
        db.session.flush()
    except DBAPIError as exc:  # the database trigger refused it
        db.session.rollback()
        return error_response(409, str(exc.orig).split("\n")[0][:200])
    _audit(row.sacco_id, user.id, f"support.{action}", row.id, row.scope.value)
    db.session.commit()
    return jsonify(_json(row))


@support_bp.post("/<int:request_id>/approve")
@write_limit
def approve(request_id):
    return _decide(request_id, "approve")


@support_bp.post("/<int:request_id>/deny")
@write_limit
def deny(request_id):
    return _decide(request_id, "deny")


@support_bp.post("/<int:request_id>/revoke")
@write_limit
def revoke(request_id):
    return _decide(request_id, "revoke")


@support_bp.after_app_request
def _audit_support_reads(response):
    """One audit row per grant a request read through, with the grant id."""
    used = g.pop("support_reads", None)
    if not used:
        return response
    try:
        user = current_user()
        db.session.rollback()  # never carry the view's open transaction
        for grant_id, sacco_id in sorted(used):
            _audit(
                sacco_id,
                user.id,
                "support.read",
                grant_id,
                f"{request.method} {request.path} -> {response.status_code}",
            )
        db.session.commit()
    except Exception:  # the response is already decided; log, do not break it
        db.session.rollback()
        from flask import current_app

        current_app.logger.exception("Could not write the support-read audit row")
    return response
