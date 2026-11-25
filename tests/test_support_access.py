"""Item 4: consent-based support access for platform owners."""

import time

import pytest
from sqlalchemy import func, select, text

from app.extensions import db
from app.models.audit import AuditLog
from app.models.membership import MembershipRole, SaccoMembership
from savings_support import Setup, release_session, schema_app  # noqa: F401
from test_schema_rules import auth, make_user, officer

pytestmark = pytest.mark.schema

READS = {
    "audit": "/api/v1/admin/saccos/{sid}/audit",
    "members": "/api/v1/saccos/{sid}/members",
    "savings": "/api/v1/admin/saccos/{sid}/accounts",
    "loans": "/api/v1/loans/saccos/{sid}",
}


def ask(client, owner, s, scope="audit", reason="Customer ticket 42"):
    return client.post(
        f"/api/v1/saccos/{s.sacco.id}/support-access",
        json={"reason": reason, "scope": scope},
        headers=auth(owner),
    )


def decide(client, who, request_id, action, **body):
    return client.post(
        f"/api/v1/support-access/{request_id}/{action}", json=body, headers=auth(who)
    )


def reads(client, owner, s):
    return {
        scope: client.get(path.format(sid=s.sacco.id), headers=auth(owner)).status_code
        for scope, path in READS.items()
    }


def test_without_a_grant_an_owner_reads_no_sacco_data(schema_app):  # noqa: F811
    s = Setup()
    owner = make_user(owner=True)
    client = schema_app.test_client()
    assert set(reads(client, owner, s).values()) == {403}
    # Platform information stays visible: name, status and settings.
    assert (
        client.get(f"/api/v1/saccos/{s.sacco.id}", headers=auth(owner)).status_code
        == 200
    )


def test_only_a_platform_owner_can_ask(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    assert ask(client, s.admin, s).status_code == 403
    for bad in ({"reason": "", "scope": "audit"}, {"reason": "x", "scope": "wallets"}):
        resp = client.post(
            f"/api/v1/saccos/{s.sacco.id}/support-access",
            json=bad,
            headers=auth(make_user(owner=True)),
        )
        assert resp.status_code == 400


def test_an_approved_grant_reads_its_scope_only_and_is_audited(schema_app):  # noqa: F811
    s = Setup()
    owner = make_user(owner=True)
    client = schema_app.test_client()
    asked = ask(client, owner, s, scope="audit")
    assert asked.status_code == 201 and asked.json["status"] == "requested"
    grant_id = asked.json["id"]
    approved = decide(client, s.admin, grant_id, "approve", hours=2)
    assert approved.status_code == 200 and approved.json["status"] == "approved"

    status = reads(client, owner, s)
    assert status == {"audit": 200, "members": 403, "savings": 403, "loans": 403}
    rows = (
        db.session.execute(
            select(AuditLog.target).where(
                AuditLog.action == "support.read", AuditLog.sacco_id == s.sacco.id
            )
        )
        .scalars()
        .all()
    )
    db.session.remove()
    assert len(rows) == 1 and rows[0].startswith(f"grant:{grant_id} GET ")

    # Never write: an owner under a grant still cannot add a member.
    added = client.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"phone": "+254700000960"},
        headers=auth(owner),
    )
    assert added.status_code == 403


def test_a_grant_belongs_to_the_owner_who_asked(schema_app):  # noqa: F811
    s = Setup()
    owner, other_owner = make_user(owner=True), make_user(owner=True)
    client = schema_app.test_client()
    grant = ask(client, owner, s).json["id"]
    decide(client, s.admin, grant, "approve", hours=1)
    url = READS["audit"].format(sid=s.sacco.id)
    assert client.get(url, headers=auth(owner)).status_code == 200
    assert client.get(url, headers=auth(other_owner)).status_code == 403


def test_who_may_decide(schema_app):  # noqa: F811
    s, other = Setup(), Setup()
    owner = make_user(owner=True)
    client = schema_app.test_client()
    grant = ask(client, owner, s).json["id"]
    secretary = officer(s, "secretary")
    for who in (secretary, other.admin, s.member, owner):
        assert decide(client, who, grant, "approve", hours=1).status_code == 403
    assert decide(client, s.admin, grant, "approve", hours=25).status_code == 400


def test_the_requester_cannot_approve_even_as_the_saccos_admin(schema_app):  # noqa: F811
    s = Setup()
    owner = make_user(owner=True)
    db.session.add(
        SaccoMembership(
            user_id=owner.id, sacco_id=s.sacco.id, role=MembershipRole.SACCO_ADMIN
        )
    )
    db.session.commit()
    db.session.remove()
    client = schema_app.test_client()
    grant = ask(client, owner, s).json["id"]
    assert decide(client, owner, grant, "approve", hours=1).status_code == 403


def test_deny_and_revoke(schema_app):  # noqa: F811
    s = Setup()
    owner = make_user(owner=True)
    client = schema_app.test_client()
    url = READS["members"].format(sid=s.sacco.id)

    denied = ask(client, owner, s, scope="members").json["id"]
    assert decide(client, s.admin, denied, "deny").json["status"] == "denied"
    assert decide(client, s.admin, denied, "approve", hours=1).status_code == 409
    assert client.get(url, headers=auth(owner)).status_code == 403

    granted = ask(client, owner, s, scope="members").json["id"]
    full_day = decide(client, s.admin, granted, "approve", hours=24)
    assert full_day.status_code == 200, full_day.json  # the full 24 hours is allowed
    assert client.get(url, headers=auth(owner)).status_code == 200
    revoked = decide(client, s.admin, granted, "revoke")
    assert revoked.status_code == 200 and revoked.json["status"] == "revoked"
    assert client.get(url, headers=auth(owner)).status_code == 403
    assert decide(client, s.admin, granted, "revoke").status_code == 409

    listed = client.get(
        f"/api/v1/saccos/{s.sacco.id}/support-access", headers=auth(s.admin)
    )
    assert [r["status"] for r in listed.json["requests"]] == ["revoked", "denied"]
    assert (
        client.get(
            f"/api/v1/saccos/{s.sacco.id}/support-access", headers=auth(s.member)
        ).status_code
        == 403
    )


def test_a_grant_ends_when_it_expires(schema_app):  # noqa: F811
    s = Setup()
    owner = make_user(owner=True)
    client = schema_app.test_client()
    grant = ask(client, owner, s).json["id"]
    # Approve for two seconds, straight in the database (the API's minimum is
    # one hour); the trigger still checks who decides.
    db.session.execute(
        text(
            "UPDATE support_access_requests SET status = 'approved', "
            "decided_by_membership_id = :m, expires_at = now() + interval '2 seconds' "
            "WHERE id = :g"
        ),
        {"m": s.admin_membership.id, "g": grant},
    )
    db.session.commit()
    db.session.remove()
    url = READS["audit"].format(sid=s.sacco.id)
    assert client.get(url, headers=auth(owner)).status_code == 200
    time.sleep(2.5)
    assert client.get(url, headers=auth(owner)).status_code == 403
    listed = client.get(
        f"/api/v1/saccos/{s.sacco.id}/support-access", headers=auth(s.admin)
    )
    assert listed.json["requests"][0]["status"] == "expired"


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "INSERT INTO support_access_requests (sacco_id, requested_by_user_id, "
            "reason, scope) VALUES (:s, :member, 'r', 'audit')",
            "only an active platform owner",
        ),
        (
            "UPDATE support_access_requests SET status = 'approved', "
            "decided_by_membership_id = :mm, expires_at = now() + interval '1 hour' "
            "WHERE id = :g",
            "only an active SACCO admin",
        ),
        (
            "UPDATE support_access_requests SET status = 'approved', "
            "decided_by_membership_id = :am, expires_at = now() + interval '25 hours' "
            "WHERE id = :g",
            "at most 24 hours",
        ),
        (
            "UPDATE support_access_requests SET scope = 'loans' WHERE id = :g",
            "cannot be rewritten",
        ),
        ("DELETE FROM support_access_requests WHERE id = :g", "append-only"),
    ],
)
def test_the_database_holds_the_rules(schema_app, statement, message):  # noqa: F811
    s = Setup()
    owner = make_user(owner=True)
    grant = ask(schema_app.test_client(), owner, s).json["id"]
    params = {
        "s": s.sacco.id,
        "member": s.member.id,
        "mm": s.member_membership.id,
        "am": s.admin_membership.id,
        "g": grant,
    }
    with pytest.raises(Exception, match=message):
        db.session.execute(text(statement), params)
    db.session.rollback()
    count = db.session.scalar(select(func.count()).select_from(AuditLog))
    db.session.remove()
    assert count >= 1
