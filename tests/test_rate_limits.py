"""The three rate-limit classes: reads, ordinary writes and money movements.

Each class gets its own allowance, and the request past the limit answers 429
with the JSON error every client already sees for a breach.
"""

import pytest

from app import create_app
from app.auth.limits import (
    LOGIN_LIMIT,
    MONEY_LIMIT,
    READ_LIMIT,
    WRITE_LIMIT,
    limiter,
)
from config import TestConfig


@pytest.fixture
def limited(app):
    """The real limits on, for one app, and off again afterwards."""

    class Limited(TestConfig):
        RATELIMIT_ENABLED = True

    client = create_app(Limited).test_client()
    limiter.reset()
    yield client
    limiter.reset()
    limiter.enabled = False


def per_minute(limit):
    return int(limit.split()[0])


def test_the_classes_are_the_agreed_rates():
    assert READ_LIMIT == "120 per minute"
    assert WRITE_LIMIT == "30 per minute"
    assert MONEY_LIMIT == "20 per minute"


def _exhaust(client, method, path, limit):
    """Call the route `limit` times, then once more; return every status code."""
    call = getattr(client, method)
    codes = [call(path).status_code for _ in range(per_minute(limit))]
    assert 429 not in codes
    return codes + [call(path).status_code]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/saccos/1",
        "/api/v1/savings/accounts/me",
        # A read of the member's own request history, not a write: it must not
        # run out at the 30 a minute that sending a request does.
        "/api/v1/savings/requests/me",
    ],
)
def test_reads_allow_120_a_minute(limited, path):
    codes = _exhaust(limited, "get", path, READ_LIMIT)
    assert codes[-1] == 429
    assert limited.get(path).get_json()["error"]["message"]


def test_health_is_a_read(limited):
    """Each call opens a database connection, so it is limited like a read."""
    codes = _exhaust(limited, "get", "/api/health", READ_LIMIT)
    assert codes[-1] == 429


def test_writes_allow_30_a_minute(limited):
    codes = _exhaust(limited, "post", "/api/v1/saccos", WRITE_LIMIT)
    assert codes[-1] == 429


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/savings/accounts/1/deposits",
        "/api/v1/savings/accounts/1/withdrawals",
        "/api/v1/savings/transactions/1/reversals",
    ],
)
def test_money_movements_allow_20_a_minute(limited, path):
    codes = _exhaust(limited, "post", path, MONEY_LIMIT)
    assert codes[-1] == 429


def test_each_route_has_its_own_allowance(limited):
    _exhaust(limited, "get", "/api/v1/saccos/1", READ_LIMIT)
    # A different read route is untouched by that route's counter.
    assert limited.get("/api/v1/savings/accounts/me").status_code != 429


def test_change_password_shares_the_login_limit(limited):
    codes = [
        limited.post("/api/v1/auth/change-password", json={}).status_code
        for _ in range(per_minute(LOGIN_LIMIT) + 1)
    ]
    assert codes[-1] == 429
    assert 429 not in codes[:-1]


def test_logout_all_is_an_ordinary_write(limited):
    codes = [
        limited.post("/api/v1/auth/logout-all").status_code
        for _ in range(per_minute(WRITE_LIMIT) + 1)
    ]
    assert codes[-1] == 429
    assert 429 not in codes[:-1]


@pytest.mark.parametrize(
    "path", ["/api/v1/auth/forgot-password", "/api/v1/auth/resend-verification"]
)
def test_the_always_ok_routes_still_stop_at_the_login_limit(limited, path):
    codes = [
        limited.post(path, json={"email": "nobody@example.com"}).status_code
        for _ in range(per_minute(LOGIN_LIMIT) + 1)
    ]
    assert codes[-1] == 429
    assert set(codes[:-1]) == {200}
