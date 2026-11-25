"""Declining a SACCO invitation: the reason is required, kept for the officers,
and the member is never enrolled."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.auth.tokens import create_access_token
from app.extensions import db
from app.models.identity import JoinCode
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.models.sacco import Sacco
from app.models.user import User
from app.saccos import rules as sacco_rules
from test_sacco_rules import RULES

PIN = "1234"


def _truncate():
    db.session.remove()
    db.session.execute(text("TRUNCATE users, saccos RESTART IDENTITY CASCADE"))
    db.session.commit()


@pytest.fixture(autouse=True)
def clean_tables(app):
    _truncate()
    yield
    _truncate()


def make_user(email):
    user = User(
        email=email,
        password_hash="not-a-real-hash",
        email_verified_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


def auth(user):
    token = create_access_token(user.id, user.token_version)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sent(monkeypatch):
    """The join-code emails the API asked to send: (to, code, sacco name, minutes)."""
    outbox = []
    monkeypatch.setattr(
        "app.members.routes.send_join_code_safely",
        lambda *args: outbox.append(args),
    )
    return outbox


@pytest.fixture
def world(app):
    sacco = Sacco(name="Umoja SACCO")
    db.session.add(sacco)
    db.session.commit()
    admin = make_user("admin@example.com")
    db.session.add(
        SaccoMembership(
            user_id=admin.id,
            sacco_id=sacco.id,
            role=MembershipRole.SACCO_ADMIN,
            status=MembershipStatus.ACTIVE,
        )
    )
    db.session.commit()
    values, errors = sacco_rules.validate_rules(RULES)
    assert not errors, errors
    rules = sacco_rules.publish_rules(sacco.id, values, admin.id)
    db.session.commit()
    return SimpleNamespace(
        sacco=sacco,
        admin=admin,
        joiner=make_user("joiner@example.com"),
        other=make_user("other@example.com"),
        rules=rules,
    )


def invite(client, world, user=None):
    """The admin invites the joiner by email and the joiner enters the code."""
    made = client.post(
        f"/api/v1/saccos/{world.sacco.id}/members",
        json={"email": "joiner@example.com", "role": "member", "invite": True},
        headers=auth(world.admin),
    )
    assert made.status_code == 201, made.get_data(as_text=True)
    return made.get_json()


def join(client, world, code, user=None):
    """The joiner enters the code: the setup token that the rules screens need."""
    joined = client.post(
        "/api/v1/auth/join", json={"code": code}, headers=auth(user or world.joiner)
    )
    assert joined.status_code == 200, joined.get_data(as_text=True)
    return joined.get_json()["setup_token"]


def row_of(membership_id):
    db.session.expire_all()
    return db.session.get(SaccoMembership, membership_id)


def test_declining_keeps_the_reason_and_never_enrols_the_member(
    client, world, sent
):
    invited = invite(client, world)
    token = join(client, world, sent[0][1])

    done = client.post(
        "/api/v1/auth/join/rules/decline",
        json={
            "setup_token": token,
            "reason": "The fees are too high for me.",
            "version": 1,
        },
        headers=auth(world.joiner),
    )
    assert done.status_code == 200, done.get_data(as_text=True)
    assert done.get_json() == {"membership_id": invited["id"], "status": "declined"}

    membership = row_of(invited["id"])
    assert membership.status is MembershipStatus.DECLINED
    assert membership.decline_reason == "The fees are too high for me."
    assert membership.declined_at is not None
    assert membership.pin_hash is None

    # Not enrolled: no PIN, no way into the SACCO.
    blocked = client.post(
        "/api/v1/auth/join/pin",
        json={"setup_token": token, "pin": PIN},
        headers=auth(world.joiner),
    )
    assert blocked.status_code in (401, 403, 409)
    entered = client.post(
        "/api/v1/auth/sacco-login",
        json={"sacco_id": world.sacco.id, "pin": PIN},
        headers=auth(world.joiner),
    )
    assert entered.status_code == 403

    # The code is spent, so the invitation cannot be finished later.
    db.session.expire_all()
    codes = db.session.execute(
        select(JoinCode).where(JoinCode.membership_id == invited["id"])
    ).scalars().all()
    assert codes and all(row.used_at is not None for row in codes)


def test_the_officers_see_the_reason_on_the_member(client, world, sent):
    invited = invite(client, world)
    token = join(client, world, sent[0][1])
    client.post(
        "/api/v1/auth/join/rules/decline",
        json={
            "setup_token": token,
            "reason": "I joined my other SACCO instead.",
            "version": 1,
        },
        headers=auth(world.joiner),
    )

    one = client.get(
        f"/api/v1/saccos/{world.sacco.id}/members/{invited['id']}",
        headers=auth(world.admin),
    )
    assert one.status_code == 200
    body = one.get_json()
    assert body["status"] == "declined"
    assert body["decline_reason"] == "I joined my other SACCO instead."
    assert body["declined_at"]

    listed = client.get(
        f"/api/v1/saccos/{world.sacco.id}/members", headers=auth(world.admin)
    ).get_json()["members"]
    mine = next(row for row in listed if row["id"] == invited["id"])
    assert mine["decline_reason"] == "I joined my other SACCO instead."


def test_a_reason_is_required_and_kept_short(client, world, sent):
    invited = invite(client, world)
    token = join(client, world, sent[0][1])
    for reason in (None, "", "   "):
        body = {"setup_token": token, "version": 1}
        if reason is not None:
            body["reason"] = reason
        refused = client.post(
            "/api/v1/auth/join/rules/decline", json=body, headers=auth(world.joiner)
        )
        assert refused.status_code == 400, reason
        assert "why you are declining" in refused.get_json()["error"]["message"]
    too_long = client.post(
        "/api/v1/auth/join/rules/decline",
        json={"setup_token": token, "version": 1, "reason": "x" * 501},
        headers=auth(world.joiner),
    )
    assert too_long.status_code == 400
    # Nothing was saved: the invitation still stands.
    assert row_of(invited["id"]).status is MembershipStatus.INVITED


def test_only_the_person_invited_can_decline(client, world, sent):
    invited = invite(client, world)
    token = join(client, world, sent[0][1])
    stolen = client.post(
        "/api/v1/auth/join/rules/decline",
        json={"setup_token": token, "reason": "Not mine.", "version": 1},
        headers=auth(world.other),
    )
    assert stolen.status_code == 403
    anonymous = client.post(
        "/api/v1/auth/join/rules/decline",
        json={"setup_token": token, "reason": "No account.", "version": 1},
    )
    assert anonymous.status_code == 401
    assert row_of(invited["id"]).status is MembershipStatus.INVITED


def test_declining_the_wrong_rules_version_is_refused(client, world, sent):
    invited = invite(client, world)
    token = join(client, world, sent[0][1])
    stale = client.post(
        "/api/v1/auth/join/rules/decline",
        json={"setup_token": token, "reason": "Not for me.", "version": 99},
        headers=auth(world.joiner),
    )
    assert stale.status_code == 409
    assert row_of(invited["id"]).status is MembershipStatus.INVITED


def test_declining_twice_is_refused(client, world, sent):
    invite(client, world)
    token = join(client, world, sent[0][1])
    first = client.post(
        "/api/v1/auth/join/rules/decline",
        json={"setup_token": token, "reason": "Too expensive.", "version": 1},
        headers=auth(world.joiner),
    )
    assert first.status_code == 200
    again = client.post(
        "/api/v1/auth/join/rules/decline",
        json={"setup_token": token, "reason": "Still too expensive.", "version": 1},
        headers=auth(world.joiner),
    )
    assert again.status_code == 403


def test_an_officer_can_invite_a_person_who_declined_again(client, world, sent):
    invited = invite(client, world)
    token = join(client, world, sent[0][1])
    client.post(
        "/api/v1/auth/join/rules/decline",
        json={"setup_token": token, "reason": "Changed my mind later.", "version": 1},
        headers=auth(world.joiner),
    )

    again = client.post(
        f"/api/v1/saccos/{world.sacco.id}/members",
        json={"email": "joiner@example.com", "role": "member", "invite": True},
        headers=auth(world.admin),
    )
    assert again.status_code == 200, again.get_data(as_text=True)
    rejoined = client.post(
        "/api/v1/auth/join", json={"code": sent[-1][1]}, headers=auth(world.joiner)
    )
    assert rejoined.status_code == 200, rejoined.get_data(as_text=True)
    membership = row_of(invited["id"])
    assert membership.status is MembershipStatus.INVITED
    assert membership.decline_reason is None and membership.declined_at is None

    accepted = client.post(
        "/api/v1/auth/join/rules/accept",
        json={"setup_token": rejoined.get_json()["setup_token"], "version": 1},
        headers=auth(world.joiner),
    )
    assert accepted.status_code == 200, accepted.get_data(as_text=True)
    done = client.post(
        "/api/v1/auth/join/pin",
        json={"setup_token": rejoined.get_json()["setup_token"], "pin": PIN},
        headers=auth(world.joiner),
    )
    assert done.status_code == 200, done.get_data(as_text=True)
    assert row_of(invited["id"]).status is MembershipStatus.ACTIVE
