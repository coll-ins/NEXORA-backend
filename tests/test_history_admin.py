# ruff: noqa: F811  (pytest fixtures imported from savings_support)
"""Transaction history and admin report tests (Move 3)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.auth.tokens import create_access_token
from app.extensions import db
from app.ledger import reversals
from app.models.membership import MembershipRole, SaccoMembership
from app.models.sacco import Sacco, SaccoStatus
from app.models.user import User
from app.savings import service
from savings_support import Setup, release_session, schema_app  # noqa: F401

pytestmark = pytest.mark.schema

SAVINGS = "/api/v1/savings"
ADMIN = "/api/v1/admin"


def auth(user):
    token = create_access_token(user.id, user.token_version)
    return {"Authorization": f"Bearer {token}"}


def history_url(account_id):
    return f"{SAVINGS}/accounts/{account_id}/transactions"


def summary_url(sacco_id):
    return f"{ADMIN}/saccos/{sacco_id}/reports/summary"


def activity_url(sacco_id):
    return f"{ADMIN}/saccos/{sacco_id}/reports/member-activity"


def suspend(sacco_id):
    db.session.get(Sacco, sacco_id).status = SaccoStatus.SUSPENDED
    db.session.commit()
    db.session.remove()


def add_member(s, role=MembershipRole.MEMBER, with_account=True):
    """Another user in the same SACCO, optionally with a savings account."""
    tag = f"{s.sacco.id}-{id(object())}"
    user = User(email=f"extra-{tag}@example.com", password_hash="x")
    db.session.add(user)
    db.session.flush()
    membership = SaccoMembership(user_id=user.id, sacco_id=s.sacco.id, role=role)
    db.session.add(membership)
    db.session.commit()
    user_id, membership_id = user.id, membership.id
    account_id = None
    if with_account:
        account_id = service.open_account(s.admin.id, membership_id).id
    db.session.remove()

    class Added:
        id = user_id
        token_version = 0

    Added.account_id = account_id
    return Added


# ---------- history ----------


def test_member_sees_own_history_newest_first(schema_app):
    s = Setup(balance=1000)
    service.deposit(s.admin.id, s.account.id, 250, "d2")
    service.withdraw(s.admin.id, s.account.id, 100, "w1")
    resp = schema_app.test_client().get(
        history_url(s.account.id), headers=auth(s.member)
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 3
    assert [t["reference"] for t in body["transactions"]][:2] == ["w1", "d2"]
    first = body["transactions"][0]
    assert set(first) == {
        "id", "type", "amount", "reference", "created_at", "reverses_id", "source"
    }
    assert first["source"] == "standard"
    assert first["type"] == "withdrawal"
    assert first["amount"] == 100 and isinstance(first["amount"], int)
    assert first["reverses_id"] is None


def test_history_is_paginated(schema_app):
    s = Setup()
    for i in range(5):
        service.deposit(s.admin.id, s.account.id, 10, f"d{i}")
    client = schema_app.test_client()
    resp = client.get(
        history_url(s.account.id) + "?page=2&per_page=2", headers=auth(s.admin)
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert (body["page"], body["per_page"], body["total"]) == (2, 2, 5)
    assert [t["reference"] for t in body["transactions"]] == ["d2", "d1"]


def test_per_page_is_capped_and_bad_values_are_400(schema_app):
    s = Setup()
    client = schema_app.test_client()
    url = history_url(s.account.id)
    capped = client.get(url + "?per_page=5000", headers=auth(s.admin))
    assert capped.get_json()["per_page"] == 100
    for query in ("?page=0", "?page=abc", "?per_page=-1"):
        assert client.get(url + query, headers=auth(s.admin)).status_code == 400


def test_member_cannot_see_another_members_account(schema_app):
    s = Setup(balance=500)
    other = add_member(s)
    client = schema_app.test_client()
    assert client.get(history_url(s.account.id), headers=auth(other)).status_code == 404
    mine = client.get(history_url(other.account_id), headers=auth(other))
    assert mine.status_code == 200


def test_admin_and_staff_see_any_account_in_their_sacco(schema_app):
    s = Setup(balance=500)
    staff = add_member(s, role=MembershipRole.STAFF, with_account=False)
    client = schema_app.test_client()
    for user in (s.admin, staff):
        resp = client.get(history_url(s.account.id), headers=auth(user))
        assert resp.status_code == 200
        assert resp.get_json()["total"] == 1


def test_other_sacco_and_missing_account_are_404(schema_app):
    s = Setup(balance=500)
    outsider = Setup()
    client = schema_app.test_client()
    resp = client.get(history_url(s.account.id), headers=auth(outsider.admin))
    assert resp.status_code == 404
    resp = client.get(history_url(2_000_000_000), headers=auth(s.admin))
    assert resp.status_code == 404


def test_history_without_token_is_401(schema_app):
    s = Setup()
    assert schema_app.test_client().get(history_url(s.account.id)).status_code == 401


def test_history_of_a_suspended_sacco_is_423(schema_app):
    s = Setup(balance=500)
    suspend(s.sacco.id)
    client = schema_app.test_client()
    for user in (s.admin, s.member):
        resp = client.get(history_url(s.account.id), headers=auth(user))
        assert resp.status_code == 423


def test_reversal_shows_up_in_history_with_reverses_id(schema_app):
    s = Setup()
    tx = service.deposit(s.admin.id, s.account.id, 700, "d1")
    rev = reversals.reverse_transaction(s.admin.id, tx.id, "r1")
    resp = schema_app.test_client().get(
        history_url(s.account.id), headers=auth(s.admin)
    )
    rows = resp.get_json()["transactions"]
    assert [r["reverses_id"] for r in rows] == [tx.id, None]
    assert rows[0]["id"] == rev.id and rows[0]["type"] == "withdrawal"


# ---------- admin reports ----------


def test_summary_totals_balance_and_account_count(schema_app):
    s = Setup(balance=1000)
    service.deposit(s.admin.id, s.account.id, 500, "d2")
    service.withdraw(s.admin.id, s.account.id, 200, "w1")
    resp = schema_app.test_client().get(summary_url(s.sacco.id), headers=auth(s.admin))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total_balance"] == 1300
    assert body["deposit_total"] == 1500
    assert body["withdrawal_total"] == 200
    assert body["account_count"] == 1
    assert body["reversal_count"] == 0


def test_summary_leaves_out_reversed_entries(schema_app):
    s = Setup(balance=1000)
    tx = service.deposit(s.admin.id, s.account.id, 500, "d2")
    reversals.reverse_transaction(s.admin.id, tx.id, "r1")
    body = (
        schema_app.test_client()
        .get(summary_url(s.sacco.id), headers=auth(s.admin))
        .get_json()
    )
    assert body["total_balance"] == 1000
    assert body["deposit_total"] == 1000
    assert body["withdrawal_total"] == 0
    assert body["reversal_count"] == 1


def test_summary_date_range_filters_totals_not_balance(schema_app):
    s = Setup(balance=1000)
    old = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
    body = (
        schema_app.test_client()
        .get(
            summary_url(s.sacco.id) + f"?from=2000-01-01&to={old}",
            headers=auth(s.admin),
        )
        .get_json()
    )
    assert body["deposit_total"] == 0
    assert body["total_balance"] == 1000


def test_summary_rejects_bad_dates(schema_app):
    s = Setup()
    client = schema_app.test_client()
    for query in ("?from=nope", "?from=2026-02-01&to=2026-01-01"):
        resp = client.get(summary_url(s.sacco.id) + query, headers=auth(s.admin))
        assert resp.status_code == 400


def test_summary_access_rules(schema_app):
    s = Setup(balance=100)
    outsider = Setup()
    staff = add_member(s, role=MembershipRole.STAFF, with_account=False)
    client = schema_app.test_client()
    url = summary_url(s.sacco.id)
    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth(s.member)).status_code == 403
    assert client.get(url, headers=auth(outsider.admin)).status_code == 404
    assert client.get(url, headers=auth(staff)).status_code == 200
    missing = client.get(summary_url(2_000_000_000), headers=auth(s.admin))
    assert missing.status_code == 404
    suspend(s.sacco.id)
    assert client.get(url, headers=auth(s.admin)).status_code == 423


def test_summary_only_counts_its_own_sacco(schema_app):
    s = Setup(balance=1000)
    Setup(balance=9999)
    body = (
        schema_app.test_client()
        .get(summary_url(s.sacco.id), headers=auth(s.admin))
        .get_json()
    )
    assert (body["total_balance"], body["account_count"]) == (1000, 1)


def test_member_activity_counts_and_last_transaction(schema_app):
    s = Setup(balance=1000)
    service.deposit(s.admin.id, s.account.id, 50, "d2")
    idle = add_member(s)
    resp = schema_app.test_client().get(activity_url(s.sacco.id), headers=auth(s.admin))
    assert resp.status_code == 200
    by_account = {m["account_id"]: m for m in resp.get_json()["members"]}
    busy = by_account[s.account.id]
    assert busy["transaction_count"] == 2
    assert busy["last_transaction_at"] is not None
    quiet = by_account[idle.account_id]
    assert quiet["transaction_count"] == 0
    assert quiet["last_transaction_at"] is None


def test_member_activity_excludes_other_saccos_and_blocks_members(schema_app):
    s = Setup(balance=1000)
    other = Setup(balance=1000)
    client = schema_app.test_client()
    resp = client.get(activity_url(s.sacco.id), headers=auth(s.admin))
    ids = [m["account_id"] for m in resp.get_json()["members"]]
    assert ids == [s.account.id] and other.account.id not in ids
    member_resp = client.get(activity_url(s.sacco.id), headers=auth(s.member))
    assert member_resp.status_code == 403
    other_resp = client.get(activity_url(s.sacco.id), headers=auth(other.admin))
    assert other_resp.status_code == 404


def test_huge_page_is_not_a_server_error(schema_app):
    s = Setup(balance=100)
    client = schema_app.test_client()
    big = history_url(s.account.id) + "?page=99999999999999999999"
    resp = client.get(big, headers=auth(s.admin))
    assert resp.status_code == 200 and resp.get_json()["transactions"] == []
    # Digits that are not plain ASCII are refused, so one page has one URL.
    for query in ("?page=1_0", "?page=%EF%BC%91"):
        got = client.get(history_url(s.account.id) + query, headers=auth(s.admin))
        assert got.status_code == 400


def test_summary_to_the_last_date_is_not_a_server_error(schema_app):
    s = Setup(balance=100)
    resp = schema_app.test_client().get(
        summary_url(s.sacco.id) + "?to=9999-12-31", headers=auth(s.admin)
    )
    assert resp.status_code == 200


def test_member_activity_is_paged(schema_app):
    s = Setup(balance=100)
    client = schema_app.test_client()
    url = activity_url(s.sacco.id)
    ok = client.get(url + "?per_page=1&page=1", headers=auth(s.admin))
    assert ok.status_code == 200 and ok.get_json()["per_page"] == 1
    assert len(ok.get_json()["members"]) <= 1
    assert client.get(url + "?page=0", headers=auth(s.admin)).status_code == 400


def test_unexpected_errors_answer_in_json(schema_app, monkeypatch):
    s = Setup(balance=100)

    def boom(*args, **kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr("app.admin.routes.reports.sacco_summary", boom)
    schema_app.config["PROPAGATE_EXCEPTIONS"] = False
    resp = schema_app.test_client().get(summary_url(s.sacco.id), headers=auth(s.admin))
    assert resp.status_code == 500
    assert resp.get_json()["error"]["message"] == "Internal error."
