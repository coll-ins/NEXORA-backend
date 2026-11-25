from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.auth.pin import PinError, check_pin, set_pin

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def blank_membership():
    return SimpleNamespace(pin_hash=None, pin_attempts=0, pin_locked_until=None)


def make_membership():
    m = blank_membership()
    set_pin(m, "1234")
    return m


@pytest.mark.parametrize("bad", ["123", "12345", "12a4", "", None, 1234])
def test_set_pin_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        set_pin(blank_membership(), bad)


def test_pin_is_stored_hashed():
    assert make_membership().pin_hash.startswith("$argon2")


def test_right_pin_passes_and_clears_attempts():
    m = make_membership()
    with pytest.raises(PinError):
        check_pin(m, "0000", NOW)
    check_pin(m, "1234", NOW)
    assert m.pin_attempts == 0


def test_five_wrong_pins_lock_it():
    m = make_membership()
    for _ in range(4):
        with pytest.raises(PinError) as exc:
            check_pin(m, "0000", NOW)
        assert exc.value.name == "PIN_WRONG"
    with pytest.raises(PinError) as exc:
        check_pin(m, "0000", NOW)
    assert exc.value.name == "PIN_LOCKED"
    with pytest.raises(PinError) as exc:
        check_pin(m, "1234", NOW)  # the right PIN is refused while locked
    assert exc.value.name == "PIN_LOCKED"


def test_lock_ends_after_fifteen_minutes():
    m = make_membership()
    for _ in range(5):
        with pytest.raises(PinError):
            check_pin(m, "0000", NOW)
    check_pin(m, "1234", NOW + timedelta(minutes=16))
    assert m.pin_attempts == 0
    assert m.pin_locked_until is None
