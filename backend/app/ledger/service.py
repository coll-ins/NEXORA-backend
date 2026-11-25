"""Read-side helpers for the ledger. The ledger is append-only: nothing here
updates or deletes a row."""

from sqlalchemy import select

from app.extensions import db
from app.models.ledger import Transaction
from app.models.savings import SavingsAccount


def get_entry(transaction_id: int) -> Transaction | None:
    return db.session.get(Transaction, transaction_id)


def lock_account(account_id: int) -> SavingsAccount:
    """Lock the account row until the transaction ends, and read it fresh."""
    return db.session.execute(
        select(SavingsAccount)
        .where(SavingsAccount.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()


def reversal_of(transaction_id: int) -> Transaction | None:
    """The entry that undoes `transaction_id`, if there is one."""
    return db.session.execute(
        select(Transaction).where(Transaction.reverses_id == transaction_id)
    ).scalar_one_or_none()
