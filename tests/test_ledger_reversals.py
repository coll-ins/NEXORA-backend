# ruff: noqa: F811  (pytest fixtures imported from savings_support)
"""Ledger balance and reversal tests (Move 3)."""

import pytest
from sqlalchemy import text

from app.auth.tokens import create_access_token
from app.extensions import db
from app.ledger import reversals
from app.models.audit import AuditLog
from app.savings import service
from app.savings.balance import ledger_balance, verify_balance
from savings_support import (  # noqa: F401
    Setup,
    add_officer,
    approve_reversal,
    release_session,
    schema_app,
)

pytestmark = pytest.mark.schema


def auth(user):
    token = create_access_token(user.id, user.token_version)
    return {"Authorization": f"Bearer {token}"}


def deposit(s, amount, ref):
    return service.deposit(s.admin.id, s.account.id, amount, ref)


def test_balance_matches_ledger(schema_app):
    s = Setup(balance=1000)
    deposit(s, 500, "d2")
    service.withdraw(s.admin.id, s.account.id, 300, "w1")
    check = verify_balance(s.account.id)
    assert (check.ledger, check.stored, check.entries) == (1200, 1200, 3)
    assert check.consistent
    assert ledger_balance(s.account.id) == 1200


def test_empty_account_balance_is_zero(schema_app):
    s = Setup()
    assert ledger_balance(s.account.id) == 0
    assert verify_balance(s.account.id).consistent
    assert verify_balance(2_000_000_000) is None


def test_reverse_deposit_adds_opposite_entry_and_keeps_history(schema_app):
    s = Setup()
    tx = deposit(s, 700, "d1")
    rev = reversals.reverse_transaction(s.admin.id, tx.id, "r1")
    assert (rev.type, rev.amount, rev.reverses_id, rev.balance) == (
        "withdrawal", 700, tx.id, 0)
    rows = db.session.execute(
        text("SELECT id, type, amount FROM transactions "
             "WHERE savings_account_id = :a ORDER BY id"), {"a": s.account.id}
    ).all()
    assert [(r.type, r.amount) for r in rows] == [("deposit", 700), ("withdrawal", 700)]
    assert verify_balance(s.account.id).consistent
    actions = [a.action for a in db.session.query(AuditLog)
               .filter_by(sacco_id=s.sacco.id)]
    assert "savings.reverse" in actions


def test_reverse_withdrawal_restores_funds(schema_app):
    s = Setup(balance=1000)
    w = service.withdraw(s.admin.id, s.account.id, 400, "w1")
    rev = reversals.reverse_transaction(s.admin.id, w.id, "r1")
    assert (rev.type, rev.balance) == ("deposit", 1000)


def test_cannot_reverse_twice(schema_app):
    s = Setup()
    tx = deposit(s, 100, "d1")
    reversals.reverse_transaction(s.admin.id, tx.id, "r1")
    with pytest.raises(service.Conflict):
        reversals.reverse_transaction(s.admin.id, tx.id, "r2")


def test_retry_with_same_reference_replays(schema_app):
    s = Setup()
    tx = deposit(s, 100, "d1")
    first = reversals.reverse_transaction(s.admin.id, tx.id, "r1")
    again = reversals.reverse_transaction(s.admin.id, tx.id, "r1")
    assert again.replayed and again.id == first.id


def test_cannot_reverse_a_reversal(schema_app):
    s = Setup()
    tx = deposit(s, 100, "d1")
    rev = reversals.reverse_transaction(s.admin.id, tx.id, "r1")
    with pytest.raises(service.Conflict):
        reversals.reverse_transaction(s.admin.id, rev.id, "r2")
    assert verify_balance(s.account.id).ledger == 0


def test_reversal_cannot_overdraw(schema_app):
    s = Setup()
    tx = deposit(s, 1000, "d1")
    service.withdraw(s.admin.id, s.account.id, 800, "w1")
    with pytest.raises(service.Conflict):
        reversals.reverse_transaction(s.admin.id, tx.id, "r1")
    check = verify_balance(s.account.id)
    assert (check.ledger, check.entries) == (200, 2)


def test_reference_used_elsewhere_is_refused(schema_app):
    s = Setup()
    tx = deposit(s, 100, "d1")
    with pytest.raises(service.Conflict):
        reversals.reverse_transaction(s.admin.id, tx.id, "d1")


def test_member_cannot_reverse(schema_app):
    s = Setup(balance=100)
    tx_id = db.session.execute(
        text("SELECT id FROM transactions WHERE savings_account_id = :a"),
        {"a": s.account.id}).scalar_one()
    with pytest.raises(service.Forbidden):
        reversals.reverse_transaction(s.member.id, tx_id, "r1")


def test_other_sacco_gets_404(schema_app):
    a, b = Setup(balance=100), Setup()
    tx_id = db.session.execute(
        text("SELECT id FROM transactions WHERE savings_account_id = :a"),
        {"a": a.account.id}).scalar_one()
    with pytest.raises(service.NotFound):
        reversals.reverse_transaction(b.admin.id, tx_id, "r1")
    with pytest.raises(service.NotFound):
        reversals.reverse_transaction(b.admin.id, 2_000_000_000, "r1")
    assert verify_balance(a.account.id).ledger == 100


def test_route_happy_and_failure(schema_app):
    client = schema_app.test_client()
    s = Setup()
    tx = deposit(s, 250, "d1")
    url = f"/api/v1/savings/transactions/{tx.id}/reversals"
    why = {"reason": "Posted to the wrong member"}

    assert client.post(url, json=why).status_code == 401
    assert client.post(url, json=why, headers=auth(s.member)).status_code == 403
    asked = client.post(url, json=why, headers=auth(s.admin))
    assert asked.status_code == 201 and asked.json["status"] == "pending"
    second = add_officer(s)
    ok = approve_reversal(client, second, asked.json["id"], "r1")
    assert ok.status_code == 201
    assert ok.json["type"] == "withdrawal" and ok.json["balance"] == 0
    assert client.post(url, json=why, headers=auth(s.admin)).status_code == 409
    assert client.post(url, json={"reason": "short"},
                       headers=auth(s.admin)).status_code == 400
    other = Setup()
    assert client.post(url, json=why, headers=auth(other.admin)).status_code == 404
