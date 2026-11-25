from datetime import UTC, datetime, timedelta

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(UTC)


def check_lockout(user, now=None) -> bool:
    """True if the account is locked. Call this BEFORE checking the password."""
    now = now or _now()
    return user.locked_until is not None and user.locked_until > now


def record_failed_login(user, now=None) -> None:
    """Record a failed login; lock the account once the threshold is reached."""
    now = now or _now()
    if user.locked_until is not None and user.locked_until <= now:
        user.failed_login_count = 0  # old lock expired, start fresh
        user.locked_until = None
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= MAX_FAILED_ATTEMPTS:
        user.locked_until = now + LOCKOUT_DURATION


def reset_failed_logins(user) -> None:
    user.failed_login_count = 0
    user.locked_until = None
