from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.auth.join import JoinError, check_code, digest_code, generate_code

SECRET = "test-secret"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def make_row(code="123456", minutes=15, used_at=None, attempts=0):
    return SimpleNamespace(
        membership_id=7,
        code_digest=digest_code(7, code, SECRET),
        expires_at=NOW + timedelta(minutes=minutes),
        used_at=used_at,
        attempts=attempts,
    )


def test_code_is_six_digits():
    for _ in range(50):
        assert len(generate_code()) == 6 and generate_code().isdigit()


def test_right_code_is_spent_once():
    row = make_row()
    check_code(row, "123456", SECRET, NOW)
    assert row.used_at == NOW
    with pytest.raises(JoinError) as exc:
        check_code(row, "123456", SECRET, NOW)
    assert exc.value.name == "CODE_USED"


def test_expired_code_fails():
    row = make_row(minutes=15)
    with pytest.raises(JoinError) as exc:
        check_code(row, "123456", SECRET, NOW + timedelta(minutes=16))
    assert exc.value.name == "CODE_EXPIRED"


def test_five_wrong_codes_burn_it():
    row = make_row()
    for _ in range(5):
        with pytest.raises(JoinError) as exc:
            check_code(row, "000000", SECRET, NOW)
        assert exc.value.name == "CODE_WRONG"
    with pytest.raises(JoinError) as exc:
        check_code(row, "123456", SECRET, NOW)  # even the right code now fails
    assert exc.value.name == "CODE_USED"


def test_code_for_another_membership_is_wrong():
    row = make_row()
    row.membership_id = 8
    with pytest.raises(JoinError) as exc:
        check_code(row, "123456", SECRET, NOW)
    assert exc.value.name == "CODE_WRONG"
