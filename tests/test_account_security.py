"""Account security: POST /auth/change-password and POST /auth/logout-all."""

import pytest
from sqlalchemy import select

from app import create_app
from app.auth.limits import limiter
from app.auth.passwords import verify_password
from app.extensions import db
from app.models.audit import AuditLog
from app.models.refresh_token import RefreshToken
from app.models.sacco import SaccoStatus
from app.models.user import User
from config import TestConfig
from test_auth_routes import (  # noqa: F401  (clean_tables is an autouse fixture)
    EMAIL,
    PASSWORD,
    WRONG,
    clean_tables,
    join_sacco,
    logged_in,
    login,
    refresh,
    register,
)

NEW = "a-brand-new-passphrase"
OTHER_EMAIL = "other@example.com"
INVALID = {"error": {"code": "unauthorized", "message": "Invalid email or password."}}


def bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def change(client, tokens, current=PASSWORD, new=NEW, **extra):
    return client.post(
        "/api/v1/auth/change-password",
        json={"current_password": current, "new_password": new, **extra},
        headers=bearer(tokens),
    )


def logout_all(client, tokens):
    return client.post("/api/v1/auth/logout-all", headers=bearer(tokens))


def the_user(email=EMAIL):
    db.session.remove()
    return db.session.execute(select(User).where(User.email == email)).scalar_one()


def audit_rows(action):
    db.session.remove()
    return db.session.execute(
        select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    ).scalars().all()


def live_tokens(user_id):
    db.session.remove()
    return db.session.query(RefreshToken).filter_by(
        user_id=user_id, revoked_at=None
    ).count()


# ---------- change-password: who may call it ----------


@pytest.mark.parametrize("path", ["change-password", "logout-all"])
def test_no_token_is_401(client, path):
    resp = client.post(f"/api/v1/auth/{path}", json={})
    assert resp.status_code == 401
    assert resp.get_json()["error"]["message"] == "Authentication required."


@pytest.mark.parametrize("path", ["change-password", "logout-all"])
def test_a_refresh_token_is_not_an_access_token(client, path):
    tokens = logged_in(client)
    resp = client.post(
        f"/api/v1/auth/{path}",
        json={"current_password": PASSWORD, "new_password": NEW},
        headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
    )
    assert resp.status_code == 401


# ---------- change-password: wrong current password ----------


def test_a_wrong_current_password_is_401_and_counts_as_a_failed_login(client):
    tokens = logged_in(client)
    resp = change(client, tokens, current=WRONG)
    assert resp.status_code == 401
    assert resp.get_json() == INVALID == login(client, password=WRONG).get_json()
    # one from change-password, one from the login just above
    assert the_user().failed_login_count == 2
    assert verify_password(PASSWORD, the_user().password_hash)  # nothing changed


def test_five_wrong_attempts_lock_the_account(client):
    tokens = logged_in(client)
    codes = [change(client, tokens, current=WRONG).status_code for _ in range(5)]
    assert codes == [401] * 5
    assert the_user().locked_until is not None
    # locked: even the right password is refused, here and at login
    locked = change(client, tokens)
    assert locked.status_code == 423
    assert login(client).status_code == 423
    assert verify_password(PASSWORD, the_user().password_hash)


def test_wrong_attempts_at_login_lock_change_password_too(client):
    tokens = logged_in(client)
    for _ in range(5):
        login(client, password=WRONG)
    assert change(client, tokens).status_code == 423


def test_a_failed_attempt_leaves_the_sessions_and_the_audit_log_alone(client):
    tokens = logged_in(client)
    change(client, tokens, current=WRONG)
    assert live_tokens(the_user().id) == 1
    assert audit_rows("auth.password_change") == []


# ---------- change-password: bad input is 400 ----------


@pytest.mark.parametrize(
    "new, fragment",
    [
        ("short", "at least 8"),
        (f"xx{EMAIL.upper()}xx", "email"),
        (PASSWORD, "different"),
        ("x" * 129, "max 128"),
    ],
    ids=["too-short", "contains-email", "same-as-current", "over-length-cap"],
)
def test_bad_new_passwords_are_400(client, new, fragment):
    tokens = logged_in(client)
    resp = change(client, tokens, new=new)
    assert resp.status_code == 400
    assert fragment in resp.get_json()["error"]["message"]
    assert verify_password(PASSWORD, the_user().password_hash)
    assert live_tokens(the_user().id) == 1
    assert audit_rows("auth.password_change") == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"current_password": PASSWORD},
        {"new_password": NEW},
        {"current_password": 123, "new_password": NEW},
        {"current_password": PASSWORD, "new_password": ["x"]},
        {"current_password": PASSWORD, "new_password": "bad\x00pass-word"},
        {"current_password": "", "new_password": NEW},
    ],
)
def test_missing_or_malformed_fields_are_400_and_not_counted(client, body):
    tokens = logged_in(client)
    resp = client.post(
        "/api/v1/auth/change-password", json=body, headers=bearer(tokens)
    )
    assert resp.status_code == 400
    assert the_user().failed_login_count == 0


def test_a_non_json_body_is_400(client):
    tokens = logged_in(client)
    resp = client.post(
        "/api/v1/auth/change-password", data="nope", headers=bearer(tokens)
    )
    assert resp.status_code == 400


# ---------- change-password: success ----------


def test_success_changes_the_hash_and_the_new_password_logs_in(client):
    tokens = logged_in(client)
    old_hash = the_user().password_hash

    resp = change(client, tokens)
    assert resp.status_code == 200
    assert resp.get_json() == {"message": "Password changed. Sign in again."}
    assert resp.headers["Cache-Control"] == "no-store"

    user = the_user()
    assert user.password_hash != old_hash
    assert user.password_hash.startswith("$argon2")
    assert verify_password(NEW, user.password_hash)
    assert login(client, password=PASSWORD).status_code == 401
    assert login(client, password=NEW).status_code == 200


def test_success_revokes_every_refresh_token_of_the_user(client):
    tokens = logged_in(client)
    second = login(client).get_json()  # a second device
    assert live_tokens(the_user().id) == 2

    assert change(client, tokens).status_code == 200

    assert live_tokens(the_user().id) == 0
    assert refresh(client, tokens["refresh_token"]).status_code == 401
    assert refresh(client, second["refresh_token"]).status_code == 401
    fresh = login(client, password=NEW).get_json()
    assert refresh(client, fresh["refresh_token"]).status_code == 200


def test_an_access_token_issued_before_either_action_stops_working(client):
    tokens = logged_in(client)
    me = client.get("/api/v1/members/me", headers=bearer(tokens))
    assert me.status_code != 401
    assert change(client, tokens).status_code == 200
    stale = client.get("/api/v1/members/me", headers=bearer(tokens))
    assert stale.status_code == 401

    fresh = login(client, password=NEW).get_json()
    assert client.get("/api/v1/members/me", headers=bearer(fresh)).status_code != 401
    assert logout_all(client, fresh).status_code == 200
    assert client.get("/api/v1/members/me", headers=bearer(fresh)).status_code == 401


def test_a_token_without_a_version_is_refused(client, app):
    from app.auth import tokens as token_module

    user_id = logged_in(client) and the_user().id
    with app.app_context():
        lifetime = app.config["ACCESS_TOKEN_EXPIRES"]
        old = token_module._encode(user_id, "access", lifetime)[0]
    resp = client.get("/api/v1/members/me", headers={"Authorization": f"Bearer {old}"})
    assert resp.status_code == 401


def test_success_resets_the_failure_count(client):
    tokens = logged_in(client)
    for _ in range(3):
        change(client, tokens, current=WRONG)
    assert the_user().failed_login_count == 3
    assert change(client, tokens).status_code == 200
    user = the_user()
    assert user.failed_login_count == 0 and user.locked_until is None


def test_success_leaves_another_users_tokens_and_password_alone(client):
    register(client, email=OTHER_EMAIL)
    theirs = login(client, email=OTHER_EMAIL).get_json()
    other_hash = the_user(OTHER_EMAIL).password_hash
    mine = logged_in(client)

    assert change(client, mine).status_code == 200

    assert the_user(OTHER_EMAIL).password_hash == other_hash
    assert refresh(client, theirs["refresh_token"]).status_code == 200


def test_success_writes_one_audit_row_with_the_user_as_actor(client):
    tokens = logged_in(client)
    change(client, tokens)
    rows = audit_rows("auth.password_change")
    assert len(rows) == 1
    assert rows[0].actor_id == the_user().id
    assert rows[0].sacco_id is None
    # nothing secret is stored
    assert NEW not in f"{rows[0].target}" and PASSWORD not in f"{rows[0].target}"


def test_a_suspended_sacco_member_may_still_change_their_password(client):
    tokens = logged_in(client)
    join_sacco(SaccoStatus.SUSPENDED)
    assert change(client, tokens).status_code == 200


# ---------- logout-all ----------


def test_logout_all_revokes_every_token_of_the_caller(client):
    tokens = logged_in(client)
    second = login(client).get_json()

    resp = logout_all(client, tokens)
    assert resp.status_code == 200
    assert resp.get_json() == {"message": "Logged out everywhere."}
    assert resp.headers["Cache-Control"] == "no-store"

    assert live_tokens(the_user().id) == 0
    assert refresh(client, tokens["refresh_token"]).status_code == 401
    assert refresh(client, second["refresh_token"]).status_code == 401


def test_logout_all_revokes_only_that_users_tokens(client):
    register(client, email=OTHER_EMAIL)
    theirs = login(client, email=OTHER_EMAIL).get_json()
    mine = logged_in(client)

    assert logout_all(client, mine).status_code == 200

    assert live_tokens(the_user(OTHER_EMAIL).id) == 1
    assert refresh(client, theirs["refresh_token"]).status_code == 200
    assert refresh(client, mine["refresh_token"]).status_code == 401


def test_logout_all_twice_is_fine(client):
    tokens = logged_in(client)
    assert logout_all(client, tokens).status_code == 200
    # The first call also killed this access token; signing in again works.
    assert logout_all(client, tokens).status_code == 401
    assert logout_all(client, login(client).get_json()).status_code == 200
    assert len(audit_rows("auth.logout_all")) == 2


def test_logout_all_writes_an_audit_row_with_the_user_as_actor(client):
    register(client, email=OTHER_EMAIL)
    mine = logged_in(client)
    logout_all(client, mine)
    rows = audit_rows("auth.logout_all")
    assert len(rows) == 1
    assert rows[0].actor_id == the_user().id
    assert rows[0].actor_id != the_user(OTHER_EMAIL).id


@pytest.mark.parametrize("sacco_status", list(SaccoStatus))
def test_logout_all_works_for_a_member_of_a_suspended_sacco(client, sacco_status):
    tokens = logged_in(client)
    join_sacco(sacco_status)
    assert logout_all(client, tokens).status_code == 200
    assert live_tokens(the_user().id) == 0


# ---------- rate limit ----------


def test_change_password_shares_logins_limit_of_five_per_minute(app):
    class Limited(TestConfig):
        RATELIMIT_ENABLED = True

    limited = create_app(Limited)
    client = limited.test_client()
    limiter.reset()
    try:
        tokens = logged_in(app.test_client())  # the shared app has limits off
        codes = [change(client, tokens, current=WRONG).status_code for _ in range(6)]
        assert 429 not in codes[:5] and codes[5] == 429
        assert change(client, tokens).get_json()["error"]["message"]
    finally:
        limiter.reset()
        limiter.enabled = False


# ---------- refresh racing logout-all ----------


def test_a_refresh_in_flight_cannot_outlive_logout_all(app, client, monkeypatch):
    """logout-all arrives while a refresh has spent its token but not committed."""
    import threading
    import time

    from app.auth import tokens

    tokens_ = logged_in(client)
    user_id = the_user().id
    real_create = tokens.create_refresh_token
    result = {}

    def sweep():
        result["resp"] = logout_all(app.test_client(), tokens_)

    def slow_create(uid):
        worker = threading.Thread(target=sweep)
        worker.start()
        time.sleep(0.5)  # the sweep must be waiting on the user's row by now
        result["worker"] = worker
        return real_create(uid)

    monkeypatch.setattr(tokens, "create_refresh_token", slow_create)
    assert refresh(client, tokens_["refresh_token"]).status_code == 200
    result["worker"].join(5)

    assert result["resp"].status_code == 200
    assert live_tokens(user_id) == 0


def test_change_password_reads_the_row_after_the_lock(client, monkeypatch):
    """A password changed after the guard loaded the user must not be bypassed."""
    from sqlalchemy import text

    from app.auth import account
    from app.auth.passwords import hash_password

    tokens_ = logged_in(client)
    real_lock = account.db.session.execute
    swapped = {}

    def swap_then_execute(stmt, *a, **kw):
        # Runs at the lock: another request has just committed a new password.
        if not swapped and "FOR UPDATE" in str(stmt):
            with db.engine.begin() as conn:
                conn.execute(
                    text("UPDATE users SET password_hash = :h WHERE email = :e"),
                    {"h": hash_password("someone-elses-new-pass"), "e": EMAIL},
                )
            swapped["done"] = True
        return real_lock(stmt, *a, **kw)

    monkeypatch.setattr(account.db.session, "execute", swap_then_execute, raising=False)
    resp = change(client, tokens_)  # still sends the OLD password
    monkeypatch.undo()
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "claims", [{"iss": "someone-else"}, {"aud": "another-service"}]
)
def test_a_token_for_another_issuer_or_audience_is_refused(app, client, claims):
    from datetime import UTC, datetime, timedelta

    import jwt

    logged_in(client)
    now = datetime.now(UTC)
    payload = {
        "sub": str(the_user().id),
        "type": "access",
        "jti": "a" * 32,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "tv": 0,
        "iss": "nexora-api",
        "aud": "nexora",
    } | claims
    token = jwt.encode(payload, app.config["SECRET_KEY"], algorithm="HS256")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/members/me", headers=headers).status_code == 401
