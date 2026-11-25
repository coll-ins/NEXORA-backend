"""Inviting a phone with a job title (Neo, N1b).

`POST /saccos/<id>/members {phone, role}` accepts the legacy role values
(`sacco_admin`, `staff`, `member`), the owner-only `sacco_admin`, and the job
titles the Add a member page offers. `can()` reads titles, so the invite must
grant the title that was asked for instead of always `secretary`.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.auth.tokens import create_access_token
from app.extensions import db
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.models.sacco import Sacco
from app.models.user import User
from app.security.permissions import _role_codes

PASSWORD_HASH = "not-a-real-hash"  # these tests mint tokens; they never log in
PHONE = "+254700000123"


def _truncate():
    db.session.remove()
    db.session.execute(text("TRUNCATE users, saccos RESTART IDENTITY CASCADE"))
    db.session.commit()


@pytest.fixture(autouse=True)
def clean_tables(app):
    _truncate()
    yield
    _truncate()


def make_user(email, owner=False):
    user = User(
        email=email,
        password_hash=PASSWORD_HASH,
        is_platform_owner=owner,
        email_verified_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


def make_sacco(name):
    sacco = Sacco(name=name)
    db.session.add(sacco)
    db.session.commit()
    return sacco


def make_membership(user, sacco, role=MembershipRole.MEMBER):
    membership = SaccoMembership(
        user_id=user.id, sacco_id=sacco.id, role=role, status=MembershipStatus.ACTIVE
    )
    db.session.add(membership)
    db.session.commit()
    return membership


def auth(user):
    token = create_access_token(user.id, user.token_version)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(app):
    owner = make_user("owner@example.com", owner=True)
    sacco = make_sacco("Umoja SACCO")
    other_sacco = make_sacco("Harambee SACCO")
    admin = make_user("admin@example.com")
    make_membership(admin, sacco, MembershipRole.SACCO_ADMIN)
    staff_admin = make_user("staff-admin@example.com")
    make_membership(staff_admin, other_sacco, MembershipRole.SACCO_ADMIN)
    return {
        "owner": owner,
        "sacco": sacco,
        "admin": admin,
        "other_admin": staff_admin,
    }


def invite(client, world, user, role, phone=PHONE):
    return client.post(
        f"/api/v1/saccos/{world['sacco'].id}/members",
        json={"phone": phone, "role": role},
        headers=auth(user),
    )


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("secretary", MembershipRole.STAFF),
        ("finance_officer", MembershipRole.STAFF),
        ("loans_officer", MembershipRole.STAFF),
        ("member", MembershipRole.MEMBER),
    ],
)
def test_an_invite_grants_the_title_that_was_asked_for(client, world, title, role):
    resp = invite(client, world, world["admin"], title)

    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["title"] == title
    assert body["role"] == role.value
    membership = db.session.get(SaccoMembership, body["id"])
    assert membership.role is role
    assert _role_codes(membership) == {title}


def test_the_legacy_staff_value_still_means_a_secretary(client, world):
    resp = invite(client, world, world["admin"], "staff")

    assert resp.status_code == 201, resp.get_data(as_text=True)
    assert resp.get_json()["title"] == "secretary"


def test_reinviting_a_pending_phone_updates_the_existing_invite_title(client, world):
    first = invite(client, world, world["admin"], "member")
    resent = invite(client, world, world["admin"], "finance_officer")

    assert first.status_code == 201
    assert resent.status_code == 200
    body = resent.get_json()
    assert body["id"] == first.get_json()["id"]
    assert body["title"] == "finance_officer"
    assert body["role"] == MembershipRole.STAFF.value
    membership = db.session.get(SaccoMembership, body["id"])
    assert _role_codes(membership) == {"finance_officer"}


def test_only_the_platform_owner_invites_a_sacco_admin(client, world):
    by_admin = invite(client, world, world["admin"], "sacco_admin")
    assert by_admin.status_code == 403
    assert "Only the platform owner" in by_admin.get_json()["error"]["message"]

    by_owner = invite(client, world, world["owner"], "sacco_admin")
    assert by_owner.status_code == 201, by_owner.get_data(as_text=True)
    body = by_owner.get_json()
    assert body["role"] == MembershipRole.SACCO_ADMIN.value
    membership = db.session.get(SaccoMembership, body["id"])
    assert _role_codes(membership) == {"sacco_admin"}


def test_an_unknown_role_is_refused(client, world):
    resp = invite(client, world, world["admin"], "wizard")

    assert resp.status_code == 400
    assert resp.get_json()["error"]["message"] == "A valid phone and role are required."
