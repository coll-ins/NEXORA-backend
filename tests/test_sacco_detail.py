"""GET and PATCH /api/v1/saccos/<id> and the tenant guard behind them."""

import itertools
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.auth.suspension import SUSPENDED_MESSAGE
from app.auth.tokens import create_access_token, create_refresh_token
from app.extensions import db
from app.models.audit import AuditLog
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.models.sacco import Sacco, SaccoStatus
from app.models.user import User

BASE = "/api/v1/saccos"
_n = itertools.count(1)


def _truncate():
    db.session.remove()
    db.session.execute(
        text("TRUNCATE refresh_tokens, users, saccos RESTART IDENTITY CASCADE")
    )
    db.session.commit()


@pytest.fixture(autouse=True)
def clean_tables(app):
    _truncate()
    yield
    _truncate()


def make_sacco(name=None, status=SaccoStatus.ACTIVE):
    sacco = Sacco(name=name or f"Sacco {next(_n)}", status=status)
    db.session.add(sacco)
    db.session.commit()
    return sacco.id


def make_user(owner=False):
    user = User(
        email=f"user{next(_n)}@example.com", password_hash="x", is_platform_owner=owner
    )
    db.session.add(user)
    db.session.commit()
    return user.id


def join(user_id, sacco_id, role, status=MembershipStatus.ACTIVE):
    db.session.add(
        SaccoMembership(user_id=user_id, sacco_id=sacco_id, role=role, status=status)
    )
    db.session.commit()


def auth(user_id):
    return {"Authorization": f"Bearer {create_access_token(user_id, 0)}"}


def person(sacco_id, role, status=MembershipStatus.ACTIVE):
    """A user with this role in this SACCO; returns their auth header."""
    user_id = make_user()
    join(user_id, sacco_id, role, status)
    return auth(user_id)


ADMIN, STAFF, MEMBER = (
    MembershipRole.SACCO_ADMIN,
    MembershipRole.STAFF,
    MembershipRole.MEMBER,
)


def name_of(sacco_id):
    db.session.expire_all()
    return db.session.get(Sacco, sacco_id).name


def audit_rows():
    db.session.expire_all()
    return db.session.query(AuditLog).order_by(AuditLog.id).all()


# ---------- authentication ----------


@pytest.mark.parametrize("method", ["get", "patch"])
def test_no_token_is_401(client, method):
    sacco_id = make_sacco()
    assert getattr(client, method)(f"{BASE}/{sacco_id}").status_code == 401


@pytest.mark.parametrize("method", ["get", "patch"])
def test_garbage_token_is_401(client, method):
    sacco_id = make_sacco()
    resp = getattr(client, method)(
        f"{BASE}/{sacco_id}", headers={"Authorization": "Bearer nonsense"}
    )
    assert resp.status_code == 401


def test_refresh_token_is_not_an_access_token(client):
    sacco_id = make_sacco()
    user_id = make_user()
    join(user_id, sacco_id, ADMIN)
    headers = {"Authorization": f"Bearer {create_refresh_token(user_id)}"}
    assert client.get(f"{BASE}/{sacco_id}", headers=headers).status_code == 401


def test_a_valid_token_for_a_user_that_no_longer_exists_is_401(client):
    sacco_id = make_sacco()
    headers = auth(9999)
    assert client.get(f"{BASE}/{sacco_id}", headers=headers).status_code == 401


# ---------- GET /saccos/<id> ----------


def test_admin_reads_their_own_sacco(client):
    sacco_id = make_sacco("Umoja")
    resp = client.get(f"{BASE}/{sacco_id}", headers=person(sacco_id, ADMIN))
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Umoja"
    assert set(resp.get_json()) == {
        "id",
        "name",
        "currency",
        "status",
        "member_count",
        "created_at",
        "registration_number",
        "sasra_licence_number",
        "registration_date",
    }
    assert resp.get_json()["member_count"] == 1  # the admin above
    assert resp.headers["Cache-Control"] == "no-store"


def test_member_count_ignores_a_deactivated_member(client):
    sacco_id = make_sacco("Umoja")
    admin_id = make_user()
    join(admin_id, sacco_id, ADMIN)
    gone_id = make_user()
    join(gone_id, sacco_id, MembershipRole.MEMBER, status=MembershipStatus.INACTIVE)

    body = client.get(f"{BASE}/{sacco_id}", headers=auth(admin_id)).get_json()
    assert body["member_count"] == 1


def test_platform_owner_reads_any_sacco(client):
    sacco_id = make_sacco("Umoja")
    owner = auth(make_user(owner=True))
    assert client.get(f"{BASE}/{sacco_id}", headers=owner).status_code == 200


def test_another_saccos_admin_gets_the_same_404_as_a_missing_sacco(client):
    mine, theirs = make_sacco("Mine"), make_sacco("Theirs")
    headers = person(mine, ADMIN)
    cross = client.get(f"{BASE}/{theirs}", headers=headers)
    missing = client.get(f"{BASE}/9999", headers=headers)
    assert cross.status_code == missing.status_code == 404
    assert cross.get_json() == missing.get_json()  # existence is never revealed
    assert "Theirs" not in cross.get_data(as_text=True)


def test_404s_for_other_and_missing_saccos_have_identical_headers(client):
    mine, theirs = make_sacco("Mine"), make_sacco("Theirs")
    headers = person(mine, ADMIN)
    cross = client.get(f"{BASE}/{theirs}", headers=headers)
    missing = client.get(f"{BASE}/9999", headers=headers)
    assert dict(cross.headers) == dict(missing.headers)


@pytest.mark.parametrize("sacco_id", [2**31, 2**31 + 1, 99999999999, "9" * 40, 0])
@pytest.mark.parametrize("who", ["owner", "admin", "outsider"])
@pytest.mark.parametrize("method", ["get", "patch"])
def test_ids_outside_the_database_range_are_a_clean_404_not_a_500(
    client, method, who, sacco_id
):
    own = make_sacco("Own")
    headers = {
        "owner": lambda: auth(make_user(owner=True)),
        "admin": lambda: person(own, ADMIN),
        "outsider": lambda: auth(make_user()),
    }[who]()
    resp = getattr(client, method)(
        f"{BASE}/{sacco_id}", headers=headers, json={"name": "X"}
    )
    assert resp.status_code == 404
    assert resp.get_json()["error"]["message"] == "Not found."


def test_missing_sacco_is_404_for_the_platform_owner(client):
    owner = auth(make_user(owner=True))
    assert client.get(f"{BASE}/9999", headers=owner).status_code == 404


def test_user_with_no_membership_is_404(client):
    sacco_id = make_sacco()
    resp = client.get(f"{BASE}/{sacco_id}", headers=auth(make_user()))
    assert resp.status_code == 404


@pytest.mark.parametrize("role", [STAFF, MEMBER])
def test_staff_and_members_read_their_sacco_but_cannot_rename_it(client, role):
    # Every active member holds sacco.view; renaming needs sacco.update (admin).
    sacco_id = make_sacco()
    headers = person(sacco_id, role)
    assert client.get(f"{BASE}/{sacco_id}", headers=headers).status_code == 200
    renamed = client.patch(f"{BASE}/{sacco_id}", json={"name": "X"}, headers=headers)
    assert renamed.status_code == 403


def test_inactive_admin_is_403(client):
    sacco_id = make_sacco()
    headers = person(sacco_id, ADMIN, MembershipStatus.INACTIVE)
    assert client.get(f"{BASE}/{sacco_id}", headers=headers).status_code == 403


def test_admin_of_two_saccos_is_still_confined_to_the_one_they_administer(client):
    a, b = make_sacco("A"), make_sacco("B")
    user_id = make_user()
    join(user_id, a, ADMIN)
    join(user_id, b, MEMBER)
    headers = auth(user_id)
    assert (
        client.patch(f"{BASE}/{a}", json={"name": "A2"}, headers=headers).status_code
        == 200
    )
    # Admin powers stay in A: in B (a plain member) renaming is refused.
    assert (
        client.patch(f"{BASE}/{b}", json={"name": "B2"}, headers=headers).status_code
        == 403
    )
    assert client.get(f"{BASE}/{make_sacco('C')}", headers=headers).status_code == 404


def test_suspended_sacco_blocks_its_admin_but_not_the_platform_owner(client):
    sacco_id = make_sacco(status=SaccoStatus.SUSPENDED)
    resp = client.get(f"{BASE}/{sacco_id}", headers=person(sacco_id, ADMIN))
    assert resp.status_code == 423
    assert resp.get_json()["error"]["message"] == SUSPENDED_MESSAGE
    owner = auth(make_user(owner=True))
    assert client.get(f"{BASE}/{sacco_id}", headers=owner).status_code == 200


def test_owner_status_is_read_from_the_database_not_the_token(client):
    sacco_id = make_sacco()
    owner_id = make_user(owner=True)
    headers = auth(owner_id)
    assert client.get(f"{BASE}/{sacco_id}", headers=headers).status_code == 200
    db.session.get(User, owner_id).is_platform_owner = False
    db.session.commit()
    # Same token, but the user is no longer an owner and has no membership.
    assert client.get(f"{BASE}/{sacco_id}", headers=headers).status_code == 404


# ---------- PATCH /saccos/<id> ----------


def test_admin_renames_their_own_sacco_and_it_is_audited(client):
    sacco_id = make_sacco("Old Name")
    admin_id = make_user()
    join(admin_id, sacco_id, ADMIN)
    resp = client.patch(
        f"{BASE}/{sacco_id}", headers=auth(admin_id), json={"name": "  New Name  "}
    )
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "New Name"
    assert name_of(sacco_id) == "New Name"
    (row,) = audit_rows()
    assert (row.action, row.actor_id, row.sacco_id) == (
        "sacco.rename",
        admin_id,
        sacco_id,
    )
    assert f"sacco:{sacco_id}" in row.target


def test_platform_owner_renames_any_sacco(client):
    sacco_id = make_sacco("Old Name")
    owner_id = make_user(owner=True)
    resp = client.patch(
        f"{BASE}/{sacco_id}", headers=auth(owner_id), json={"name": "By Owner"}
    )
    assert resp.status_code == 200
    assert name_of(sacco_id) == "By Owner"
    assert audit_rows()[0].actor_id == owner_id


def test_renaming_to_the_same_name_changes_and_logs_nothing(client):
    sacco_id = make_sacco("Same")
    resp = client.patch(
        f"{BASE}/{sacco_id}", headers=person(sacco_id, ADMIN), json={"name": " Same "}
    )
    assert resp.status_code == 200
    assert audit_rows() == []


def test_duplicate_name_in_any_case_is_409_and_changes_nothing(client):
    make_sacco("Taken")
    sacco_id = make_sacco("Mine")
    resp = client.patch(
        f"{BASE}/{sacco_id}", headers=person(sacco_id, ADMIN), json={"name": " TAKEN "}
    )
    assert resp.status_code == 409
    assert name_of(sacco_id) == "Mine"
    assert audit_rows() == []  # the rename and its audit row fail together


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": 123},
        {"name": None},
        {"name": "x" * 256},
        {"name": "a\x00b"},
        {"name": "Ok", "status": "active"},
        {"name": "Ok", "currency": "USD"},
        {"name": "Ok", "id": 99},
        {"status": "active"},
        {"currency": "USD"},
    ],
)
def test_bad_or_forbidden_fields_are_400(client, payload):
    sacco_id = make_sacco("Keep")
    resp = client.patch(
        f"{BASE}/{sacco_id}", headers=person(sacco_id, ADMIN), json=payload
    )
    assert resp.status_code == 400
    assert name_of(sacco_id) == "Keep"
    assert audit_rows() == []


def test_a_body_that_is_not_json_is_400(client):
    sacco_id = make_sacco()
    resp = client.patch(
        f"{BASE}/{sacco_id}",
        headers=person(sacco_id, ADMIN),
        data="nope",
        content_type="text/plain",
    )
    assert resp.status_code == 400


def test_admin_cannot_change_status_or_currency_through_patch(client):
    sacco_id = make_sacco("Keep")
    client.patch(
        f"{BASE}/{sacco_id}",
        headers=person(sacco_id, ADMIN),
        json={"name": "Keep", "status": "suspended", "currency": "USD"},
    )
    db.session.expire_all()
    sacco = db.session.get(Sacco, sacco_id)
    assert (sacco.status, sacco.currency) == (SaccoStatus.ACTIVE, "KES")


def test_another_saccos_admin_cannot_rename_it(client):
    mine, theirs = make_sacco("Mine"), make_sacco("Theirs")
    resp = client.patch(
        f"{BASE}/{theirs}", headers=person(mine, ADMIN), json={"name": "Hijacked"}
    )
    assert resp.status_code == 404
    assert name_of(theirs) == "Theirs"
    assert name_of(mine) == "Mine"
    assert audit_rows() == []


@pytest.mark.parametrize("role", [STAFF, MEMBER])
def test_staff_and_members_cannot_rename(client, role):
    sacco_id = make_sacco("Keep")
    resp = client.patch(
        f"{BASE}/{sacco_id}", headers=person(sacco_id, role), json={"name": "Nope"}
    )
    assert resp.status_code == 403
    assert name_of(sacco_id) == "Keep"


def test_suspended_sacco_admin_cannot_rename(client):
    sacco_id = make_sacco("Keep", status=SaccoStatus.SUSPENDED)
    resp = client.patch(
        f"{BASE}/{sacco_id}", headers=person(sacco_id, ADMIN), json={"name": "Nope"}
    )
    assert resp.status_code == 423
    assert name_of(sacco_id) == "Keep"


def test_patch_with_a_non_numeric_id_is_404(client):
    headers = auth(make_user(owner=True))
    resp = client.patch(f"{BASE}/abc", headers=headers, json={"name": "X"})
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "name",
    ["\u200b", "Foo\u200b", "a\u2060b", "\ufeffFoo", "tab\there", "new\nline", "\x1f"],
)
def test_invisible_and_control_characters_in_a_name_are_400(client, name):
    sacco_id = make_sacco("Keep")
    resp = client.patch(
        f"{BASE}/{sacco_id}", headers=person(sacco_id, ADMIN), json={"name": name}
    )
    assert resp.status_code == 400
    assert name_of(sacco_id) == "Keep"
    assert audit_rows() == []


def test_ordinary_unicode_names_are_still_allowed(client):
    sacco_id = make_sacco("Keep")
    resp = client.patch(
        f"{BASE}/{sacco_id}",
        headers=person(sacco_id, ADMIN),
        json={"name": "Maendeleo Sacco ya Wakulima \u00e9"},
    )
    assert resp.status_code == 200


def test_concurrent_renames_leave_a_consistent_audit_trail(app):
    """Overlapping renames queue up on the row lock instead of racing."""
    sacco_id = make_sacco("Start")
    headers = person(sacco_id, ADMIN)
    names = ["Aaa", "Bbb", "Ccc", "Ddd"]

    def rename(new_name):
        with app.app_context():
            return (
                app.test_client()
                .patch(f"{BASE}/{sacco_id}", headers=headers, json={"name": new_name})
                .status_code
            )

    with ThreadPoolExecutor(len(names)) as pool:
        codes = list(pool.map(rename, names))

    assert codes == [200] * len(names)
    rows = audit_rows()
    assert len(rows) == len(names)  # every rename is logged
    replaced = [r.target.split(" from ")[1].strip("'") for r in rows]
    # Each rename replaced a different name, and each is the starting name or a
    # name one of the other renames set: a true chain, with no stale reads.
    assert len(set(replaced)) == len(names)
    assert set(replaced) <= {"Start", *names}
    assert name_of(sacco_id) in names
    assert name_of(sacco_id) not in replaced  # the final name replaced nothing
