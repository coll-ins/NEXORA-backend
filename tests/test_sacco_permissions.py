"""Item 2: SACCO-scoped routes by permission, and an audit row for every write."""

import pytest
from sqlalchemy import func, select

from app.extensions import db
from app.models.audit import AuditLog
from savings_support import (  # noqa: F401
    Setup,
    manual_body,
    release_session,
    schema_app,
    switch_off_quick_withdrawal_flag,
)
from test_schema_rules import auth, make_user, officer

pytestmark = pytest.mark.schema


def audit_count(action):
    count = db.session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
    )
    db.session.remove()
    return count


def test_renaming_a_sacco_needs_sacco_update(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    secretary = officer(s, "secretary")
    url = f"/api/v1/saccos/{s.sacco.id}"
    refused = client.patch(url, json={"name": "Renamed"}, headers=auth(secretary))
    assert refused.status_code == 403
    renamed = client.patch(
        url, json={"name": f"Renamed {s.sacco.id}"}, headers=auth(s.admin)
    )
    assert renamed.status_code == 200


def test_every_active_member_reads_their_own_sacco(schema_app):  # noqa: F811
    s, other = Setup(), Setup()
    client = schema_app.test_client()
    url = f"/api/v1/saccos/{s.sacco.id}"
    assert client.get(url, headers=auth(s.member)).status_code == 200
    assert client.get(url, headers=auth(other.member)).status_code == 404


def test_only_the_admin_title_manages_members_and_titles(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    secretary = officer(s, "secretary")
    base = f"/api/v1/saccos/{s.sacco.id}/members"
    add = client.post(base, json={"phone": "+254700000901"}, headers=auth(secretary))
    assert add.status_code == 403
    # Reading titles needs member.view (the secretary has it); setting needs
    # member.manage (the admin).
    url = f"{base}/{s.member_membership.id}/roles"
    assert client.get(url, headers=auth(secretary)).status_code == 200
    put = client.put(url, json={"roles": ["member"]}, headers=auth(secretary))
    assert put.status_code == 403
    assert (
        client.post(
            base, json={"phone": "+254700000902"}, headers=auth(s.admin)
        ).status_code
        == 201
    )


def test_sacco_login_writes_an_audit_row_ok_or_refused(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    person = make_user()
    phone = "+254700000903"
    invited = client.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"phone": phone},
        headers=auth(s.admin),
    )
    joined = client.post(
        "/api/v1/auth/join",
        json={"phone": phone, "code": invited.json["join_code"]},
        headers=auth(person),
    )
    client.post(
        "/api/v1/auth/join/pin",
        json={"setup_token": joined.json["setup_token"], "pin": "4826"},
        headers=auth(person),
    )
    before = audit_count("member.sacco_login")
    url = "/api/v1/auth/sacco-login"
    good, wrong = (
        {"sacco_id": s.sacco.id, "pin": "4826"},
        {"sacco_id": s.sacco.id, "pin": "0000"},
    )
    ok = client.post(url, json=good, headers=auth(person))
    bad = client.post(url, json=wrong, headers=auth(person))
    assert ok.status_code == 200 and bad.status_code == 401
    assert audit_count("member.sacco_login") == before + 2


WRITES = [
    # (method, path template, body, audit action prefix)
    (
        "post",
        "/api/v1/savings/accounts/{account}/deposits",
        manual_body(10, receipt="AUD0DEP001"),
        "savings.deposit",
    ),
    (
        "post",
        "/api/v1/savings/accounts/{account}/withdrawals",
        {"amount": 5, "reference": "aud-wd"},
        "savings.withdraw",
    ),
    ("patch", "/api/v1/saccos/{sacco}", {"name": "Audit Rename {sacco}"}, "sacco."),
    ("post", "/api/v1/saccos/{sacco}/members", {"phone": "+254700000904"}, "member."),
]


@pytest.mark.parametrize(("method", "path", "body", "action"), WRITES)
def test_sacco_writes_leave_an_audit_row(schema_app, method, path, body, action):  # noqa: F811
    s = Setup(balance=1_000)
    switch_off_quick_withdrawal_flag(s.sacco.id, s.admin.id)
    client = schema_app.test_client()
    fill = {"account": s.account.id, "sacco": s.sacco.id}
    body = {k: v.format(**fill) if isinstance(v, str) else v for k, v in body.items()}
    before = db.session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.sacco_id == s.sacco.id, AuditLog.action.like(f"{action}%"))
    )
    db.session.remove()
    call = getattr(client, method)
    resp = call(path.format(**fill), json=body, headers=auth(s.admin))
    assert resp.status_code in (200, 201), resp.json
    after = db.session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.sacco_id == s.sacco.id, AuditLog.action.like(f"{action}%"))
    )
    db.session.remove()
    assert after == before + 1
