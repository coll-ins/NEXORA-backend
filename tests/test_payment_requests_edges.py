"""Item 6 edges: paging, filters, bad input and the refusals that the main
test file (test_payment_requests.py) does not reach.

Same rule as there: a request moves no money; only a confirmation does.
"""

import uuid

import pytest

from app.extensions import db
from app.models.access import MembershipRoleLink, Role
from app.models.membership import MembershipRole, SaccoMembership
from app.models.user import User
from app.savings import service
from savings_support import Setup, release_session, schema_app  # noqa: F401
from test_payment_requests import BASE, confirm, create, reject
from test_schema_rules import auth

pytestmark = pytest.mark.schema


def _mine(client, user, s, query=""):
    return client.get(
        f"{BASE}/requests/me?sacco_id={s.sacco.id}{query}", headers=auth(user)
    )


def _queue(client, user, s, query=""):
    return client.get(f"{BASE}/saccos/{s.sacco.id}/requests{query}", headers=auth(user))


# ------------------------------------------------------------------ paging


def test_paging_values_must_be_whole_numbers(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    for resp in (
        _mine(client, s.member, s, "&limit=abc"),
        _mine(client, s.member, s, "&cursor=x"),
        _queue(client, s.admin, s, "?limit=1.5"),
    ):
        assert resp.status_code == 400
        assert resp.json["error"]["message"] == (
            "limit and cursor must be whole numbers."
        )


def test_my_requests_need_a_numeric_sacco_id(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    missing = client.get(f"{BASE}/requests/me", headers=auth(s.member))
    bad = client.get(f"{BASE}/requests/me?sacco_id=abc", headers=auth(s.member))
    for resp in (missing, bad):
        assert resp.status_code == 400
        assert resp.json["error"]["message"] == "sacco_id is required."


def test_kind_and_status_filters_are_checked(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    kind = _mine(client, s.member, s, "&kind=loan")
    status = _queue(client, s.admin, s, "?status=weird")
    for resp in (kind, status):
        assert resp.status_code == 400
        assert "must be one of" in resp.json["error"]["message"]


def test_pages_walk_newest_first_with_limit_and_cursor(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    ids = [create(client, s.member, s, amount=100 * n).json["id"] for n in (1, 2, 3)]

    first = _mine(client, s.member, s, "&limit=2").json
    assert [item["id"] for item in first["items"]] == [ids[2], ids[1]]
    assert first["next"] == ids[1]

    second = _mine(client, s.member, s, f"&limit=2&cursor={first['next']}").json
    assert [item["id"] for item in second["items"]] == [ids[0]]
    assert second["next"] is None

    # A kind filter narrows the member's list; the officer queue pages too.
    assert _mine(client, s.member, s, "&kind=withdrawal").json["items"] == []
    queue = _queue(client, s.admin, s, "?status=pending&limit=1").json
    assert len(queue["items"]) == 1 and queue["next"] == queue["items"][0]["id"]


def test_a_stranger_gets_404_for_a_sacco_they_are_not_in(schema_app):  # noqa: F811
    mine, other = Setup(), Setup()
    client = schema_app.test_client()
    resp = client.get(
        f"{BASE}/requests/me?sacco_id={other.sacco.id}", headers=auth(mine.member)
    )
    assert resp.status_code == 404


# ------------------------------------------------------------ create replay


def test_a_reference_reused_with_other_details_is_a_conflict(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    reference = uuid.uuid4().hex
    assert (
        create(client, s.member, s, amount=500, reference=reference).status_code == 201
    )
    other = create(client, s.member, s, amount=900, reference=reference)
    assert other.status_code == 409
    assert other.json["error"]["code"] == "DUPLICATE_REFERENCE"


# ----------------------------------------------------------- confirm, reject


def test_confirm_needs_a_reference(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    request_id = create(client, s.member, s).json["id"]
    for body in ({}, {"reference": "   "}, {"reference": 12}):
        resp = client.post(
            f"{BASE}/requests/{request_id}/confirm", json=body, headers=auth(s.admin)
        )
        assert resp.status_code == 400
        assert resp.json["error"]["message"] == "reference is required."


def test_a_reason_over_500_characters_is_refused(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    request_id = create(client, s.member, s).json["id"]
    resp = reject(client, s.admin, request_id, reason="x" * 501)
    assert resp.status_code == 400
    assert resp.json["error"]["message"] == "reason cannot exceed 500 characters."
    assert reject(client, s.admin, request_id, reason="x" * 500).status_code == 200


def test_an_unknown_request_is_404_to_confirm_or_reject(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    assert confirm(client, s.admin, 999999999).status_code == 404
    assert reject(client, s.admin, 999999999).status_code == 404


def test_a_decided_request_cannot_be_decided_again(schema_app):  # noqa: F811
    s = Setup(balance=1000)
    client = schema_app.test_client()
    rejected = create(client, s.member, s).json["id"]
    assert reject(client, s.admin, rejected).status_code == 200
    for resp in (reject(client, s.admin, rejected), confirm(client, s.admin, rejected)):
        assert resp.status_code == 409
        assert resp.json["error"]["code"] == "REQUEST_ALREADY_DECIDED"


def test_a_withdrawal_that_no_longer_fits_stays_pending(schema_app):  # noqa: F811
    s = Setup(balance=5000)
    client = schema_app.test_client()
    request_id = create(client, s.member, s, kind="withdrawal", amount=4000).json["id"]
    # The balance drops after the request was made: another officer posts a withdrawal.
    service.withdraw(s.admin.id, s.account.id, 3000, uuid.uuid4().hex)
    db.session.remove()

    resp = confirm(client, s.admin, request_id)
    assert resp.status_code == 409
    assert resp.json["error"]["code"] == "INSUFFICIENT_FUNDS"
    pending = _queue(client, s.admin, s, "?status=pending").json["items"]
    assert [item["id"] for item in pending] == [request_id]


def test_confirming_for_a_member_with_no_account_is_a_conflict(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    newcomer = User(email=f"new-{uuid.uuid4().hex[:8]}@example.com", password_hash="x")
    db.session.add(newcomer)
    db.session.flush()
    db.session.add(
        SaccoMembership(
            user_id=newcomer.id, sacco_id=s.sacco.id, role=MembershipRole.MEMBER
        )
    )
    db.session.commit()
    made = create(client, newcomer, s)
    assert made.status_code == 201, made.json
    resp = confirm(client, s.admin, made.json["id"])
    assert resp.status_code == 409
    assert resp.json["error"]["message"] == (
        "The member has no savings account in this SACCO."
    )


def test_an_officer_cannot_reject_their_own_request(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    for code in ("member", "sacco_admin"):
        role = db.session.query(Role).filter_by(code=code).one()
        db.session.add(
            MembershipRoleLink(membership_id=s.member_membership.id, role_id=role.id)
        )
    db.session.commit()
    made = create(client, s.member, s)
    assert made.status_code == 201, made.json
    resp = reject(client, s.member, made.json["id"])
    assert resp.status_code == 403
    assert resp.json["error"]["code"] == "OWN_REQUEST"
