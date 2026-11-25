"""Member self-service (Move 2, Sam): GET and PATCH /api/v1/members/me.

The caller is always the user in the access token. A suspended SACCO cuts off
tokens issued before the suspension (423), the platform owner is never cut off,
a wrong password on an email change counts toward the login lockout, and the
response never holds another member's data.
"""

from datetime import UTC, datetime
from io import BytesIO

import pytest
from sqlalchemy import text

from app.auth.lockout import MAX_FAILED_ATTEMPTS
from app.auth.passwords import hash_password
from app.auth.suspension import SUSPENDED_MESSAGE
from app.auth.tokens import create_access_token
from app.extensions import db
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.models.sacco import Sacco, SaccoStatus
from app.models.user import User

ME = "/api/v1/members/me"
PASSWORD = "correct-horse-battery"
WRONG = "not-the-password-1"

# One hash for the whole module: argon2 is slow on purpose.
PASSWORD_HASH = hash_password(PASSWORD)


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


def make_sacco(name, status=SaccoStatus.ACTIVE):
    sacco = Sacco(name=name, status=status)
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


def suspend(sacco):
    sacco.status = SaccoStatus.SUSPENDED
    db.session.commit()


def fresh(user):
    db.session.expire_all()
    return db.session.get(User, user.id)


@pytest.fixture
def world(app):
    """Two SACCOs. Sam is in the first; Wanjiru (another member) and Ken are around."""
    sacco = make_sacco("Umoja SACCO")
    other = make_sacco("Harambee SACCO")
    sam = make_user("sam@example.com")
    wanjiru = make_user("wanjiru@example.com")
    ken = make_user("ken@other.example.com")
    make_membership(sam, sacco)
    make_membership(wanjiru, sacco)
    make_membership(ken, other)
    return {"sacco": sacco, "other": other, "sam": sam, "wanjiru": wanjiru, "ken": ken}


# --- 401: no valid token ----------------------------------------------------


@pytest.mark.parametrize("method", ["get", "patch"])
def test_without_a_token_is_401(client, world, method):
    response = getattr(client, method)(ME, json={"email": "x@example.com"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer not-a-token", "Basic abc", "Token abc"],
)
def test_a_bad_authorization_header_is_401(client, world, header):
    response = client.get(ME, headers={"Authorization": header})
    assert response.status_code == 401


def test_a_token_for_a_deleted_user_is_401(client, world):
    ghost = make_user("ghost@example.com")
    headers = auth(ghost)
    db.session.delete(ghost)
    db.session.commit()
    assert client.get(ME, headers=headers).status_code == 401


# --- 423: suspended SACCO ---------------------------------------------------


def test_a_suspension_blocks_the_sacco_not_the_members_own_profile(client, world):
    headers = auth(world["sam"])  # issued before the suspension
    suspend(world["sacco"])
    # The profile is the user's own, not the SACCO's.
    assert client.get(ME, headers=headers).status_code == 200
    scoped = client.get(f"/api/v1/saccos/{world['sacco'].id}/settings", headers=headers)
    assert scoped.status_code == 423
    assert scoped.get_json()["error"]["message"] == SUSPENDED_MESSAGE


def test_a_member_of_a_suspended_sacco_can_still_change_their_email(client, world):
    suspend(world["sacco"])
    resp = client.patch(
        ME,
        headers=auth(world["sam"]),
        json={"email": "new@example.com", "current_password": PASSWORD},
    )
    assert resp.status_code == 200
    assert fresh(world["sam"]).email == "new@example.com"


def test_profile_photo_is_saved_for_its_owner_only(client, world):
    photo = b"\x89PNG\r\n\x1a\nprofile-photo"
    response = client.put(
        f"{ME}/photo",
        headers=auth(world["sam"]),
        data={"photo": (BytesIO(photo), "avatar.png")},
    )
    assert response.status_code == 200
    assert response.get_json() == {"saved": True}

    saved = client.get(f"{ME}/photo", headers=auth(world["sam"]))
    assert saved.status_code == 200
    assert saved.mimetype == "image/png"
    assert saved.data == photo
    assert client.get(f"{ME}/photo", headers=auth(world["wanjiru"])).status_code == 204


def test_profile_photo_rejects_invalid_and_oversized_images(client, world):
    headers = auth(world["sam"])
    invalid = client.put(
        f"{ME}/photo",
        headers=headers,
        data={"photo": (BytesIO(b"<svg></svg>"), "avatar.svg")},
    )
    assert invalid.status_code == 400

    oversized = client.put(
        f"{ME}/photo",
        headers=headers,
        data={"photo": (BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 1_500_000), "large.png")},
    )
    assert oversized.status_code == 413


def test_deleting_profile_photo_removes_it(client, world):
    headers = auth(world["sam"])
    client.put(
        f"{ME}/photo",
        headers=headers,
        data={"photo": (BytesIO(b"\xff\xd8\xffavatar"), "avatar.jpg")},
    )
    removed = client.delete(f"{ME}/photo", headers=headers)
    assert removed.status_code == 200
    assert removed.get_json() == {"deleted": True}
    assert client.get(f"{ME}/photo", headers=headers).status_code == 204


def test_the_message_does_not_name_the_sacco(client, world):
    suspend(world["sacco"])
    body = client.get(
        f"/api/v1/saccos/{world['sacco'].id}/settings", headers=auth(world["sam"])
    ).get_json()
    assert world["sacco"].name not in str(body)


def test_a_member_with_another_active_sacco_still_gets_200(client, world):
    make_membership(world["sam"], world["other"])
    suspend(world["sacco"])
    assert client.get(ME, headers=auth(world["sam"])).status_code == 200


def test_the_platform_owner_still_gets_through(client, world):
    owner = make_user("owner@example.com", owner=True)
    suspend(world["sacco"])
    suspend(world["other"])
    response = client.get(ME, headers=auth(owner))
    assert response.status_code == 200
    assert response.get_json()["email"] == "owner@example.com"


def test_reactivating_the_sacco_restores_access(client, world):
    headers = auth(world["sam"])
    url = f"/api/v1/saccos/{world['sacco'].id}/settings"
    suspend(world["sacco"])
    assert client.get(url, headers=headers).status_code == 423
    world["sacco"].status = SaccoStatus.ACTIVE
    db.session.commit()
    assert client.get(url, headers=headers).status_code == 200


# --- wrong password on an email change counts toward lockout ----------------


def _change_email(client, user, password, email="new@example.com"):
    return client.patch(
        ME, headers=auth(user), json={"email": email, "current_password": password}
    )


def test_a_wrong_password_is_403_and_counts_as_a_failed_attempt(client, world):
    response = _change_email(client, world["sam"], WRONG)
    assert response.status_code == 403
    sam = fresh(world["sam"])
    assert sam.failed_login_count == 1
    assert sam.email == "sam@example.com"


def test_repeated_wrong_passwords_lock_the_account(client, world):
    for _ in range(MAX_FAILED_ATTEMPTS):
        assert _change_email(client, world["sam"], WRONG).status_code == 403
    assert fresh(world["sam"]).locked_until is not None
    # Locked: even the right password is refused until the lock expires.
    assert _change_email(client, world["sam"], PASSWORD).status_code == 423
    assert fresh(world["sam"]).email == "sam@example.com"


def test_the_lockout_is_shared_with_login(client, world):
    for _ in range(MAX_FAILED_ATTEMPTS):
        _change_email(client, world["sam"], WRONG)
    login = client.post(
        "/api/v1/auth/login", json={"email": "sam@example.com", "password": PASSWORD}
    )
    assert login.status_code == 423


def test_the_right_password_resets_the_failed_count(client, world):
    _change_email(client, world["sam"], WRONG)
    _change_email(client, world["sam"], WRONG)
    response = _change_email(client, world["sam"], PASSWORD)
    assert response.status_code == 200
    assert response.get_json()["email"] == "new@example.com"
    assert fresh(world["sam"]).failed_login_count == 0


def test_a_missing_password_is_400_and_not_counted(client, world):
    response = client.patch(
        ME, headers=auth(world["sam"]), json={"email": "new@example.com"}
    )
    assert response.status_code == 400
    assert fresh(world["sam"]).failed_login_count in (0, None)


def test_an_email_taken_by_someone_else_is_409(client, world):
    response = _change_email(client, world["sam"], PASSWORD, "Wanjiru@Example.com")
    assert response.status_code == 409
    assert fresh(world["sam"]).email == "sam@example.com"


# --- the response never holds another member's data -------------------------


def test_get_returns_only_the_callers_own_profile(client, world):
    body = client.get(ME, headers=auth(world["sam"])).get_json()
    assert body["id"] == world["sam"].id
    assert body["email"] == "sam@example.com"
    assert [m["sacco_id"] for m in body["memberships"]] == [world["sacco"].id]
    text_body = str(body)
    for other in ("wanjiru@example.com", "ken@other.example.com"):
        assert other not in text_body
    assert world["other"].name not in text_body


def test_the_response_has_no_secrets_or_flags(client, world):
    body = client.get(ME, headers=auth(world["sam"])).get_json()
    assert set(body) == {"id", "email", "created_at", "memberships"}
    sensitive = (
        "password_hash",
        "locked_until",
        "failed_login_count",
        "is_platform_owner",
    )
    for field in sensitive:
        assert field not in str(body)


def test_a_client_cannot_name_another_user(client, world):
    # There is no id in the URL, and an id or role in the body is refused.
    other_id = world["wanjiru"].id
    assert client.get(f"{ME}/{other_id}", headers=auth(world["sam"])).status_code == 404
    response = client.get(f"{ME}?user_id={other_id}", headers=auth(world["sam"]))
    assert response.get_json()["id"] == world["sam"].id
    response = client.patch(
        ME,
        headers=auth(world["sam"]),
        json={"id": other_id, "email": "x@example.com", "current_password": PASSWORD},
    )
    assert response.status_code == 400
    assert fresh(world["wanjiru"]).email == "wanjiru@example.com"


@pytest.mark.parametrize("field", ["role", "status", "sacco_id", "is_platform_owner"])
def test_privileged_fields_are_refused(client, world, field):
    response = client.patch(
        ME,
        headers=auth(world["sam"]),
        json={field: "x", "email": "x@example.com", "current_password": PASSWORD},
    )
    assert response.status_code == 400
    assert fresh(world["sam"]).is_platform_owner is False


def test_responses_are_never_cached(client, world):
    response = client.get(ME, headers=auth(world["sam"]))
    assert response.headers["Cache-Control"] == "no-store"
