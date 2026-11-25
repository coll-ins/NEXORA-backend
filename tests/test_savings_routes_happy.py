# ruff: noqa: F811  (pytest fixtures imported from savings_support)
"""Happy paths for the savings routes that only the service tests reached."""

import pytest

from app.auth.tokens import create_access_token
from app.extensions import db
from app.models.membership import MembershipRole, SaccoMembership
from app.models.user import User
from savings_support import Setup, release_session, schema_app  # noqa: F401

pytestmark = pytest.mark.schema


def _admin_headers(s):
    user = db.session.get(User, s.admin.id)
    token = create_access_token(user.id, user.token_version)
    return {"Authorization": f"Bearer {token}"}


def test_the_admin_opens_an_account_over_http(schema_app):
    s = Setup()
    # Setup already gave its member an account (one per member), so add a member.
    extra = User(email=f"extra-{s.sacco.id}@example.com", password_hash="x")
    db.session.add(extra)
    db.session.flush()
    membership = SaccoMembership(
        user_id=extra.id, sacco_id=s.sacco.id, role=MembershipRole.MEMBER
    )
    db.session.add(membership)
    db.session.commit()
    membership_id = membership.id

    resp = schema_app.test_client().post(
        "/api/v1/savings/accounts",
        json={"membership_id": membership_id},
        headers=_admin_headers(s),
    )
    assert resp.status_code == 201
    assert resp.json["balance"] == 0


@pytest.mark.parametrize("kind", ["interest", "fees"])
def test_the_admin_posts_interest_and_fees_over_http(schema_app, kind):
    s = Setup(balance=10_000)
    resp = schema_app.test_client().post(
        f"/api/v1/savings/accounts/{s.account.id}/{kind}",
        json={"amount": 500, "reference": f"{kind}-1"},
        headers=_admin_headers(s),
    )
    assert resp.status_code == 201
    assert resp.json["balance"] == (10_500 if kind == "interest" else 9_500)
