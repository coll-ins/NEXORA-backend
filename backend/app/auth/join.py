"""Join codes: issue, check and burn. Section 2.

A code is six digits, valid for the SACCO's TTL (15 minutes by default) and usable
once. Five wrong entries burn it. Only an HMAC digest is stored, never the code.
"""

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.errors import CODE_EXPIRED, CODE_USED, CODE_WRONG
from app.extensions import db
from app.models.identity import JoinCode

CODE_DIGITS = 6
MAX_ATTEMPTS = 5
DEFAULT_TTL = timedelta(minutes=15)


class JoinError(Exception):
    """`name` is one of the published codes in app.errors.INTERFACE_CODES."""

    def __init__(self, name: str, message: str):
        super().__init__(message)
        self.name = name
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_phone(value) -> str:
    phone = re.sub(r"[\s\-()]", "", str(value or ""))
    if not re.fullmatch(r"\+?[0-9]{9,15}", phone):
        raise ValueError("Enter a valid phone number.")
    return phone


def generate_code() -> str:
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def digest_code(membership_id: int, code: str, secret: str) -> str:
    """Keyed hash, tied to the membership so one code means nothing elsewhere."""
    message = f"{membership_id}:{str(code)[:32]}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def burn_codes(membership_id: int) -> None:
    """Spend every unused code of a membership (counted as out of attempts).
    The caller commits."""
    db.session.execute(
        update(JoinCode)
        .where(JoinCode.membership_id == membership_id, JoinCode.used_at.is_(None))
        .values(attempts=MAX_ATTEMPTS)
    )


def issue_code(
    membership_id: int,
    created_by_user_id: int | None,
    secret: str,
    ttl: timedelta = DEFAULT_TTL,
    now: datetime | None = None,
) -> tuple[JoinCode, str]:
    """Create a code for an invited membership. The caller commits.

    Any earlier live code for the same membership is burned, so reissuing never
    gives an attacker a fresh set of five guesses.
    """
    now = now or _now()
    burn_codes(membership_id)
    code = generate_code()
    row = JoinCode(
        membership_id=membership_id,
        code_digest=digest_code(membership_id, code, secret),
        expires_at=now + ttl,
        created_by_user_id=created_by_user_id,
    )
    db.session.add(row)
    return row, code  # show `code` once on the officer's screen, never store it


def check_code(row, code: str, secret: str, now: datetime | None = None) -> None:
    """Spend the code or raise JoinError. Mutates `row`; the caller commits.

    The caller must commit even when this raises, or the wrong-attempt count is lost.
    """
    now = now or _now()
    if row.used_at is not None or row.attempts >= MAX_ATTEMPTS:
        raise JoinError(CODE_USED, "This code has already been used.")
    if row.expires_at <= now:
        raise JoinError(CODE_EXPIRED, "This code has expired.")
    expected = digest_code(row.membership_id, code, secret)
    if not hmac.compare_digest(row.code_digest, expected):
        row.attempts += 1
        raise JoinError(CODE_WRONG, "That code is not correct.")
    row.used_at = now
