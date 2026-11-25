"""Role and cross-SACCO matrix for the Move 2 endpoints (Neo's task 7).

Expected statuses come from docs/roles-permissions.md: the platform owner
manages SACCOs, a SACCO administrator is confined to their own SACCO, a member
has no management endpoints, and an anonymous caller gets 401.

Another SACCO's administrator gets 404, the same answer as a missing SACCO, so
the existence of a record is never revealed.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.auth.tokens import create_access_token
from app.extensions import db
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.models.sacco import Sacco
from app.models.user import User

PASSWORD_HASH = "not-a-real-hash"  # tokens are minted directly; no login needed

ROLES = ("anonymous", "member", "sacco_admin", "other_admin", "platform_owner")

# (method, path, body, {role: expected status})
MATRIX = [
    ("get", "/api/v1/saccos", None, {
        "anonymous": 401, "member": 403, "sacco_admin": 403,
        "other_admin": 403, "platform_owner": 200,
    }),
    ("post", "/api/v1/saccos", {"name": "New SACCO"}, {
        "anonymous": 401, "member": 403, "sacco_admin": 403,
        "other_admin": 403, "platform_owner": 201,
    }),
    ("post", "/api/v1/saccos/{sacco}/activate", None, {
        "anonymous": 401, "member": 403, "sacco_admin": 403,
        "other_admin": 403, "platform_owner": 200,
    }),
    ("post", "/api/v1/saccos/{sacco}/deactivate", None, {
        "anonymous": 401, "member": 403, "sacco_admin": 403,
        "other_admin": 403, "platform_owner": 200,
    }),
    ("get", "/api/v1/saccos/{sacco}", None, {
        "anonymous": 401, "member": 200, "sacco_admin": 200,
        "other_admin": 404, "platform_owner": 200,
    }),
    ("patch", "/api/v1/saccos/{sacco}", {"name": "Renamed SACCO"}, {
        "anonymous": 401, "member": 403, "sacco_admin": 200,
        "other_admin": 404, "platform_owner": 200,
    }),
    ("get", "/api/v1/saccos/{sacco}/members", None, {
        "anonymous": 401, "member": 403, "sacco_admin": 200,
        "other_admin": 404, "platform_owner": 403,
    }),
    ("post", "/api/v1/saccos/{sacco}/members", {"email": "new@example.com"}, {
        "anonymous": 401, "member": 403, "sacco_admin": 201,
        "other_admin": 404, "platform_owner": 403,
    }),
    ("get", "/api/v1/saccos/{sacco}/members/{membership}", None, {
        "anonymous": 401, "member": 403, "sacco_admin": 200,
        "other_admin": 404, "platform_owner": 403,
    }),
    ("patch", "/api/v1/saccos/{sacco}/members/{membership}", {"role": "staff"}, {
        "anonymous": 401, "member": 403, "sacco_admin": 200,
        "other_admin": 404, "platform_owner": 403,
    }),
]

MATRIX_IDS = [f"{method}-{path}" for method, path, _, _ in MATRIX]


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


def make_membership(user, sacco, role=MembershipRole.MEMBER):
    membership = SaccoMembership(
        user_id=user.id, sacco_id=sacco.id, role=role, status=MembershipStatus.ACTIVE
    )
    db.session.add(membership)
    db.session.commit()
    return membership


def make_sacco(name):
    sacco = Sacco(name=name)
    db.session.add(sacco)
    db.session.commit()
    return sacco


@dataclass
class World:
    headers: dict
    sacco: Sacco
    other_sacco: Sacco
    member_membership: SaccoMembership
    other_membership: SaccoMembership


@pytest.fixture
def world(app):
    sacco = make_sacco("Umoja SACCO")
    other_sacco = make_sacco("Harambee SACCO")

    owner = make_user("owner@example.com", owner=True)
    admin = make_user("admin@example.com")
    make_membership(admin, sacco, MembershipRole.SACCO_ADMIN)
    other_admin = make_user("other-admin@example.com")
    other_membership = make_membership(
        other_admin, other_sacco, MembershipRole.SACCO_ADMIN
    )
    member = make_user("member@example.com")
    member_membership = make_membership(member, sacco)
    make_user("new@example.com")

    def headers(user):
        token = create_access_token(user.id, user.token_version)
        return {"Authorization": f"Bearer {token}"}

    return World(
        headers={
            "anonymous": {},
            "member": headers(member),
            "sacco_admin": headers(admin),
            "other_admin": headers(other_admin),
            "platform_owner": headers(owner),
        },
        sacco=sacco,
        other_sacco=other_sacco,
        member_membership=member_membership,
        other_membership=other_membership,
    )


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("method,path,body,expected", MATRIX, ids=MATRIX_IDS)
def test_role_matrix(client, world, role, method, path, body, expected):
    url = path.format(sacco=world.sacco.id, membership=world.member_membership.id)
    kwargs = {"headers": world.headers[role]}
    if body is not None:
        kwargs["json"] = body
    resp = getattr(client, method)(url, **kwargs)
    assert resp.status_code == expected[role], resp.get_json()


# ---------- cross-SACCO isolation ----------


def test_the_other_saccos_administrator_cannot_reach_members_or_a_membership(
    client, world
):
    headers = world.headers["other_admin"]
    resp = client.get(f"/api/v1/saccos/{world.sacco.id}/members", headers=headers)
    assert resp.status_code == 404
    assert "member@example.com" not in resp.get_data(as_text=True)

    resp = client.get(
        f"/api/v1/saccos/{world.sacco.id}/members/{world.member_membership.id}",
        headers=headers,
    )
    assert resp.status_code == 404
    # Same answer and body as a SACCO that does not exist.
    assert resp.get_json() == client.get(
        "/api/v1/saccos/999999/members", headers=headers
    ).get_json()


def test_a_listing_never_contains_another_saccos_members(client, world):
    body = client.get(
        f"/api/v1/saccos/{world.sacco.id}/members",
        headers=world.headers["sacco_admin"],
    ).get_json()
    emails = {m["email"] for m in body["members"]}
    assert emails == {"admin@example.com", "member@example.com"}


def test_a_member_can_change_only_their_own_profile(client, world):
    headers = world.headers["member"]
    assert client.get("/api/v1/members/me", headers=headers).status_code == 200
    # There is no route that takes a member id, so nobody reaches another profile.
    assert (
        client.get(
            f"/api/v1/members/{world.member_membership.id}", headers=headers
        ).status_code
        == 404
    )
    assert client.get("/api/v1/members/me").status_code == 401


def test_me_never_returns_another_saccos_records(client, world):
    body = client.get(
        "/api/v1/members/me", headers=world.headers["other_admin"]
    ).get_json()
    assert [m["sacco_id"] for m in body["memberships"]] == [world.other_sacco.id]
    assert "member@example.com" not in str(body)
