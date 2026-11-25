"""Platform owners: list, grant, revoke. Platform owners only.

Rules from the agreed schema (docs/roles-permissions.md, section 6):

* only an active platform owner grants or revokes platform ownership;
* nobody revokes themselves;
* at least two platform owners always remain.

Ownership is the `users.is_platform_owner` flag plus a `platform_grants` row;
both are written and cleared together, so `is_platform_owner()` (grant or flag)
and the tenant guard (flag) always agree. A revoked owner loses access on their
next request, because the flag is read from the database every time.
"""

from flask import g, jsonify
from sqlalchemy import func, or_, select, text

from app import admin_bp
from app.audit import log as audit_log
from app.auth.guards import platform_owner_required
from app.auth.limits import read_limit, write_limit
from app.auth.routes import _body
from app.errors import error_response
from app.extensions import db
from app.models.access import PlatformGrant
from app.models.user import User
from app.security.permissions import is_platform_owner

MIN_OWNERS = 2
# One lock for every grant and revoke, held to the end of the transaction. Row
# locks alone are not enough: the grant subquery would read a stale snapshot, so
# two revokes at once could each count three owners and leave one.
OWNERS_LOCK = "SELECT pg_advisory_xact_lock(hashtext('nexora.platform_owners'))"


def _serialise():
    """Take the owners lock, then check the caller again: their ownership may
    have been revoked while they waited. Returns an error response or None."""
    db.session.execute(text(OWNERS_LOCK))
    db.session.refresh(g.user)
    if not is_platform_owner(g.user):
        db.session.rollback()
        return error_response(403, "Platform owner access required.")
    return None


def _owners(lock: bool = False) -> list[User]:
    granted = select(PlatformGrant.user_id)
    stmt = (
        select(User)
        .where(or_(User.is_platform_owner.is_(True), User.id.in_(granted)))
        .order_by(User.id)
    )
    return list(db.session.execute(stmt.with_for_update() if lock else stmt).scalars())


def _json(user: User) -> dict:
    return {"user_id": user.id, "email": user.email}


@admin_bp.get("/owners")
@read_limit
@platform_owner_required
def list_owners():
    return jsonify(owners=[_json(user) for user in _owners()])


@admin_bp.post("/owners")
@write_limit
@platform_owner_required
def grant_owner():
    email = _body().get("email")
    if (
        not isinstance(email, str)
        or not email.strip()
        or len(email) > 255
        or "\x00" in email
    ):
        return error_response(400, "email is required.")
    if error := _serialise():
        return error
    user = db.session.execute(
        select(User).where(func.lower(User.email) == email.strip().lower())
    ).scalar_one_or_none()
    if user is None:
        db.session.rollback()
        return error_response(404, "No registered user with that email.")
    if user.email_verified_at is None:
        db.session.rollback()
        return error_response(409, "That user has not verified their email yet.")
    if user.is_platform_owner or db.session.get(PlatformGrant, user.id):
        db.session.rollback()
        return error_response(409, "That user is already a platform owner.")
    user.is_platform_owner = True
    db.session.add(PlatformGrant(user_id=user.id, granted_by_user_id=g.user.id))
    audit_log.record(None, g.user.id, "platform.owner_grant", f"user:{user.id}")
    body = _json(user)
    db.session.commit()
    return jsonify(body), 201


@admin_bp.delete("/owners/<int:user_id>")
@write_limit
@platform_owner_required
def revoke_owner(user_id: int):
    if user_id == g.user.id:
        return error_response(403, "You cannot revoke your own platform ownership.")
    if error := _serialise():
        return error
    owners = _owners(lock=True)  # read after the lock: the count below is current
    target = next((user for user in owners if user.id == user_id), None)
    if target is None:
        db.session.rollback()
        return error_response(404, "No platform owner with that id.")
    if len(owners) - 1 < MIN_OWNERS:
        db.session.rollback()
        return error_response(
            409, f"At least {MIN_OWNERS} platform owners must remain."
        )
    target.is_platform_owner = False
    grant = db.session.get(PlatformGrant, target.id)
    if grant is not None:
        db.session.delete(grant)
    audit_log.record(None, g.user.id, "platform.owner_revoke", f"user:{target.id}")
    db.session.commit()
    return jsonify(owners=[_json(user) for user in owners if user.id != target.id])
