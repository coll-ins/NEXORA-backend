# ruff: noqa: F811  (pytest fixtures imported from savings_support)
"""Reversal, audit and money-rule tests (Move 3, Neo).

Collins's tests/test_ledger_reversals.py already covers reversing a deposit and
a withdrawal, reversing twice, replaying a reference, overdraw, member 403 and
cross-SACCO 404. This file adds what it does not:

- a reversal of a reversal is refused, and the ledger, the balance and the audit
  trail are unchanged afterwards;
- every financial action writes one audit row with the actor and the target;
- the money rules hold through the HTTP layer: floats, numeric strings,
  booleans, zero, negatives and anything over the maximum are 400.
"""

import pytest
from sqlalchemy import text

from app.audit import log as audit_log
from app.auth.tokens import create_access_token
from app.extensions import db
from app.ledger import reversals
from app.models.audit import AuditLog
from app.savings import service
from app.savings.balance import verify_balance
from app.savings.validation import MAX_TRANSACTION_AMOUNT
from savings_support import (  # noqa: F401
    Setup,
    add_officer,
    approve_reversal,
    ask_for_reversal,
    manual_body,
    release_session,
    schema_app,
)

pytestmark = pytest.mark.schema

REVERSAL_OF_REVERSAL = "A reversal cannot be reversed. Post a new entry instead."


def auth(user):
    token = create_access_token(user.id, user.token_version)
    return {"Authorization": f"Bearer {token}"}


def deposit(s, amount, ref):
    return service.deposit(s.admin.id, s.account.id, amount, ref)


def deposit_url(s):
    return f"/api/v1/savings/accounts/{s.account.id}/deposits"


def reversal_url(transaction_id):
    return f"/api/v1/savings/transactions/{transaction_id}/reversals"


def ledger_rows(account_id):
    """Every ledger row for one account, oldest first, read from the table."""
    db.session.remove()
    rows = db.session.execute(
        text(
            "SELECT id, type, amount, reverses_id FROM transactions "
            "WHERE savings_account_id = :a ORDER BY id"
        ),
        {"a": account_id},
    ).all()
    db.session.remove()
    return [(row.id, row.type, row.amount, row.reverses_id) for row in rows]


def audit_rows(sacco_id):
    """(action, actor_id, target) for one SACCO, oldest first."""
    db.session.expire_all()
    rows = (
        db.session.query(AuditLog)
        .filter_by(sacco_id=sacco_id)
        .order_by(AuditLog.id)
        .all()
    )
    return [(row.action, row.actor_id, row.target) for row in rows]


# ---------- a reversal of a reversal ----------


def test_a_reversal_of_a_reversal_is_refused_by_the_service(schema_app):
    s = Setup()
    original = deposit(s, 500, "d1")
    reversal = reversals.reverse_transaction(s.admin.id, original.id, "r1")

    with pytest.raises(service.Conflict) as refused:
        reversals.reverse_transaction(s.admin.id, reversal.id, "r2")

    assert refused.value.message == REVERSAL_OF_REVERSAL
    assert refused.value.status == 409


def test_the_refusal_leaves_the_ledger_the_balance_and_the_audit_alone(schema_app):
    s = Setup()
    original = deposit(s, 500, "d1")
    reversal = reversals.reverse_transaction(s.admin.id, original.id, "r1")
    before_rows = ledger_rows(s.account.id)
    before_audit = audit_rows(s.sacco.id)

    with pytest.raises(service.Conflict):
        reversals.reverse_transaction(s.admin.id, reversal.id, "r2")

    assert ledger_rows(s.account.id) == before_rows
    assert audit_rows(s.sacco.id) == before_audit
    check = verify_balance(s.account.id)
    assert (check.ledger, check.stored) == (0, 0)
    assert check.consistent


def test_the_endpoint_refuses_a_reversal_of_a_reversal(schema_app):
    client = schema_app.test_client()
    s = Setup()
    original = deposit(s, 200, "d1")

    asked = ask_for_reversal(client, s.admin, original.id)
    first = approve_reversal(client, add_officer(s), asked.json["id"], "r1")
    assert first.status_code == 201
    assert first.get_json()["balance"] == 0
    assert type(first.get_json()["amount"]) is int

    reversed_id = first.get_json()["id"]
    resp = ask_for_reversal(client, s.admin, reversed_id)
    assert resp.status_code == 409
    assert resp.get_json()["error"]["message"] == REVERSAL_OF_REVERSAL
    assert ledger_rows(s.account.id) == [
        (original.id, "deposit", 200, None),
        (reversed_id, "withdrawal", 200, original.id),
    ]


def test_a_retried_reversal_of_a_reversal_is_still_refused(schema_app):
    client = schema_app.test_client()
    s = Setup()
    original = deposit(s, 100, "d1")
    reversal = reversals.reverse_transaction(s.admin.id, original.id, "r1")

    for _ in range(3):
        resp = ask_for_reversal(client, s.admin, reversal.id)
        assert resp.status_code == 409
    assert len(ledger_rows(s.account.id)) == 2


# ---------- the audit trail ----------


def test_every_financial_action_is_audited_with_the_actor_and_target(schema_app):
    s = Setup(balance=300)  # savings.open and savings.deposit
    withdrawal = service.withdraw(s.admin.id, s.account.id, 100, "w1")
    reversal = reversals.reverse_transaction(s.admin.id, withdrawal.id, "r1")

    rows = audit_rows(s.sacco.id)
    assert [action for action, _, _ in rows] == [
        "savings.open",
        "savings.deposit",
        "savings.withdrawal",
        "savings.reverse",
    ]
    assert {actor for _, actor, _ in rows} == {s.admin.id}
    assert all(target for _, _, target in rows)
    assert rows[0][2] == f"savings_account:{s.account.id}"
    assert rows[1][2].startswith(f"savings_account:{s.account.id}/transaction:")
    assert rows[3][2] == (
        f"savings_account:{s.account.id}/transaction:{reversal.id}"
        f"/reverses:{withdrawal.id}"
    )


def test_a_refused_action_writes_no_audit_row(schema_app):
    s = Setup(balance=100)
    before = audit_rows(s.sacco.id)

    with pytest.raises(service.Conflict):
        service.withdraw(s.admin.id, s.account.id, 101, "too-much")

    assert audit_rows(s.sacco.id) == before


def test_record_queues_the_row_and_does_not_commit(schema_app):
    s = Setup()
    before = len(audit_rows(s.sacco.id))

    row = audit_log.record(s.sacco.id, s.admin.id, "savings.test", "target:1")
    db.session.flush()  # visible inside the caller's transaction, id assigned
    assert row.id is not None
    assert len(audit_rows(s.sacco.id)) == before + 1

    db.session.rollback()  # the caller did not commit, so neither did the row
    assert len(audit_rows(s.sacco.id)) == before


@pytest.mark.parametrize(
    "action,target",
    [
        ("", None),
        ("   ", None),
        ("x" * 101, None),
        ("savings.test", "   "),
        ("savings.test", "x" * 256),
    ],
)
def test_record_refuses_a_row_the_column_cannot_hold(schema_app, action, target):
    s = Setup()
    with pytest.raises(ValueError):
        audit_log.record(s.sacco.id, s.admin.id, action, target)


def test_the_savings_service_writes_its_audit_rows_through_audit_log(
    schema_app, monkeypatch
):
    """One writer for the trail: the service must not insert rows by hand."""
    seen = []
    real_record = audit_log.record

    def spy(sacco_id, actor_user_id, action, target=None):
        seen.append(action)
        return real_record(sacco_id, actor_user_id, action, target)

    monkeypatch.setattr(audit_log, "record", spy)
    s = Setup(balance=100)
    service.withdraw(s.admin.id, s.account.id, 10, "w1")

    assert seen == ["savings.open", "savings.deposit", "savings.withdrawal"]


# ---------- money rules through the HTTP layer ----------


@pytest.mark.parametrize(
    "amount",
    [
        1.5,  # float
        100.0,  # whole float, still not an int
        "100",  # numeric string
        "1e3",
        True,  # bool is an int subclass
        False,
        0,
        -5,
        MAX_TRANSACTION_AMOUNT + 1,
        None,
        [100],
        {"value": 100},
    ],
)
def test_a_bad_amount_is_400_and_writes_nothing(schema_app, amount):
    client = schema_app.test_client()
    s = Setup()

    resp = client.post(
        deposit_url(s),
        json=manual_body(amount),
        headers=auth(s.admin),
    )

    assert resp.status_code == 400
    assert ledger_rows(s.account.id) == []


def test_the_kept_messages_are_still_the_ones_clients_see(schema_app):
    client = schema_app.test_client()
    s = Setup()
    headers = auth(s.admin)

    whole = client.post(
        deposit_url(s), json=manual_body(1.5), headers=headers
    )
    assert whole.get_json()["error"]["message"] == (
        "amount must be a whole number of minor units."
    )

    over = client.post(
        deposit_url(s),
        json=manual_body(MAX_TRANSACTION_AMOUNT + 1),
        headers=headers,
    )
    assert over.get_json()["error"]["message"] == (
        f"amount cannot exceed {MAX_TRANSACTION_AMOUNT}."
    )

    zero = client.post(
        deposit_url(s), json=manual_body(0), headers=headers
    )
    assert zero.get_json()["error"]["message"] == "amount must be greater than zero."


def test_the_maximum_is_accepted_and_stays_an_integer(schema_app):
    client = schema_app.test_client()
    s = Setup()

    resp = client.post(
        deposit_url(s),
        json=manual_body(MAX_TRANSACTION_AMOUNT),
        headers=auth(s.admin),
    )

    # The maximum is a large amount, so it waits for a second officer.
    assert resp.status_code == 202
    body = resp.get_json()["request"]
    assert body["amount_minor"] == MAX_TRANSACTION_AMOUNT
    assert type(body["amount_minor"]) is int
    assert "large_amount" in body["flags"]
    assert ledger_rows(s.account.id) == []


def test_a_reference_used_by_a_reversal_cannot_be_replayed_as_a_deposit(schema_app):
    s = Setup(balance=100)
    w = service.withdraw(s.admin.id, s.account.id, 100, "w-once")
    reversals.reverse_transaction(s.admin.id, w.id, "rv-once")
    with pytest.raises(service.Conflict):
        service.deposit(s.admin.id, s.account.id, 100, "rv-once")


def test_database_refuses_a_reversal_of_a_reversal(schema_app):
    s = Setup(balance=100)
    w = service.withdraw(s.admin.id, s.account.id, 100, "w-db")
    rev = reversals.reverse_transaction(s.admin.id, w.id, "rv-db")
    with pytest.raises(Exception, match="a reversal cannot be reversed"):
        db.session.execute(
            text(
                "INSERT INTO transactions (sacco_id, savings_account_id, type, "
                "reference, created_by_membership_id, reverses_id, amount) "
                "VALUES (:s, :a, 'withdrawal', 'raw', :m, :r, 100)"
            ),
            {"s": s.sacco.id, "a": s.account.id,
             "m": s.admin_membership.id, "r": rev.id},
        )
    db.session.rollback()
