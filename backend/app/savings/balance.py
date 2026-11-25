"""Balances come from the ledger.

The ledger (`transactions`) is the source of truth. `savings_accounts.balance` is
a copy the database keeps in step through a trigger; nothing in the app writes it.
`ledger_balance` recomputes the balance from the rows alone, and
`verify_balance` checks the stored copy against it.
"""

from dataclasses import dataclass

from sqlalchemy import case, func, select

from app.extensions import db
from app.models.ledger import Transaction, TransactionType
from app.models.savings import SavingsAccount

CREDITS = (TransactionType.DEPOSIT, TransactionType.INTEREST)


@dataclass(frozen=True)
class BalanceCheck:
    account_id: int
    ledger: int  # recomputed from the ledger alone
    stored: int  # the trigger-maintained column
    entries: int

    @property
    def consistent(self) -> bool:
        return self.ledger == self.stored


def ledger_balance(account_id: int) -> int:
    """Credits (deposit, interest) minus debits (withdrawal, fee) in minor units."""
    signed = case(
        (Transaction.type.in_(CREDITS), Transaction.amount),
        else_=-Transaction.amount,
    )
    total = db.session.execute(
        select(func.coalesce(func.sum(signed), 0)).where(
            Transaction.savings_account_id == account_id
        )
    ).scalar_one()
    return int(total)


def verify_balance(account_id: int) -> BalanceCheck | None:
    """Compare the stored balance with the ledger. None if the account is missing."""
    account = db.session.get(SavingsAccount, account_id)
    if account is None:
        return None
    db.session.refresh(account)
    entries = db.session.execute(
        select(func.count()).where(Transaction.savings_account_id == account_id)
    ).scalar_one()
    return BalanceCheck(
        account_id, ledger_balance(account_id), account.balance, entries
    )
