"""Add a member by email: the join code is emailed, not shown to the officer."""

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


def make_user(email, verified=True):
    user = User(
        email=email,
        password_hash="not-a-real-hash",
        email_verified_at=datetime.now(UTC) if verified else None,
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
    return SimpleNamespace(
        sacco=sacco,
        admin=admin,
        joiner=make_user("joiner@example.com"),
        other=make_user("other@example.com"),
    )


def invite_by_email(client, world, email="joiner@example.com", role="member", by=None):
    return client.post(
        f"/api/v1/saccos/{world.sacco.id}/members",
        json={"email": email, "role": role, "invite": True},
        headers=auth(by or world.admin),
    )


def join(client, user, code):
    return client.post("/api/v1/auth/join", json={"code": code}, headers=auth(user))


def row_of(membership_id):
    db.session.expire_all()
    return db.session.get(SaccoMembership, membership_id)


def test_the_code_is_emailed_and_never_shown_to_the_officer(client, world, sent):
    resp = invite_by_email(client, world)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["status"] == "invited" and body["email"] == "joiner@example.com"
    assert "join_code" not in body and "join_code" not in resp.get_data(as_text=True)
    assert len(sent) == 1
    to, code, sacco_name, minutes = sent[0]
    assert to == "joiner@example.com" and sacco_name == "Umoja SACCO"
    assert len(code) == 6 and code.isdigit() and minutes == 15
    membership = row_of(body["id"])
    assert membership.user_id == world.joiner.id and membership.phone is None
    assert membership.status is MembershipStatus.INVITED


def test_the_emailed_code_joins_without_a_phone_and_the_pin_activates(
    client, world, sent
):
    body = invite_by_email(client, world).get_json()
    code = sent[0][1]
    joined = join(client, world.joiner, code)
    assert joined.status_code == 200, joined.get_data(as_text=True)
    # Linked already, but not active until the PIN is set.
    assert row_of(body["id"]).status is MembershipStatus.INVITED
    done = client.post(
        "/api/v1/auth/join/pin",
        json={"setup_token": joined.get_json()["setup_token"], "pin": PIN},
        headers=auth(world.joiner),
    )
    assert done.status_code == 200, done.get_data(as_text=True)
    assert row_of(body["id"]).status is MembershipStatus.ACTIVE
    entered = client.post(
        "/api/v1/auth/sacco-login",
        json={"sacco_id": world.sacco.id, "pin": PIN},
        headers=auth(world.joiner),
    )
    assert entered.status_code == 200


def test_someone_else_cannot_use_the_emailed_code(client, world, sent):
    invite_by_email(client, world)
    code = sent[0][1]
    stolen = join(client, world.other, code)
    assert stolen.status_code == 400
    assert "CODE_WRONG" in stolen.get_data(as_text=True)
    assert row_of(1) is not None  # the invitation is still there, and still theirs
    assert join(client, world.joiner, code).status_code == 200


def test_five_wrong_codes_burn_the_emailed_code(client, world, sent):
    invite_by_email(client, world)
    code = sent[0][1]
    bad = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert join(client, world.joiner, bad).status_code == 400
    burned = join(client, world.joiner, code)
    assert burned.status_code == 409
    assert "CODE_USED" in burned.get_data(as_text=True)


def test_inviting_again_emails_a_new_code_and_kills_the_old_one(client, world, sent):
    first = invite_by_email(client, world)
    again = invite_by_email(client, world)
    assert first.status_code == 201 and again.status_code == 200
    assert first.get_json()["id"] == again.get_json()["id"]
    assert len(sent) == 2 and sent[0][1] != sent[1][1]
    old = join(client, world.joiner, sent[0][1])
    assert old.status_code == 400 and "CODE_WRONG" in old.get_data(as_text=True)
    assert join(client, world.joiner, sent[1][1]).status_code == 200
    rows = db.session.execute(select(JoinCode)).scalars().all()
    assert len(rows) == 2


def test_joining_with_no_invitation_answers_like_a_wrong_code(client, world, sent):
    resp = join(client, world.joiner, "123456")
    assert resp.status_code == 400
    assert "CODE_WRONG" in resp.get_data(as_text=True)


def test_unknown_unverified_and_existing_members_are_refused(client, world, sent):
    assert invite_by_email(client, world, email="nobody@example.com").status_code == 404
    make_user("pending@example.com", verified=False)
    assert (
        invite_by_email(client, world, email="pending@example.com").status_code == 409
    )
    already = invite_by_email(client, world, email="admin@example.com")
    assert already.status_code == 409
    bad_role = invite_by_email(client, world, role="president")
    assert bad_role.status_code == 400
    assert sent == []


def test_only_someone_who_can_manage_members_can_invite(client, world, sent):
    assert invite_by_email(client, world, by=world.other).status_code in (403, 404)
    assert sent == []


def test_the_plain_email_add_still_cannot_be_invited(client, world, sent):
    resp = client.post(
        f"/api/v1/saccos/{world.sacco.id}/members",
        json={"email": "joiner@example.com", "status": "invited"},
        headers=auth(world.admin),
    )
    assert resp.status_code == 400
    assert sent == []
