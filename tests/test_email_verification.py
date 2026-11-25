"""Email verification: only an address's owner can be added to a SACCO."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models.email_verification import EmailVerification
from app.models.membership import MembershipRole, SaccoMembership
from app.models.sacco import Sacco
from app.models.user import User
from test_auth_routes import (  # noqa: F401  (clean_tables is an autouse fixture)
    EMAIL,
    PASSWORD,
    clean_tables,
    login,
    register,
)
from test_members import PASSWORD_HASH

VERIFY = "/api/v1/auth/verify-email"
RESEND = "/api/v1/auth/resend-verification"


@pytest.fixture
def mailbox(monkeypatch):
    """Every verification email the app tries to send: (address, raw token)."""
    sent = []
    def record(to, token, purpose="verify"):
        sent.append((to, token) if purpose == "verify" else (to, token, purpose))

    monkeypatch.setattr("app.auth.routes.send_safely", record)
    monkeypatch.setattr("app.members.self_service.send_safely", record)
    return sent


def user(email=EMAIL):
    db.session.remove()
    return db.session.execute(select(User).where(User.email == email)).scalar_one()


def test_register_sends_a_link_and_leaves_the_address_unverified(client, mailbox):
    assert register(client).status_code == 201
    assert user().email_verified_at is None
    assert [to for to, _ in mailbox] == [EMAIL]


def test_only_the_hash_of_the_token_is_stored(client, mailbox):
    register(client)
    token = mailbox[0][1]
    db.session.remove()
    row = db.session.execute(select(EmailVerification)).scalar_one()
    assert token not in row.token_hash and len(row.token_hash) == 64


def test_the_link_verifies_once(client, mailbox):
    register(client)
    token = mailbox[0][1]
    ok = client.post(VERIFY, json={"token": token})
    assert ok.status_code == 200
    assert user().email_verified_at is not None
    again = client.post(VERIFY, json={"token": token})
    assert again.status_code == 400
    assert again.get_json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "body", [{}, {"token": ""}, {"token": 5}, {"token": "x" * 500}]
)
def test_a_bad_token_is_400(client, body):
    assert client.post(VERIFY, json=body).status_code == 400


def test_an_expired_link_is_400(client, mailbox):
    register(client)
    row = db.session.execute(select(EmailVerification)).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.session.commit()
    assert client.post(VERIFY, json={"token": mailbox[0][1]}).status_code == 400
    assert user().email_verified_at is None


def test_resend_replaces_the_old_link_after_the_cooldown(client, mailbox):
    register(client)
    old = mailbox[0][1]
    row = db.session.execute(select(EmailVerification)).scalar_one()
    row.created_at = datetime.now(UTC) - timedelta(seconds=120)
    db.session.commit()
    assert client.post(RESEND, json={"email": EMAIL}).status_code == 200
    new = mailbox[1][1]
    assert new != old
    assert client.post(VERIFY, json={"token": old}).status_code == 400
    assert client.post(VERIFY, json={"token": new}).status_code == 200


def test_resending_within_a_minute_sends_nothing_new(client, mailbox):
    register(client)
    for _ in range(3):
        assert client.post(RESEND, json={"email": EMAIL}).status_code == 200
    assert len(mailbox) == 1
    assert client.post(VERIFY, json={"token": mailbox[0][1]}).status_code == 200


def test_resend_answers_the_same_for_unknown_and_verified_addresses(client, mailbox):
    register(client)
    client.post(VERIFY, json={"token": mailbox[0][1]})
    sent_before = len(mailbox)
    known = client.post(RESEND, json={"email": EMAIL})
    unknown = client.post(RESEND, json={"email": "nobody@example.com"})
    junk = client.post(RESEND, json={"email": 5})
    assert known.status_code == unknown.status_code == junk.status_code == 200
    assert known.get_json() == unknown.get_json() == junk.get_json()
    assert len(mailbox) == sent_before


def test_login_does_not_need_verification(client, mailbox):
    register(client)
    assert login(client).status_code == 200


# ---------- adding to a SACCO ----------


@pytest.fixture
def sacco_and_admin(client):
    sacco = Sacco(name="Verify SACCO")
    boss = User(
        email="boss@example.com",
        password_hash=PASSWORD_HASH,
        email_verified_at=datetime.now(UTC),
    )
    db.session.add_all([sacco, boss])
    db.session.flush()
    db.session.add(
        SaccoMembership(
            user_id=boss.id, sacco_id=sacco.id, role=MembershipRole.SACCO_ADMIN
        )
    )
    db.session.commit()
    from app.auth.tokens import create_access_token

    token = create_access_token(boss.id, boss.token_version)
    return sacco.id, {"Authorization": f"Bearer {token}"}


def test_an_unverified_user_cannot_be_added_until_they_verify(
    client, mailbox, sacco_and_admin
):
    sacco_id, headers = sacco_and_admin
    register(client)
    url = f"/api/v1/saccos/{sacco_id}/members"

    refused = client.post(url, json={"email": EMAIL}, headers=headers)
    assert refused.status_code == 409
    assert refused.get_json()["error"]["message"] == (
        "That user has not verified their email yet."
    )

    client.post(VERIFY, json={"token": mailbox[0][1]})
    assert client.post(url, json={"email": EMAIL}, headers=headers).status_code == 201


def test_changing_the_email_needs_the_new_address_proved(client, mailbox):
    register(client)
    client.post(VERIFY, json={"token": mailbox[0][1]})
    tokens = login(client).get_json()
    resp = client.patch(
        "/api/v1/members/me",
        json={"email": "moved@example.com", "current_password": PASSWORD},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert resp.status_code == 200
    assert user("moved@example.com").email_verified_at is None
    assert mailbox[-1][0] == "moved@example.com"
    # Whoever held a session before the change has to sign in again.
    stale = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert client.get("/api/v1/members/me", headers=stale).status_code == 401
    assert client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    ).status_code == 401
