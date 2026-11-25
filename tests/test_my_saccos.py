"""Item 5: one user, several SACCOs, one login."""

import pytest

from app.extensions import db
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from savings_support import (  # noqa: F401
    Setup,
    manual_body,
    release_session,
    schema_app,
)
from test_schema_rules import _link, auth, make_user

pytestmark = pytest.mark.schema


def join_as(user, s, role=MembershipRole.MEMBER, title=None):
    membership = SaccoMembership(user_id=user.id, sacco_id=s.sacco.id, role=role)
    db.session.add(membership)
    db.session.commit()
    mid = membership.id
    db.session.remove()
    if title:
        _link(mid, title, granted_by=s.admin.id)
        db.session.remove()
    return mid


def test_one_user_in_three_saccos_acts_in_each_without_logging_in_again(schema_app):  # noqa: F811
    a, b, c = Setup(balance=1_000), Setup(), Setup()
    person = make_user()
    join_as(person, a, MembershipRole.STAFF, "finance_officer")
    join_as(person, b, MembershipRole.STAFF, "secretary")
    c_mid = join_as(person, c)
    client = schema_app.test_client()
    headers = auth(person)  # one token for everything below

    listed = client.get("/api/v1/members/me/saccos", headers=headers)
    assert listed.status_code == 200
    by_id = {row["id"]: row for row in listed.json["saccos"]}
    assert set(by_id) == {a.sacco.id, b.sacco.id, c.sacco.id}
    assert "finance_officer" in by_id[a.sacco.id]["roles"]
    assert by_id[b.sacco.id]["roles"] == ["secretary"]
    assert by_id[c.sacco.id]["roles"] == ["member"]
    assert {row["status"] for row in listed.json["saccos"]} == {"active"}

    # Act in each by naming it: what is allowed depends on that SACCO's roles.
    deposit = client.post(
        f"/api/v1/savings/accounts/{a.account.id}/deposits",
        json=manual_body(10),
        headers=headers,
    )
    assert deposit.status_code == 201  # finance officer in A
    audit = client.get(f"/api/v1/admin/saccos/{b.sacco.id}/audit", headers=headers)
    assert audit.status_code == 200  # secretary in B
    assert client.get(
        f"/api/v1/admin/saccos/{c.sacco.id}/audit", headers=headers
    ).status_code == 403  # plain member in C

    # The membership is checked on every request: losing C bites at once.
    db.session.get(SaccoMembership, c_mid).status = MembershipStatus.INACTIVE
    db.session.commit()
    db.session.remove()
    settings = client.get(f"/api/v1/saccos/{c.sacco.id}/settings", headers=headers)
    assert settings.status_code == 403
    assert client.get(
        f"/api/v1/saccos/{a.sacco.id}/settings", headers=headers
    ).status_code == 200
    again = client.get("/api/v1/members/me/saccos", headers=headers).json["saccos"]
    assert {r["id"]: r["membership_status"] for r in again}[c.sacco.id] == "inactive"


def test_my_saccos_needs_a_token_and_shows_only_mine(schema_app):  # noqa: F811
    s, other = Setup(), Setup()
    client = schema_app.test_client()
    assert client.get("/api/v1/members/me/saccos").status_code == 401
    mine = client.get("/api/v1/members/me/saccos", headers=auth(s.member))
    assert [row["id"] for row in mine.json["saccos"]] == [s.sacco.id]
    assert other.sacco.id not in [row["id"] for row in mine.json["saccos"]]
