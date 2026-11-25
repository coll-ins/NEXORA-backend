"""Auth endpoints: register, login, refresh, logout, change-password, logout-all."""

import json
import re

from flask import current_app, g, jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app import auth_bp
from app.audit import log as audit_log
from app.auth import account, verification
from app.auth.guards import access_token_required
from app.auth.limits import login_limit, register_limit, write_limit
from app.auth.lockout import check_lockout, record_failed_login, reset_failed_logins
from app.auth.passwords import hash_password, validate_password, verify_password
from app.auth.tokens import (
    TokenError,
    create_access_token,
    create_refresh_token,
    revoke_refresh_token,
    rotate_refresh_token,
)
from app.errors import error_response
from app.extensions import db
from app.mail import send_safely
from app.models.user import User

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_EMAIL_LENGTH = 255
MAX_PASSWORD_LENGTH = 128

# Verified against when there is no usable hash, so every failure costs the same.
_DUMMY_HASH = hash_password("not-a-real-password")


def _error(message: str, status: int):
    return error_response(status, message)


def _body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _text(value) -> str | None:
    """The string itself, or None if it is not text Postgres and Argon2 accept."""
    if not isinstance(value, str) or "\x00" in value:
        return None
    try:
        value.encode()
    except UnicodeEncodeError:
        return None
    return value


def _email(value) -> str | None:
    email = (_text(value) or "").strip().lower()
    ok = len(email) <= MAX_EMAIL_LENGTH and EMAIL_RE.fullmatch(email)
    return email if ok else None


def _password(value) -> str | None:
    password = _text(value)
    return password if password and len(password) <= MAX_PASSWORD_LENGTH else None


@auth_bp.after_request
def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def _tokens(user_id: int, refresh_token: str):
    return jsonify(
        access_token=create_access_token(
            user_id, db.session.get(User, user_id).token_version
        ),
        refresh_token=refresh_token,
        token_type="Bearer",
    )


@auth_bp.post("/register")
@register_limit
def register():
    data = _body()
    email, password = _email(data.get("email")), _password(data.get("password"))
    if email is None:
        return error_response(400, "A valid email is required.")
    if password is None:
        return error_response(
            400, f"Password is required (max {MAX_PASSWORD_LENGTH} characters)."
        )
    if problem := validate_password(password, email):
        return error_response(400, problem)

    user = User(email=email, password_hash=hash_password(password))
    db.session.add(user)
    try:
        db.session.flush()
        token = verification.issue(user)
        db.session.commit()
    except IntegrityError:  # unique index on lower(email)
        db.session.rollback()
        return error_response(409, "Email is already registered.")
    send_safely(user.email, token)
    return jsonify(id=user.id, email=user.email), 201


@auth_bp.post("/verify-email")
@login_limit
def verify_email():
    token = _text(_body().get("token"))
    if not token or len(token) > 100 or not verification.confirm(token):
        return error_response(400, "This verification link is invalid or has expired.")
    return jsonify(message="Email verified.")


@auth_bp.post("/forgot-password")
@login_limit
def forgot_password():
    """Always the same answer, so it cannot be used to find registered emails."""
    email = _email(_body().get("email"))
    user = (
        db.session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if email
        else None
    )
    if user and not verification.recently_issued(user, verification.RESET):
        token = verification.issue(user, verification.RESET)
        target = f"user:{user.id}"
        audit_log.record(None, user.id, "auth.password_reset_requested", target)
        db.session.commit()
        send_safely(user.email, token, "reset")
    return jsonify(message="If that address has an account, a reset link was sent.")


@auth_bp.post("/reset-password")
@login_limit
def reset_password():
    data = _body()
    token, new = _text(data.get("token")), _password(data.get("new_password"))
    if not token or len(token) > 100:
        return error_response(400, "This reset link is invalid or has expired.")
    if new is None:
        return error_response(
            400, f"new_password is required (max {MAX_PASSWORD_LENGTH} characters)."
        )
    try:
        account.reset_password(token, new)
    except account.AccountError as exc:
        return error_response(exc.status, exc.message)
    return jsonify(message="Password reset. Sign in with the new password.")


@auth_bp.post("/resend-verification")
@login_limit
def resend_verification():
    """Always the same answer, so it cannot be used to find registered emails."""
    email = _email(_body().get("email"))
    user = (
        db.session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if email
        else None
    )
    if (
        user is not None
        and user.email_verified_at is None
        and not verification.recently_issued(user, verification.VERIFY)
    ):
        token = verification.issue(user)
        db.session.commit()
        send_safely(user.email, token)
    return jsonify(message="If that address needs verifying, a new link was sent.")


@auth_bp.post("/login")
@login_limit
def login():
    data = _body()
    email, password = _email(data.get("email")), _password(data.get("password"))
    if email is None or password is None:
        return error_response(401, "Invalid email or password.")

    # One attempt per account at a time: a parallel one is turned away at once, so
    # guesses cannot dodge the failure counter or pile up holding connections.
    try:
        user = (
            db.session.query(User)
            .filter(User.email == email)
            .with_for_update(nowait=True)
            .first()
        )
    except OperationalError as exc:
        db.session.rollback()
        if exc.orig.sqlstate != "55P03":  # lock_not_available
            raise
        return error_response(429, "Another attempt is in progress. Try again shortly.")
    # Lockout comes before the password, so a locked account never reveals
    # whether the guess was right.
    locked = user is not None and check_lockout(user)
    usable = user is not None and not locked
    valid = verify_password(password, user.password_hash if usable else _DUMMY_HASH)

    if locked:
        db.session.rollback()
        return error_response(423, "Account temporarily locked. Try again later.")
    if not (usable and valid):
        if user:
            record_failed_login(user)
        db.session.commit()
        return error_response(401, "Invalid email or password.")

    reset_failed_logins(user)
    # A suspended SACCO does not stop login: the user still reaches their other
    # SACCOs and sees the suspension in GET /members/me/saccos. The suspended
    # SACCO's own endpoints answer 423.
    refresh_token = create_refresh_token(user.id)
    db.session.commit()
    # Who just signed in, so a client (or Thunder Client) shows more than tokens.
    # The same shape as GET /members/me, plus the platform-owner flag.
    from app.members.self_service import get_profile  # imports this module

    profile = get_profile(user.id)
    # Who signed in first, the tokens last: this is read by people in a REST client.
    who = {
        "id": profile["id"],
        "email": profile["email"],
        "is_platform_owner": user.is_platform_owner,
        "memberships": profile["memberships"],
        "created_at": profile["created_at"],
    }
    tokens = _tokens(user.id, refresh_token).get_json()
    body = {
        "user": who,
        "token_type": tokens["token_type"],
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
    }
    # json.dumps keeps this order; jsonify would sort the keys alphabetically.
    return current_app.response_class(json.dumps(body), mimetype="application/json")


def _with_refresh_token(action):
    token = _body().get("refresh_token")
    if not isinstance(token, str) or not token:
        return error_response(400, "refresh_token is required.")
    try:
        return action(token)
    except TokenError:
        return error_response(401, "Invalid or expired refresh token.")


@auth_bp.post("/refresh")
@write_limit
def refresh():
    def rotate(token):
        user_id, new_token = rotate_refresh_token(token)
        return _tokens(user_id, new_token)

    return _with_refresh_token(rotate)


@auth_bp.post("/logout")
@write_limit
def logout():
    def revoke(token):
        revoke_refresh_token(token)
        return jsonify(message="Logged out.")

    return _with_refresh_token(revoke)


@auth_bp.post("/change-password")
@login_limit
@access_token_required
def change_password():
    data = _body()
    current, new = _password(data.get("current_password")), _password(
        data.get("new_password")
    )
    if current is None:
        return error_response(
            400, f"current_password is required (max {MAX_PASSWORD_LENGTH} characters)."
        )
    if new is None:
        return error_response(
            400, f"new_password is required (max {MAX_PASSWORD_LENGTH} characters)."
        )
    try:
        account.change_password(g.user.id, current, new)
    except account.AccountError as exc:
        return error_response(exc.status, exc.message)
    return jsonify(message="Password changed. Sign in again.")


@auth_bp.post("/logout-all")
@write_limit
@access_token_required
def logout_all():
    account.logout_all(g.user.id)
    return jsonify(message="Logged out everywhere.")

from app.auth import join_routes  # noqa: E402,F401
