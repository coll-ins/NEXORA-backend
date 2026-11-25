"""Account security: change a password, sign out everywhere.

The routes in routes.py parse the request and call these functions. Each one runs
in a single transaction and commits once, so the audit row can never be written
without the change it describes (or the other way round).
"""

from datetime import UTC, datetime

from sqlalchemy import select, update

from app.audit import log as audit_log
from app.auth import verification
from app.auth.lockout import check_lockout, record_failed_login, reset_failed_logins
from app.auth.passwords import hash_password, validate_password, verify_password
from app.auth.tokens import lock_user
from app.extensions import db
from app.models.refresh_token import RefreshToken
from app.models.user import User

# The same words a failed login uses, so the two cannot be told apart.
INVALID_CREDENTIALS = "Invalid email or password."
LOCKED_MESSAGE = "Account temporarily locked. Try again later."
SAME_PASSWORD = "New password must be different from the current one."


class AccountError(Exception):
    """A refused request. `status` and the message are safe to show the client."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def revoke_all_sessions(user_id: int) -> None:
    """Revoke every refresh token and kill every access token already issued.

    Pending reset links die with them, so an old link cannot outlive an email or
    password change. The caller holds the user row lock and commits.
    """
    verification.drop_reset_links(user_id)
    bump = User.token_version + 1
    db.session.execute(
        update(User).where(User.id == user_id).values(token_version=bump)
    )
    db.session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


def change_password(user_id: int, current: str, new: str) -> None:
    """Replace the caller's password. `current` and `new` are already length-checked.

    A wrong current password is a failed login attempt: it counts toward the same
    lockout, and a locked account is refused before the password is looked at.
    """
    # Locked for the whole request, so parallel guesses cannot dodge the counter.
    # populate_existing: the request guard already loaded this user, and without it
    # the lock would hand back the old password hash and failure counter.
    user = db.session.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if user is None:
        raise AccountError("Invalid or expired token.", 401)

    if check_lockout(user):
        db.session.rollback()
        raise AccountError(LOCKED_MESSAGE, 423)
    if not verify_password(current, user.password_hash):
        record_failed_login(user)
        db.session.commit()
        raise AccountError(INVALID_CREDENTIALS, 401)

    if problem := validate_password(new, user.email):
        db.session.rollback()
        raise AccountError(problem, 400)
    if new == current:
        db.session.rollback()
        raise AccountError(SAME_PASSWORD, 400)

    user.password_hash = hash_password(new)
    reset_failed_logins(user)
    revoke_all_sessions(user.id)
    audit_log.record(None, user.id, "auth.password_change", f"user:{user.id}")
    db.session.commit()


def logout_all(user_id: int) -> None:
    """Revoke every refresh token of the caller. Never refused for a suspension."""
    lock_user(user_id)
    revoke_all_sessions(user_id)
    audit_log.record(None, user_id, "auth.logout_all", f"user:{user_id}")
    db.session.commit()


def reset_password(token: str, new: str) -> None:
    """Set a new password from an emailed link. `new` is already length-checked.

    Owning the mailbox proves the address, so the user also becomes verified, and
    the lockout counter is cleared: this is how someone locked out by repeated
    guesses gets back in. Every session ends. The link is spent on success only.
    """
    row = verification.find(token, verification.RESET)
    if row is None:
        raise AccountError("This reset link is invalid or has expired.", 400)
    user = db.session.execute(
        select(User)
        .where(User.id == row.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if problem := validate_password(new, user.email):
        db.session.rollback()
        raise AccountError(problem, 400)

    user.password_hash = hash_password(new)
    reset_failed_logins(user)
    user.email_verified_at = user.email_verified_at or datetime.now(UTC)
    revoke_all_sessions(user.id)  # also deletes this link
    audit_log.record(None, user.id, "auth.password_reset", f"user:{user.id}")
    db.session.commit()
