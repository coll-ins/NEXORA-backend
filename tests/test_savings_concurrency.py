# ruff: noqa: F811  (pytest fixtures imported from savings_support)
"""Double-spend and concurrent-withdrawal tests (Move 3).

Each worker thread has its own app context, so its own session and its own
database connection. A barrier releases them together so the requests overlap.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from sqlalchemy import text

from app.extensions import db
from app.models.audit import AuditLog
from app.savings import service
from savings_support import Setup, release_session, schema_app  # noqa: F401

pytestmark = pytest.mark.schema


def run_together(app, jobs):
    """Run every job at the same moment, each on its own connection."""
    barrier = threading.Barrier(len(jobs))

    def run(job):
        with app.app_context():
            barrier.wait(timeout=10)
            try:
                return job()
            except service.SavingsError as exc:
                return exc
            finally:
                db.session.remove()

    with ThreadPoolExecutor(len(jobs)) as pool:
        return list(pool.map(run, jobs))


def balance_and_ledger(account_id):
    """The stored balance and the balance rebuilt from the ledger alone."""
    db.session.remove()
    stored = db.session.execute(
        text("SELECT balance FROM savings_accounts WHERE id = :id"), {"id": account_id}
    ).scalar_one()
    rebuilt = db.session.execute(
        text(
            "SELECT COALESCE(SUM(CASE type WHEN 'deposit' "
            "THEN amount ELSE -amount END), 0) "
            "FROM transactions WHERE savings_account_id = :id"
        ),
        {"id": account_id},
    ).scalar_one()
    db.session.remove()
    return stored, rebuilt


def ledger_rows(account_id):
    rows = db.session.execute(
        text("SELECT count(*) FROM transactions WHERE savings_account_id = :id"),
        {"id": account_id},
    ).scalar_one()
    db.session.remove()
    return rows


def test_two_withdrawals_that_together_exceed_the_balance_cannot_both_succeed(
    schema_app,
):
    s = Setup(balance=100)
    jobs = [
        lambda i=i: service.withdraw(s.admin.id, s.account.id, 70, f"w-{i}")
        for i in range(2)
    ]
    results = run_together(schema_app, jobs)

    done = [r for r in results if isinstance(r, service.PostedTransaction)]
    refused = [r for r in results if isinstance(r, service.SavingsError)]
    assert len(done) == 1 and len(refused) == 1
    assert refused[0].message == "Insufficient funds."
    assert balance_and_ledger(s.account.id) == (30, 30)


def test_many_withdrawals_never_overdraw(schema_app):
    s = Setup(balance=100)
    jobs = [
        lambda i=i: service.withdraw(s.admin.id, s.account.id, 30, f"w-{i}")
        for i in range(10)
    ]
    results = run_together(schema_app, jobs)

    done = [r for r in results if isinstance(r, service.PostedTransaction)]
    assert len(done) == 3  # 3 x 30 fits in 100, a 4th would not
    assert all(isinstance(r, service.SavingsError) for r in results if r not in done)
    assert balance_and_ledger(s.account.id) == (10, 10)


def test_parallel_deposits_are_all_counted(schema_app):
    s = Setup(balance=0)
    jobs = [
        lambda i=i: service.deposit(s.admin.id, s.account.id, 5, f"d-{i}")
        for i in range(8)
    ]
    results = run_together(schema_app, jobs)

    assert all(isinstance(r, service.PostedTransaction) for r in results)
    assert balance_and_ledger(s.account.id) == (40, 40)
    assert ledger_rows(s.account.id) == 8


def test_a_retry_racing_itself_posts_once(schema_app):
    s = Setup(balance=100)
    jobs = [
        lambda: service.withdraw(s.admin.id, s.account.id, 40, "same-reference")
        for _ in range(6)
    ]
    results = run_together(schema_app, jobs)

    assert all(isinstance(r, service.PostedTransaction) for r in results)
    assert sum(not r.replayed for r in results) == 1
    assert len({r.id for r in results}) == 1
    assert balance_and_ledger(s.account.id) == (60, 60)


def test_second_withdrawal_waits_for_the_first_and_then_sees_the_new_balance(
    schema_app,
):
    """Deterministic double-spend: connection A holds the account while B asks.

    A locks the account and posts a withdrawal of 70 but has not committed yet.
    B asks for 70 too. B must wait for A, then read the balance A left (30) and
    be refused. Without the row lock B would read 100 and both would succeed.
    """
    s = Setup(balance=100)
    url = db.engine.url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )

    with psycopg.connect(url) as conn_a:
        conn_a.execute(
            "SELECT id FROM savings_accounts WHERE id = %s FOR UPDATE", (s.account.id,)
        )
        conn_a.execute(
            "INSERT INTO transactions (sacco_id, savings_account_id, type, amount, "
            "reference, created_by_membership_id) "
            "VALUES (%s, %s, 'withdrawal', 70, 'a-first', %s)",
            (s.sacco.id, s.account.id, s.admin_membership.id),
        )

        def b():
            with schema_app.app_context():
                try:
                    return service.withdraw(s.admin.id, s.account.id, 70, "b-second")
                except service.SavingsError as exc:
                    return exc
                finally:
                    db.session.remove()

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(b)
            with pytest.raises(TimeoutError):
                future.result(timeout=1)  # B is blocked on A's lock
            conn_a.commit()
            result = future.result(timeout=10)

    assert isinstance(result, service.SavingsError)
    assert result.message == "Insufficient funds."
    assert balance_and_ledger(s.account.id) == (30, 30)


def test_balance_can_be_rebuilt_from_the_ledger_alone(schema_app):
    s = Setup(balance=500)
    service.withdraw(s.admin.id, s.account.id, 120, "r-1")
    service.deposit(s.admin.id, s.account.id, 45, "r-2")
    service.withdraw(s.admin.id, s.account.id, 5, "r-3")

    assert balance_and_ledger(s.account.id) == (420, 420)


def test_every_money_movement_writes_an_audit_row_with_the_actor(schema_app):
    s = Setup(balance=0)
    service.deposit(s.admin.id, s.account.id, 100, "a-1")
    service.withdraw(s.admin.id, s.account.id, 40, "a-2")

    rows = (
        db.session.query(AuditLog)
        .filter(AuditLog.sacco_id == s.sacco.id)
        .order_by(AuditLog.id)
        .all()
    )
    assert [r.action for r in rows] == [
        "savings.open",
        "savings.deposit",
        "savings.withdrawal",
    ]
    assert {r.actor_id for r in rows} == {s.admin.id}


def test_a_refused_withdrawal_leaves_no_ledger_or_audit_row(schema_app):
    s = Setup(balance=50)
    before = ledger_rows(s.account.id)
    audits = db.session.query(AuditLog).filter_by(sacco_id=s.sacco.id).count()
    db.session.remove()

    with pytest.raises(service.Conflict):
        service.withdraw(s.admin.id, s.account.id, 51, "too-much")

    assert ledger_rows(s.account.id) == before
    assert db.session.query(AuditLog).filter_by(sacco_id=s.sacco.id).count() == audits
