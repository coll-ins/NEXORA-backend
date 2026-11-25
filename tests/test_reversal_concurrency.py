# ruff: noqa: F811  (pytest fixtures imported from savings_support)
"""Reversals racing withdrawals on one account (Move 3).

reverse_transaction takes the same SELECT ... FOR UPDATE lock on the account row
as deposits and withdrawals, so a reversal and a withdrawal cannot interleave.
These tests release the requests together, each on its own connection, and check
what must stay true whichever one wins:

- the balance never goes negative, and the stored balance equals the ledger;
- a refused request leaves no ledger row and no audit row behind;
- one entry is reversed at most once, even when two reversals race.
"""

import pytest
from sqlalchemy import text

from app.extensions import db
from app.ledger.reversals import ReversalView, reverse_transaction
from app.savings import service
from savings_support import Setup, release_session, schema_app  # noqa: F401
from test_savings_concurrency import balance_and_ledger, ledger_rows, run_together

pytestmark = pytest.mark.schema


def tx_id(account_id, reference):
    """The ledger id of a posted entry, looked up by its reference."""
    db.session.remove()
    found = db.session.execute(
        text(
            "SELECT id FROM transactions WHERE savings_account_id = :a"
            " AND reference = :r"
        ),
        {"a": account_id, "r": reference},
    ).scalar_one()
    db.session.remove()
    return found


def audit_rows(account_id):
    """Audit rows naming this account (its open, deposits, withdrawals, reversals)."""
    db.session.remove()
    count = db.session.execute(
        text("SELECT count(*) FROM audit_logs WHERE target = :t OR target LIKE :p"),
        {"t": f"savings_account:{account_id}", "p": f"savings_account:{account_id}/%"},
    ).scalar_one()
    db.session.remove()
    return count


def reverse(s, original_id, reference):
    return lambda: reverse_transaction(s.admin.id, original_id, reference)


def withdraw(s, amount, reference):
    return lambda: service.withdraw(s.admin.id, s.account.id, amount, reference)


def test_a_reversal_and_a_withdrawal_that_together_overdraw_cannot_both_succeed(
    schema_app,
):
    # 100 in the account. Reversing the 100 deposit takes 100 out; so does the
    # withdrawal of 70 together with it (170 > 100). Whichever lock wins, the
    # other must see the new balance and be refused.
    s = Setup(balance=100)
    deposit_id = tx_id(s.account.id, next_setup_reference(s))
    rows_before, audit_before = ledger_rows(s.account.id), audit_rows(s.account.id)

    results = run_together(
        schema_app, [reverse(s, deposit_id, "rev-1"), withdraw(s, 70, "w-1")]
    )

    posted = ReversalView | service.PostedTransaction
    done = [r for r in results if isinstance(r, posted)]
    refused = [r for r in results if isinstance(r, service.SavingsError)]
    assert len(done) == 1 and len(refused) == 1
    assert isinstance(refused[0], service.Conflict)
    assert "Insufficient funds" in refused[0].message

    stored, rebuilt = balance_and_ledger(s.account.id)
    assert stored == rebuilt >= 0
    assert stored in (0, 30)  # reversal won -> 0, withdrawal won -> 30
    # Only the winner wrote a ledger row and an audit row.
    assert ledger_rows(s.account.id) == rows_before + 1
    assert audit_rows(s.account.id) == audit_before + 1


def test_a_reversal_and_a_withdrawal_that_both_fit_both_succeed(schema_app):
    # 200 in the account: reversing a 100 deposit and withdrawing 100 fit in
    # either order, so the lock must not turn one of them away.
    s = Setup(balance=100)
    first_deposit = tx_id(s.account.id, next_setup_reference(s))
    service.deposit(s.admin.id, s.account.id, 100, "second-deposit")
    db.session.remove()

    results = run_together(
        schema_app, [reverse(s, first_deposit, "rev-1"), withdraw(s, 100, "w-1")]
    )

    assert not [r for r in results if isinstance(r, Exception)]
    assert balance_and_ledger(s.account.id) == (0, 0)
    assert ledger_rows(s.account.id) == 4  # 2 deposits, 1 reversal, 1 withdrawal


def test_many_reversals_and_withdrawals_never_take_the_balance_below_zero(schema_app):
    # Five deposits of 10 = 50. Five reversals and five withdrawals each want 10,
    # so only five of the ten can succeed, and the account ends at exactly 0.
    s = Setup(balance=0)
    originals = []
    for i in range(5):
        service.deposit(s.admin.id, s.account.id, 10, f"d-{i}")
        originals.append(tx_id(s.account.id, f"d-{i}"))
    db.session.remove()

    jobs = [reverse(s, tx, f"rev-{i}") for i, tx in enumerate(originals)]
    jobs += [withdraw(s, 10, f"w-{i}") for i in range(5)]
    results = run_together(schema_app, jobs)

    done = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, service.SavingsError)]
    assert len(done) == 5 and len(refused) == 5
    assert all("Insufficient funds" in r.message for r in refused)
    assert balance_and_ledger(s.account.id) == (0, 0)
    assert ledger_rows(s.account.id) == 5 + 5  # 5 deposits + 5 winners only


def test_two_reversals_of_the_same_entry_with_different_references_reverse_it_once(
    schema_app,
):
    s = Setup(balance=100)
    original = tx_id(s.account.id, next_setup_reference(s))
    rows_before = ledger_rows(s.account.id)

    results = run_together(
        schema_app, [reverse(s, original, f"rev-{i}") for i in range(4)]
    )

    done = [r for r in results if isinstance(r, ReversalView)]
    refused = [r for r in results if isinstance(r, service.SavingsError)]
    assert len(done) == 1 and len(refused) == 3
    assert all("already been reversed" in r.message for r in refused)
    assert balance_and_ledger(s.account.id) == (0, 0)
    assert ledger_rows(s.account.id) == rows_before + 1


def test_a_retried_reversal_racing_itself_posts_once(schema_app):
    s = Setup(balance=100)
    original = tx_id(s.account.id, next_setup_reference(s))

    results = run_together(
        schema_app, [reverse(s, original, "same-reference") for _ in range(6)]
    )

    assert all(isinstance(r, ReversalView) for r in results)
    assert sum(not r.replayed for r in results) == 1
    assert len({r.id for r in results}) == 1
    assert balance_and_ledger(s.account.id) == (0, 0)


def next_setup_reference(s):
    """Setup funds the account with one deposit whose reference starts 'setup-'."""
    db.session.remove()
    reference = db.session.execute(
        text(
            "SELECT reference FROM transactions "
            "WHERE savings_account_id = :a AND reference LIKE 'setup-%'"
        ),
        {"a": s.account.id},
    ).scalar_one()
    db.session.remove()
    return reference
