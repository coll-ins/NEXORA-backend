"""Nightly balance reconciliation: compares every account's stored balance
against the sum of its ledger rows in `transactions`."""

from dataclasses import dataclass

import click
from flask.cli import with_appcontext
from sqlalchemy import case, func, select

from app.audit import log as audit_log
from app.extensions import db
from app.models.ledger import Transaction, TransactionType
from app.models.savings import SavingsAccount

CREDITS = (TransactionType.DEPOSIT, TransactionType.INTEREST)


@dataclass(frozen=True)
class BalanceMismatch:
    account_id: int
    sacco_id: int
    stored_balance: int
    ledger_balance: int


@dataclass(frozen=True)
class ReconciliationReport:
    accounts_checked: int
    mismatches: list[BalanceMismatch]

    @property
    def ok(self) -> bool:
        return len(self.mismatches) == 0


def run_nightly_reconciliation(sacco_id: int | None = None) -> ReconciliationReport:
    """Compare stored `savings_accounts.balance` with ledger sum across all accounts."""
    signed_amount = case(
        (Transaction.type.in_(CREDITS), Transaction.amount),
        else_=-Transaction.amount,
    )
    ledger_subq = (
        select(
            Transaction.savings_account_id.label("account_id"),
            func.coalesce(func.sum(signed_amount), 0).label("ledger_sum"),
        )
        .group_by(Transaction.savings_account_id)
        .subquery()
    )

    stmt = select(
        SavingsAccount.id,
        SavingsAccount.sacco_id,
        SavingsAccount.balance,
        func.coalesce(ledger_subq.c.ledger_sum, 0).label("ledger_sum"),
    ).outerjoin(ledger_subq, ledger_subq.c.account_id == SavingsAccount.id)

    if sacco_id is not None:
        stmt = stmt.where(SavingsAccount.sacco_id == sacco_id)

    rows = db.session.execute(stmt.order_by(SavingsAccount.id)).all()
    mismatches = [
        BalanceMismatch(
            account_id=r.id,
            sacco_id=r.sacco_id,
            stored_balance=int(r.balance),
            ledger_balance=int(r.ledger_sum),
        )
        for r in rows
        if int(r.balance) != int(r.ledger_sum)
    ]

    audit_log.record(
        sacco_id,
        None,
        "ledger.reconcile",
        f"checked:{len(rows)}/mismatches:{len(mismatches)}",
    )
    db.session.commit()
    return ReconciliationReport(accounts_checked=len(rows), mismatches=mismatches)


@click.command("reconcile-balances")
@click.option("--sacco-id", type=int, default=None, help="Limit check to one SACCO.")
@with_appcontext
def reconcile_command(sacco_id: int | None):
    """Run nightly ledger vs stored balance check."""
    report = run_nightly_reconciliation(sacco_id=sacco_id)
    click.echo(
        f"Checked {report.accounts_checked} accounts; "
        f"mismatches={len(report.mismatches)}"
    )
    if not report.ok:
        for m in report.mismatches:
            click.echo(
                f"MISMATCH account={m.account_id} sacco={m.sacco_id} "
                f"stored={m.stored_balance} ledger={m.ledger_balance}",
                err=True,
            )
        raise SystemExit(1)
