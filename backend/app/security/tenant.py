"""Tenant guard: who may touch a SACCO, decided in one place.

Order of checks, so nothing leaks whether a SACCO exists:

1. No valid access token                          -> 401
2. SACCO missing, or the caller is not a member   -> 404 (the same answer for both)
3. Member, but inactive or the wrong role          -> 403
4. SACCO suspended (platform owner exempt)         -> 423

The platform owner is read from the database on every request, never from the
token, and may open any SACCO. Everyone else needs an active membership in that
exact SACCO. A SACCO id in the URL is never trusted on its own.
"""

from dataclasses import dataclass
from functools import wraps

from flask import g, request
from sqlalchemy import select

from app.auth.suspension import SUSPENDED_MESSAGE
from app.auth.tokens import TokenError, decode_access_token
from app.errors import error_response
from app.extensions import db
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.sacco import Sacco, SaccoStatus
from app.models.user import User


class AccessDenied(Exception):
    """A refused request. `status` and the message are safe to show the client."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Access:
    user: User
    sacco: Sacco
    membership: SaccoMembership | None  # None for the platform owner


def current_user() -> User:
    """The signed-in user from the Bearer access token, or AccessDenied(401)."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AccessDenied("Authentication required.", 401)
    try:
        payload = decode_access_token(token)
    except TokenError:
        raise AccessDenied("Invalid or expired token.", 401) from None
    user = db.session.get(User, int(payload["sub"]))
    # A token issued before the last password change or logout-all is dead.
    if user is None or payload.get("tv") != user.token_version:
        raise AccessDenied("Invalid or expired token.", 401)
    return user


def sacco_access(user: User, sacco_id: int) -> Access:
    """Open `sacco_id` for `user`, or raise AccessDenied.

    Answers only "may this person act in this SACCO at all" (an active
    membership, or a platform owner). What they may do there is `can()`'s job.
    """
    # The URL converter (app/__init__.py) only lets through ids Postgres can hold.
    sacco = db.session.get(Sacco, sacco_id)
    from app.security.permissions import is_platform_owner

    if is_platform_owner(user):
        if sacco is None:
            raise AccessDenied("Not found.", 404)
        return Access(user, sacco, None)

    membership = None
    if sacco is not None:
        membership = db.session.execute(
            select(SaccoMembership).where(
                SaccoMembership.user_id == user.id,
                SaccoMembership.sacco_id == sacco_id,
            )
        ).scalar_one_or_none()
    if membership is None:
        raise AccessDenied("Not found.", 404)
    if membership.status is not MembershipStatus.ACTIVE:
        raise AccessDenied("You are not allowed to do this.", 403)
    if sacco.status is SaccoStatus.SUSPENDED:
        # Also stops access tokens that were issued before the suspension.
        raise AccessDenied(SUSPENDED_MESSAGE, 423)
    return Access(user, sacco, membership)


def permission_scoped(*permissions: str):
    """Guard a `<int:sacco_id>` route by permission, decided by `can()`: the
    caller needs at least one of `permissions` (the view may ask for more).

    401 no token, 404 for a SACCO the caller is not in, 403 inactive or without
    the permission, 423 suspended (platform owner exempt). On success the view
    runs with `g.access` set.
    """
    from app.security.permissions import can

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            try:
                user = current_user()
                access = sacco_access(user, kwargs["sacco_id"])
            except AccessDenied as exc:
                return error_response(exc.status, exc.message)
            if not any(can(user, access.sacco, p) for p in permissions):
                return error_response(403, "You are not allowed to do this.")
            g.access = access
            return view(*args, **kwargs)

        return wrapper

    return decorator
