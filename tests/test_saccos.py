import pytest
from sqlalchemy import select, text

from app.auth.passwords import hash_password
from app.extensions import db
from app.models.audit import AuditLog
from app.models.membership import MembershipRole, SaccoMembership
from app.models.user import User

OWNER = "owner@example.com"
USER = "jane@example.com"
PASSWORD = "correct-horse-battery"
URL = "/api/v1/saccos"


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


def make_user(email, owner=False):
    db.session.add(
        User(
            email=email, password_hash=hash_password(PASSWORD), is_platform_owner=owner
        )
    )
    db.session.commit()


def login_body(client, email):
    payload = {"email": email, "password": PASSWORD}
    resp = client.post("/api/v1/auth/login", json=payload)
    return resp.get_json()


def bearer(client, email=OWNER):
    return {"Authorization": f"Bearer {login_body(client, email)['access_token']}"}


@pytest.fixture
def owner(client):
    make_user(OWNER, owner=True)
    return bearer(client)


# ---------- the guard ----------


@pytest.mark.parametrize("method", ["get", "post"])
def test_no_token_is_401(client, method):
    assert getattr(client, method)(URL).status_code == 401


def test_garbage_token_is_401(client):
    resp = client.get(URL, headers={"Authorization": "Bearer nonsense"})
    assert resp.status_code == 401


def test_refresh_token_is_not_accepted_as_access_token(client):
    make_user(OWNER, owner=True)
    refresh_token = login_body(client, OWNER)["refresh_token"]
    resp = client.get(URL, headers={"Authorization": f"Bearer {refresh_token}"})
    assert resp.status_code == 401


@pytest.mark.parametrize("method", ["get", "post"])
def test_ordinary_user_is_403(client, method):
    make_user(USER)
    headers = bearer(client, USER)
    resp = getattr(client, method)(URL, headers=headers, json={"name": "X"})
    assert resp.status_code == 403


def test_register_cannot_grant_platform_owner(client):
    client.post(
        "/api/v1/auth/register",
        json={"email": USER, "password": PASSWORD, "is_platform_owner": True},
    )
    assert db.session.query(User).one().is_platform_owner is False


# ---------- POST /saccos ----------


def test_owner_creates_sacco_with_defaults(client, owner):
    resp = client.post(URL, headers=owner, json={"name": "  Umoja SACCO  "})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["name"] == "Umoja SACCO"
    assert body["currency"] == "KES"
    assert body["status"] == "active"


def test_currency_is_normalised(client, owner):
    resp = client.post(URL, headers=owner, json={"name": "A", "currency": "usd"})
    assert resp.get_json()["currency"] == "USD"


def test_duplicate_name_in_any_case_is_409(client, owner):
    assert client.post(URL, headers=owner, json={"name": "Umoja"}).status_code == 201
    assert client.post(URL, headers=owner, json={"name": " UMOJA "}).status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": 123},
        {"name": "x" * 256},
        {"name": "a\x00b"},
        {"name": "Ok", "currency": "KE"},
        {"name": "Ok", "currency": "KESH"},
        {"name": "Ok", "currency": 5},
    ],
)
def test_bad_input_is_400(client, owner, payload):
    assert client.post(URL, headers=owner, json=payload).status_code == 400


# ---------- GET /saccos ----------


def test_owner_lists_saccos_in_creation_order(client, owner):
    client.post(URL, headers=owner, json={"name": "First"})
    client.post(URL, headers=owner, json={"name": "Second"})
    names = [s["name"] for s in client.get(URL, headers=owner).get_json()["saccos"]]
    assert names == ["First", "Second"]

# ---------- activate / deactivate ----------


def create_sacco(client, headers, name="Umoja SACCO"):
    resp = client.post(URL, headers=headers, json={"name": name})
    assert resp.status_code == 201
    return resp.get_json()["id"]


@pytest.mark.parametrize("action", ["activate", "deactivate"])
def test_status_change_without_token_is_401(client, action):
    assert client.post(f"{URL}/1/{action}").status_code == 401


@pytest.mark.parametrize("action", ["activate", "deactivate"])
def test_status_change_by_ordinary_user_is_403(client, action):
    make_user(USER)
    resp = client.post(f"{URL}/1/{action}", headers=bearer(client, USER))
    assert resp.status_code == 403


@pytest.mark.parametrize("action", ["activate", "deactivate"])
def test_status_change_on_missing_sacco_is_404(client, owner, action):
    assert client.post(f"{URL}/999/{action}", headers=owner).status_code == 404


def test_owner_can_deactivate_and_reactivate(client, owner):
    sacco_id = create_sacco(client, owner)

    resp = client.post(f"{URL}/{sacco_id}/deactivate", headers=owner)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "suspended"

    listed = client.get(URL, headers=owner).get_json()["saccos"]
    assert [s["status"] for s in listed] == ["suspended"]

    resp = client.post(f"{URL}/{sacco_id}/activate", headers=owner)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "active"


def test_status_change_is_idempotent(client, owner):
    sacco_id = create_sacco(client, owner)
    for _ in range(2):
        resp = client.post(f"{URL}/{sacco_id}/deactivate", headers=owner)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "suspended"


def test_deactivating_one_sacco_leaves_others_active(client, owner):
    first = create_sacco(client, owner, "Umoja SACCO")
    create_sacco(client, owner, "Harambee SACCO")
    client.post(f"{URL}/{first}/deactivate", headers=owner)
    listed = client.get(URL, headers=owner).get_json()["saccos"]
    assert [s["status"] for s in listed] == ["suspended", "active"]


# ---------- audit, owner lockout, id range, JSON errors ----------


def audit_actions():
    db.session.expire_all()
    rows = db.session.execute(select(AuditLog).order_by(AuditLog.id)).scalars()
    return [(r.action, r.sacco_id) for r in rows]


def test_create_and_status_changes_are_audited_once_each(client, owner):
    sacco_id = create_sacco(client, owner)
    client.post(f"{URL}/{sacco_id}/deactivate", headers=owner)
    client.post(f"{URL}/{sacco_id}/deactivate", headers=owner)  # no change, no row
    client.post(f"{URL}/{sacco_id}/activate", headers=owner)
    assert audit_actions() == [
        ("sacco.create", sacco_id),
        ("sacco.deactivate", sacco_id),
        ("sacco.activate", sacco_id),
    ]


def test_a_refused_create_writes_no_audit_row(client, owner):
    create_sacco(client, owner, "Umoja SACCO")
    resp = client.post(URL, headers=owner, json={"name": "umoja sacco"})
    assert resp.status_code == 409
    assert audit_actions() == [("sacco.create", 1)]


@pytest.mark.parametrize("name", ["​", "Foo​", "tab\there"])
def test_create_refuses_hidden_characters_like_rename_does(client, owner, name):
    assert client.post(URL, headers=owner, json={"name": name}).status_code == 400


def test_the_owner_can_always_log_in_even_with_a_membership_in_a_suspended_sacco(
    client, owner
):
    sacco_id = create_sacco(client, owner)
    owner_id = db.session.execute(
        select(User.id).where(User.email == OWNER)
    ).scalar_one()
    db.session.add(
        SaccoMembership(user_id=owner_id, sacco_id=sacco_id, role=MembershipRole.MEMBER)
    )
    db.session.commit()
    client.post(f"{URL}/{sacco_id}/deactivate", headers=owner)
    assert "access_token" in login_body(client, OWNER)
    resp = client.post(f"{URL}/{sacco_id}/activate", headers=bearer(client))
    assert resp.status_code == 200


@pytest.mark.parametrize("bad_id", [0, 2**31, 10**40, "9" * 5000, "01", "%EF%BC%91"])
@pytest.mark.parametrize("action", ["activate", "deactivate"])
def test_out_of_range_status_change_is_a_json_404(client, owner, action, bad_id):
    assert create_sacco(client, owner) == 1  # so "01" would hit a real row
    resp = client.post(f"{URL}/{bad_id}/{action}", headers=owner)
    assert resp.status_code == 404
    assert resp.get_json()["error"]["message"] == "Not found."


def test_unknown_routes_and_wrong_methods_answer_in_json(client):
    resp = client.get("/api/v1/nope")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": {"code": "not_found", "message": "Not found."}}
    resp = client.delete(URL)
    assert resp.status_code == 405
    assert resp.get_json()["error"]["message"] == "Method Not Allowed."
    assert "GET" in resp.headers["Allow"]
