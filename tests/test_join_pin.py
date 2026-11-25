"""Section 2: invite, join and SACCO PIN (the five done-when cases)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.auth.tokens import create_access_token
from app.extensions import db
from app.models.identity import JoinCode, UserPhone
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.models.sacco import Sacco
from app.models.user import User

PHONE = "+254700000001"
PIN = "1234"
BAD_PIN = "9999"


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
    joiner = make_user("joiner@example.com")
    other = make_user("other@example.com")
    return SimpleNamespace(sacco=sacco, admin=admin, joiner=joiner, other=other)


def invite(client, world, phone=PHONE):
    resp = client.post(
        f"/api/v1/saccos/{world.sacco.id}/members",
        json={"phone": phone},
        headers=auth(world.admin),
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()


def join(client, user, code, phone=PHONE):
    return client.post(
        "/api/v1/auth/join",
        json={"phone": phone, "code": code},
        headers=auth(user),
    )


def set_pin(client, user, token, pin=PIN):
    return client.post(
        "/api/v1/auth/join/pin",
        json={"setup_token": token, "pin": pin},
        headers=auth(user),
    )


def sacco_login(client, user, world, pin):
    return client.post(
        "/api/v1/auth/sacco-login",
        json={"sacco_id": world.sacco.id, "pin": pin},
        headers=auth(user),
    )


def row_of(membership_id):
    db.session.expire_all()
    return db.session.get(SaccoMembership, membership_id)


def wrong_code(code):
    return "000000" if code != "000000" else "111111"


def activate(client, world):
    body = invite(client, world)
    resp = join(client, world.joiner, body["join_code"])
    assert resp.status_code == 200, resp.get_data(as_text=True)
    token = resp.get_json()["setup_token"]
    assert set_pin(client, world.joiner, token).status_code == 200
    return body["id"]


def test_invite_creates_an_invited_membership_and_shows_a_code(client, world):
    body = invite(client, world)
    assert body["status"] == "invited"
    assert len(body["join_code"]) == 6 and body["join_code"].isdigit()
    membership = row_of(body["id"])
    assert membership.user_id is None
    assert membership.status is MembershipStatus.INVITED


def test_reissuing_an_invite_burns_the_old_code_and_accepts_the_new_one(client, world):
    first = invite(client, world)
    resent = client.post(
        f"/api/v1/saccos/{world.sacco.id}/members",
        json={"phone": PHONE, "role": "member"},
        headers=auth(world.admin),
    )
    assert resent.status_code == 200, resent.get_data(as_text=True)
    replacement = resent.get_json()
    assert replacement["id"] == first["id"]
    assert replacement["join_code"] != first["join_code"]

    old_code = join(client, world.joiner, first["join_code"])
    assert old_code.status_code == 400
    assert "CODE_WRONG" in old_code.get_data(as_text=True)
    assert join(client, world.joiner, replacement["join_code"]).status_code == 200


def test_invited_goes_to_active(client, world):
    body = invite(client, world)
    resp = join(client, world.joiner, body["join_code"])
    assert resp.status_code == 200
    # Linked, but not active until the PIN is set.
    membership = row_of(body["id"])
    assert membership.user_id == world.joiner.id
    assert membership.status is MembershipStatus.INVITED
    phone = db.session.execute(
        select(UserPhone).where(UserPhone.phone == PHONE)
    ).scalar_one()
    assert phone.user_id == world.joiner.id
    resp = set_pin(client, world.joiner, resp.get_json()["setup_token"])
    assert resp.status_code == 200
    membership = row_of(body["id"])
    assert membership.status is MembershipStatus.ACTIVE
    assert membership.pin_hash and membership.pin_hash != PIN
    assert sacco_login(client, world.joiner, world, PIN).status_code == 200


def test_a_used_code_fails(client, world):
    body = invite(client, world)
    assert join(client, world.joiner, body["join_code"]).status_code == 200
    resp = join(client, world.other, body["join_code"])
    assert resp.status_code == 409
    assert "CODE_USED" in resp.get_data(as_text=True)


def test_an_expired_code_fails(client, world):
    body = invite(client, world)
    row = db.session.execute(
        select(JoinCode).where(JoinCode.membership_id == body["id"])
    ).scalar_one()
    now = datetime.now(UTC)
    row.created_at = now - timedelta(hours=2)
    row.expires_at = now - timedelta(hours=1)
    db.session.commit()
    resp = join(client, world.joiner, body["join_code"])
    assert resp.status_code == 410
    assert "CODE_EXPIRED" in resp.get_data(as_text=True)
    assert row_of(body["id"]).user_id is None


def test_five_wrong_codes_burn_the_code(client, world):
    body = invite(client, world)
    bad = wrong_code(body["join_code"])
    for _ in range(5):
        resp = join(client, world.joiner, bad)
        assert resp.status_code == 400
        assert "CODE_WRONG" in resp.get_data(as_text=True)
    # Even the right code no longer works.
    resp = join(client, world.joiner, body["join_code"])
    assert resp.status_code == 409
    assert "CODE_USED" in resp.get_data(as_text=True)
    membership = row_of(body["id"])
    assert membership.user_id is None
    assert membership.status is MembershipStatus.INVITED


def test_five_wrong_pins_lock_the_pin(client, world):
    activate(client, world)
    for _ in range(4):
        resp = sacco_login(client, world.joiner, world, BAD_PIN)
        assert resp.status_code == 401
        assert "PIN_WRONG" in resp.get_data(as_text=True)
    resp = sacco_login(client, world.joiner, world, BAD_PIN)
    assert resp.status_code == 423
    assert "PIN_LOCKED" in resp.get_data(as_text=True)
    # Locked: even the right PIN is refused.
    resp = sacco_login(client, world.joiner, world, PIN)
    assert resp.status_code == 423
    assert "PIN_LOCKED" in resp.get_data(as_text=True)


def test_sacco_login_is_refused_while_still_invited(client, world):
    body = invite(client, world)
    assert join(client, world.joiner, body["join_code"]).status_code == 200
    assert sacco_login(client, world.joiner, world, PIN).status_code == 403
