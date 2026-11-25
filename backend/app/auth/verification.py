"""Single-use emailed links: prove an email address, or reset a password.

Only the token's SHA-256 is stored, so a database leak cannot be turned into
working links. Issuing a new link replaces any earlier one of the same purpose.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from app.auth.tokens import lock_user
from app.extensions import db
from app.models.email_verification import EmailVerification
from app.models.user import User

VERIFY, RESET = "verify", "reset"
COOLDOWN = 60  # seconds before the same address can be sent another link
LIFETIMES = {VERIFY: timedelta(hours=24), RESET: timedelta(hours=1)}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(user: User, purpose: str = VERIFY) -> str:
    """Add a fresh link row and return the raw token. The caller commits."""
    db.session.execute(
        delete(EmailVerification).where(
            EmailVerification.user_id == user.id,
            EmailVerification.purpose == purpose,
        )
    )
    token = secrets.token_urlsafe(32)
    db.session.add(
        EmailVerification(
            user_id=user.id,
            purpose=purpose,
            token_hash=_hash(token),
            expires_at=datetime.now(UTC) + LIFETIMES[purpose],
        )
    )
    return token


def drop_reset_links(user_id: int) -> None:
    """Kill pending reset links: any event that ends sessions must end these too."""
    db.session.execute(
        delete(EmailVerification).where(
            EmailVerification.user_id == user_id, EmailVerification.purpose == RESET
        )
    )


def recently_issued(user: User, purpose: str, seconds: int = COOLDOWN) -> bool:
    """True if a link of this purpose was issued to `user` in the last `seconds`."""
    newest = db.session.execute(
        select(func.max(EmailVerification.created_at)).where(
            EmailVerification.user_id == user.id, EmailVerification.purpose == purpose
        )
    ).scalar_one()
    cutoff = datetime.now(UTC) - timedelta(seconds=seconds)
    return newest is not None and newest > cutoff


def find(token: str, purpose: str) -> EmailVerification | None:
    """The live (unexpired) link row, locked until the caller commits.

    Lock order everywhere is the user row first, then the link row, so a link
    being spent can never deadlock with a request that holds the user lock.
    """
    owner = db.session.execute(
        select(EmailVerification.user_id).where(
            EmailVerification.token_hash == _hash(token),
            EmailVerification.purpose == purpose,
        )
    ).scalar_one_or_none()
    if owner is None:
        return None
    lock_user(owner)
    row = db.session.execute(
        select(EmailVerification)
        .where(
            EmailVerification.token_hash == _hash(token),
            EmailVerification.purpose == purpose,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None or row.expires_at <= datetime.now(UTC):
        db.session.rollback()
        return None
    return row


def confirm(token: str) -> bool:
    """Mark the owner's email verified and spend the link. Commits on success."""
    row = find(token, VERIFY)
    if row is None:
        return False
    db.session.get(User, row.user_id).email_verified_at = datetime.now(UTC)
    db.session.delete(row)
    db.session.commit()
    return True
