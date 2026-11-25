"""SACCO PIN: set and check. Section 2.

Four digits, hashed with Argon2. Five wrong entries lock it for 15 minutes,
using the same numbers as the login lockout.
"""

import re
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.auth.lockout import LOCKOUT_DURATION, MAX_FAILED_ATTEMPTS
from app.errors import PIN_LOCKED, PIN_WRONG

_hasher = PasswordHasher()


class PinError(Exception):
    """`name` is one of the published codes in app.errors.INTERFACE_CODES."""

    def __init__(self, name: str, message: str):
        super().__init__(message)
        self.name = name
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


def set_pin(membership, pin: str) -> None:
    """Store a new PIN and clear any lock. The caller commits."""
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4}", pin):
        raise ValueError("The PIN must be exactly 4 digits.")
    membership.pin_hash = _hasher.hash(pin)
    membership.pin_attempts = 0
    membership.pin_locked_until = None


def check_pin(membership, pin: str, now: datetime | None = None) -> None:
    """Return normally if the PIN is right, else raise PinError.

    The caller commits, even on error, so the attempt count is kept.
    """
    now = now or _now()
    locked_until = membership.pin_locked_until
    if locked_until is not None and locked_until > now:
        raise PinError(PIN_LOCKED, "The PIN is locked. Try again later.")
    if locked_until is not None:  # the old lock ran out, start fresh
        membership.pin_attempts = 0
        membership.pin_locked_until = None
    if not membership.pin_hash:
        raise PinError(PIN_WRONG, "No PIN has been set.")
    try:
        _hasher.verify(membership.pin_hash, str(pin)[:32])
    except (VerifyMismatchError, InvalidHashError):
        membership.pin_attempts += 1
        if membership.pin_attempts >= MAX_FAILED_ATTEMPTS:
            membership.pin_locked_until = now + LOCKOUT_DURATION
            raise PinError(
                PIN_LOCKED, "Too many wrong PINs. The PIN is locked."
            ) from None
        raise PinError(PIN_WRONG, "That PIN is not correct.") from None
    membership.pin_attempts = 0
