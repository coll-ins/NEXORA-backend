"""Item 6: a suspended SACCO answers 423 everywhere it is named; login still works."""

import pytest

from app.auth.passwords import hash_password
from app.auth.suspension import SUSPENDED_MESSAGE
from app.extensions import db
from app.models.user import User
from savings_support import (  # noqa: F401
    Setup,
    manual_body,
    release_session,
    schema_app,
)
from test_schema_rules import auth, make_user

pytestmark = pytest.mark.schema

PASSWORD = "Suspension-Test-Pass-2026"


def can_log_in(client, user_id):
    user = db.session.get(User, user_id)
    user.password_hash = hash_password(PASSWORD)
    email = user.email
    db.session.commit()
    db.session.remove()
    resp = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    return resp


def scoped_requests(s):
    """(method, path, body) for SACCO-scoped endpoints, reads and writes."""
    sid, acc = s.sacco.id, s.account.id
    return [
        ("get", f"/api/v1/saccos/{sid}", None),
        ("get", f"/api/v1/saccos/{sid}/settings", None),
        ("get", f"/api/v1/saccos/{sid}/members", None),
        ("post", f"/api/v1/saccos/{sid}/members", {"phone": "+254700000950"}),
        ("get", f"/api/v1/saccos/{sid}/members/{s.member_membership.id}/roles", None),
        ("get", f"/api/v1/savings/accounts/{acc}", None),
        ("get", f"/api/v1/savings/accounts/{acc}/transactions", None),
        ("post", f"/api/v1/savings/accounts/{acc}/deposits",
         manual_body(10)),
        ("get", f"/api/v1/loans/saccos/{sid}", None),
        ("get", f"/api/v1/admin/saccos/{sid}/reports/summary", None),
        ("get", f"/api/v1/admin/saccos/{sid}/accounts", None),
    ]


def test_a_suspended_sacco_answers_423_but_login_and_my_saccos_work(schema_app):  # noqa: F811
    s = Setup(balance=1_000)
    client = schema_app.test_client()
    boss = auth(make_user(owner=True))

    suspended = client.post(f"/api/v1/saccos/{s.sacco.id}/deactivate", headers=boss)
    assert suspended.status_code == 200

    for method, path, body in scoped_requests(s):
        resp = getattr(client, method)(path, json=body, headers=auth(s.admin))
        assert resp.status_code == 423, (method, path, resp.json)
        assert resp.json["error"]["message"] == SUSPENDED_MESSAGE
    member_loan = client.post(
        f"/api/v1/loans/saccos/{s.sacco.id}",
        json={"principal": 100, "term_months": 3},
        headers=auth(s.member),
    )
    assert member_loan.status_code == 423

    # Not SACCO-scoped: still works, and shows the suspension.
    assert can_log_in(client, s.member.id).status_code == 200
    assert client.get("/api/v1/members/me", headers=auth(s.member)).status_code == 200
    mine = client.get("/api/v1/members/me/saccos", headers=auth(s.member))
    assert mine.status_code == 200
    assert mine.json["saccos"][0]["status"] == "suspended"

    reactivated = client.post(f"/api/v1/saccos/{s.sacco.id}/activate", headers=boss)
    assert reactivated.status_code == 200
    for method, path, _ in scoped_requests(s):
        if method == "get":
            resp = getattr(client, method)(path, headers=auth(s.admin))
            assert resp.status_code == 200, (path, resp.json)


def test_only_a_platform_owner_suspends_or_reactivates(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    for action in ("deactivate", "activate"):
        resp = client.post(
            f"/api/v1/saccos/{s.sacco.id}/{action}", headers=auth(s.admin)
        )
        assert resp.status_code == 403


def test_joining_or_pin_login_in_a_suspended_sacco_is_423(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    person = make_user()
    phone = "+254700000951"
    invited = client.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"phone": phone},
        headers=auth(s.admin),
    )
    boss = auth(make_user(owner=True))
    client.post(f"/api/v1/saccos/{s.sacco.id}/deactivate", headers=boss)
    joined = client.post(
        "/api/v1/auth/join",
        json={"phone": phone, "code": invited.json["join_code"]},
        headers=auth(person),
    )
    assert joined.status_code == 423
    entered = client.post(
        "/api/v1/auth/sacco-login",
        json={"sacco_id": s.sacco.id, "pin": "4826"},
        headers=auth(s.member),
    )
    assert entered.status_code == 423
