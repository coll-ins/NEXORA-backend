# ruff: noqa: F811  (pytest fixtures imported from savings_support)
"""End-of-day status: consistent day, disagreement, and an empty day."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.extensions import db
from app.ledger import reversals
from app.ledger import status as eod
from app.savings import service
from savings_support import Setup, release_session, schema_app  # noqa: F401

pytestmark = pytest.mark.schema

TODAY = datetime.now(UTC).date()


@pytest.fixture
def mailbox(monkeypatch):
    sent = []
    monkeypatch.setattr(eod, "send_text_email", lambda *a: sent.append(a))
    return sent


def test_a_consistent_day_reports_totals_and_emails_the_admin(schema_app, mailbox):
    s = Setup(balance=10_000)
    wd = service.withdraw(s.admin.id, s.account.id, 1_000, "wd-eod")
    reversals.reverse_transaction(s.admin.id, wd.id, "rev-eod")

    status = eod.send_day_status(s.sacco.id, TODAY)

    assert status.ok and status.accounts_checked == 1
    assert (status.entries, status.reversals) == (3, 1)
    # A reversed withdrawal is left out with its reversal, as in the admin summary.
    assert status.deposits_total == 10_000 and status.withdrawals_total == 0
    assert len(mailbox) == 1
    to, subject, body = mailbox[0]
    assert to.startswith("admin") and "(OK)" in subject
    assert "Every stored balance matches" in body


def test_a_stored_balance_that_disagrees_is_named_and_not_repaired(schema_app, mailbox):
    s = Setup(balance=5_000)
    # The trigger normally keeps the balance honest; bypass it to simulate a miss.
    db.session.execute(text("ALTER TABLE savings_accounts DISABLE TRIGGER USER"))
    try:
        db.session.execute(
            text("UPDATE savings_accounts SET balance = 1 WHERE id = :i"),
            {"i": s.account.id},
        )
        db.session.commit()
        status = eod.send_day_status(s.sacco.id, TODAY)
    finally:
        db.session.execute(text("ALTER TABLE savings_accounts ENABLE TRIGGER USER"))
        db.session.commit()

    assert not status.ok
    mismatch = status.mismatches[0]
    assert (mismatch.stored_balance, mismatch.ledger_balance) == (1, 5_000)
    assert f"account {s.account.id}: stored 1, ledger 5000" in mailbox[0][2]
    assert "MISMATCH" in mailbox[0][1]
    balance = db.session.execute(
        text("SELECT balance FROM savings_accounts WHERE id = :i"), {"i": s.account.id}
    ).scalar_one()
    assert balance == 1  # reported, not fixed


def test_a_day_with_no_activity_still_gets_a_status(schema_app, mailbox):
    s = Setup(balance=2_000)
    quiet_day = datetime(2020, 1, 1, tzinfo=UTC).date()

    status = eod.send_day_status(s.sacco.id, quiet_day)

    assert status.entries == 0 and status.ok
    assert "No activity" in mailbox[0][2]
