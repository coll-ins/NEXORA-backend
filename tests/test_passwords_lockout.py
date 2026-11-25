from datetime import UTC, datetime, timedelta

from app.auth.lockout import (
    check_lockout,
    record_failed_login,
    reset_failed_logins,
)
from app.auth.passwords import hash_password, validate_password, verify_password
from app.models.user import User

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def make_user():
    # The real model, so a column rename breaks these tests instead of production.
    return User(
        email="a@b.com", password_hash="x", failed_login_count=0, locked_until=None
    )


def test_hash_and_verify():
    h = hash_password("correct-horse")
    assert h != "correct-horse"
    assert verify_password("correct-horse", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "not-a-hash")


def test_validate_password():
    assert (
        validate_password("short", "a@b.com")
        == "Password must be at least 8 characters long."
    )
    assert validate_password("a@b.com", "A@B.com") is not None
    assert validate_password("long-enough-pw", "a@b.com") is None


def test_five_failures_lock_for_15_minutes():
    u = make_user()
    for _ in range(4):
        record_failed_login(u, now=T0)
    assert not check_lockout(u, now=T0)
    record_failed_login(u, now=T0)
    assert check_lockout(u, now=T0)
    assert check_lockout(u, now=T0 + timedelta(minutes=14))
    assert not check_lockout(u, now=T0 + timedelta(minutes=15, seconds=1))


def test_success_resets_counter():
    u = make_user()
    for _ in range(3):
        record_failed_login(u, now=T0)
    reset_failed_logins(u)
    assert u.failed_login_count == 0
    assert u.locked_until is None


def test_attempts_restart_after_lock_expires():
    u = make_user()
    for _ in range(5):
        record_failed_login(u, now=T0)
    record_failed_login(u, now=T0 + timedelta(minutes=16))
    assert u.failed_login_count == 1
    assert not check_lockout(u, now=T0 + timedelta(minutes=16))


def test_check_lockout_works_with_default_clock_and_aware_column_value():
    u = make_user()
    u.locked_until = datetime.now(UTC) + timedelta(minutes=5)
    assert check_lockout(u) is True
    u.locked_until = datetime.now(UTC) - timedelta(minutes=5)
    assert check_lockout(u) is False
