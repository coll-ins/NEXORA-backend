"""Forgot and reset password: the way back in after a lockout or a lost password."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models.audit import AuditLog
from app.models.email_verification import EmailVerification
from app.models.refresh_token import RefreshToken
from app.models.user import User
from test_auth_routes import (  # noqa: F401  (clean_tables is an autouse fixture)
    EMAIL,
    PASSWORD,
    WRONG,
    clean_tables,
    logged_in,
    login,
    refresh,
    register,
)

FORGOT = "/api/v1/auth/forgot-password"
RESET = "/api/v1/auth/reset-password"
NEW = "a-brand-new-passphrase"


@pytest.fixture
def mailbox(monkeypatch):
    """Every link email the app tries to send: (address, raw token, purpose)."""
    sent = []

    def record(to, token, purpose="verify"):
        sent.append((to, token, purpose))

    monkeypatch.setattr("app.auth.routes.send_safely", record)
    return sent


def reset(client, token, password=NEW):
    return client.post(RESET, json={"token": token, "new_password": password})


def age_links(seconds=120):
    """Pretend every link was issued `seconds` ago, so the cooldown has passed."""
    db.session.remove()
    for row in db.session.execute(select(EmailVerification)).scalars():
        row.created_at = datetime.now(UTC) - timedelta(seconds=seconds)
    db.session.commit()


def resets(mailbox):
    return [m for m in mailbox if m[2] == "reset"]


def user():
    db.session.remove()
    return db.session.execute(select(User).where(User.email == EMAIL)).scalar_one()


def test_forgot_password_emails_a_link_to_a_known_address(client, mailbox):
    register(client)
    assert client.post(FORGOT, json={"email": EMAIL}).status_code == 200
    assert [(to, purpose) for to, _, purpose in resets(mailbox)] == [(EMAIL, "reset")]


def test_forgot_password_answers_the_same_for_unknown_addresses(client, mailbox):
    register(client)
    known = client.post(FORGOT, json={"email": EMAIL})
    unknown = client.post(FORGOT, json={"email": "nobody@example.com"})
    junk = client.post(FORGOT, json={"email": 5})
    assert known.status_code == unknown.status_code == junk.status_code == 200
    assert known.get_json() == unknown.get_json() == junk.get_json()
    assert len(resets(mailbox)) == 1


def test_a_reset_sets_the_password_and_ends_every_session(client, mailbox):
    tokens = logged_in(client)
    client.post(FORGOT, json={"email": EMAIL})
    token = resets(mailbox)[0][1]

    ok = reset(client, token)
    assert ok.status_code == 200

    assert login(client, password=PASSWORD).status_code == 401
    assert login(client, password=NEW).status_code == 200
    assert refresh(client, tokens["refresh_token"]).status_code == 401
    stale = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert client.get("/api/v1/members/me", headers=stale).status_code == 401


def test_a_reset_link_works_once(client, mailbox):
    register(client)
    client.post(FORGOT, json={"email": EMAIL})
    token = resets(mailbox)[0][1]
    assert reset(client, token).status_code == 200
    again = reset(client, token, NEW + "x")
    assert again.status_code == 400
    assert login(client, password=NEW).status_code == 200


def test_a_reset_unlocks_a_locked_account(client, mailbox):
    register(client)
    for _ in range(5):
        login(client, password=WRONG)
    assert login(client).status_code == 423
    client.post(FORGOT, json={"email": EMAIL})
    token = resets(mailbox)[0][1]
    assert reset(client, token).status_code == 200
    assert user().failed_login_count == 0
    assert login(client, password=NEW).status_code == 200


def test_a_reset_proves_the_address(client, mailbox):
    register(client)
    assert user().email_verified_at is None
    client.post(FORGOT, json={"email": EMAIL})
    reset(client, resets(mailbox)[0][1])
    assert user().email_verified_at is not None


def test_a_bad_new_password_is_400_and_keeps_the_link(client, mailbox):
    register(client)
    client.post(FORGOT, json={"email": EMAIL})
    token = resets(mailbox)[0][1]
    for bad in ("short", EMAIL, "x" * 200, None, 5):
        resp = reset(client, token, bad)
        assert resp.status_code == 400, bad
    assert login(client).status_code == 200  # the old password still works
    assert reset(client, token).status_code == 200


@pytest.mark.parametrize("token", [None, "", 5, "nope", "x" * 500])
def test_a_bad_token_is_400(client, token):
    resp = reset(client, token)
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "validation_error"


def test_an_expired_link_is_400(client, mailbox):
    register(client)
    client.post(FORGOT, json={"email": EMAIL})
    row = db.session.execute(
        select(EmailVerification).where(EmailVerification.purpose == "reset")
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.session.commit()
    resp = reset(client, resets(mailbox)[0][1])
    assert resp.status_code == 400


def test_a_verification_link_cannot_reset_a_password(client, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.auth.routes.send_safely", lambda to, t, purpose="verify": sent.append(t)
    )
    register(client)
    resp = reset(client, sent[0])
    assert resp.status_code == 400


def test_a_new_link_replaces_the_old_one_after_the_cooldown(client, mailbox):
    register(client)
    client.post(FORGOT, json={"email": EMAIL})
    age_links()
    client.post(FORGOT, json={"email": EMAIL})
    first, second = (m[1] for m in resets(mailbox))
    assert reset(client, first).status_code == 400
    assert reset(client, second).status_code == 200


def test_asking_again_within_a_minute_keeps_the_first_link(client, mailbox):
    """Otherwise anyone could keep a victim's link dead and flood their inbox."""
    register(client)
    for _ in range(3):
        assert client.post(FORGOT, json={"email": EMAIL}).status_code == 200
    assert len(resets(mailbox)) == 1
    assert reset(client, resets(mailbox)[0][1]).status_code == 200


def test_a_link_issued_before_an_email_change_is_dead(client, mailbox):
    tokens = logged_in(client)
    client.post(FORGOT, json={"email": EMAIL})
    old_link = resets(mailbox)[0][1]
    resp = client.patch(
        "/api/v1/members/me",
        json={"email": "moved@example.com", "current_password": PASSWORD},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert resp.status_code == 200
    assert reset(client, old_link).status_code == 400


def test_a_link_issued_before_a_password_change_is_dead(client, mailbox):
    tokens = logged_in(client)
    client.post(FORGOT, json={"email": EMAIL})
    old_link = resets(mailbox)[0][1]
    resp = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert resp.status_code == 200
    assert reset(client, old_link).status_code == 400


def test_a_request_for_a_link_is_audited(client, mailbox):
    register(client)
    client.post(FORGOT, json={"email": EMAIL})
    client.post(FORGOT, json={"email": "nobody@example.com"})
    db.session.remove()
    rows = db.session.execute(
        select(AuditLog).where(AuditLog.action == "auth.password_reset_requested")
    ).scalars().all()
    assert [r.actor_id for r in rows] == [user().id]


def test_the_reset_is_audited_and_revokes_refresh_tokens(client, mailbox):
    logged_in(client)
    client.post(FORGOT, json={"email": EMAIL})
    reset(client, resets(mailbox)[0][1])
    db.session.remove()
    rows = db.session.execute(
        select(AuditLog).where(AuditLog.action == "auth.password_reset")
    ).scalars().all()
    assert [r.actor_id for r in rows] == [user().id]
    live = db.session.query(RefreshToken).filter_by(revoked_at=None).count()
    assert live == 0
