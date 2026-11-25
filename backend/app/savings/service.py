"""Savings accounts, deposits, withdrawals, interest and fees.

Every money movement is one database transaction: lock the account row, check
the rules, insert the ledger row, write the audit row, commit. The database
applies the ledger row to the balance (trigger in migrations 0005/0016), so this
code never writes a balance.
"""

import functools
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.audit import log as audit_log
from app.auth.suspension import SUSPENDED_MESSAGE
from app.errors import DUPLICATE_REFERENCE, INSUFFICIENT_FUNDS, NOT_ALLOWED, NOT_FOUND
from app.extensions import db
from app.ledger.service import lock_account
from app.models.ledger import Transaction, TransactionType
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.sacco import Sacco, SaccoStatus
from app.models.savings import SavingsAccount
from app.models.user import User
from app.security.permissions import can

DEBIT_TYPES = (TransactionType.WITHDRAWAL, TransactionType.FEE)


class SavingsError(Exception):
    """A refused request. `status` and the message are safe to show the client."""

    status = 400
    code: str | None = None

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotFound(SavingsError):
    # Also used for another SACCO's record, so its existence is never revealed.
    status = 404
    code = NOT_FOUND


class Forbidden(SavingsError):
    status = 403
    code = NOT_ALLOWED


class Conflict(SavingsError):
    status = 409


class Suspended(SavingsError):
    status = 423


@dataclass(frozen=True)
class AccountView:
    id: int
    membership_id: int
    sacco_id: int
    balance: int
    created_at: datetime


@dataclass(frozen=True)
class PostedTransaction:
    id: int
    account_id: int
    type: str
    amount: int
    reference: str
    balance: int  # the account balance now (right after the entry when it is new)
    created_at: datetime
    replayed: bool  # True when this reference had already been posted


def _atomic(fn):
    """Roll everything back if the operation fails, so no half-done work survives."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            db.session.rollback()
            raise

    return wrapper


def _constraint(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def _actor(
    user_id: int, sacco_id: int, permission: str = "savings.post"
) -> SaccoMembership:
    """The caller's active membership here, if it holds `permission`.

    Decided by `can()` alone (job titles), never by the legacy role column: a
    "staff" membership holding the secretary title cannot move money.
    """
    actor = db.session.execute(
        select(SaccoMembership).where(
            SaccoMembership.user_id == user_id, SaccoMembership.sacco_id == sacco_id
        )
    ).scalar_one_or_none()
    if actor is None:
        raise NotFound("Not found.")
    user = db.session.get(User, user_id)
    if actor.status is not MembershipStatus.ACTIVE or not can(
        user, sacco_id, permission
    ):
        raise Forbidden("You are not allowed to do this.")
    return actor


def _require_not_own_account(actor: SaccoMembership, account) -> None:
    """Nobody posts or reverses money on their own account (separation of duties)."""
    if account.membership_id == actor.id:
        raise Forbidden("You cannot post money on your own account.")


def _was_reversed(transaction_id: int) -> bool:
    return db.session.execute(
        select(Transaction.id).where(Transaction.reverses_id == transaction_id)
    ).first() is not None


def _is_loan_entry(transaction_id: int) -> bool:
    from app.ledger.reversals import _is_loan_entry as is_loan

    return is_loan(transaction_id)


def _require_sacco_active(sacco_id: int) -> None:
    # A suspension must also stop access tokens that were issued before it.
    sacco = db.session.get(Sacco, sacco_id)
    if sacco is None or sacco.status is SaccoStatus.SUSPENDED:
        raise Suspended(SUSPENDED_MESSAGE)


@_atomic
def open_account(user_id: int, membership_id: int) -> AccountView:
    target = db.session.get(SaccoMembership, membership_id)
    if target is None:
        raise NotFound("Not found.")
    _actor(user_id, target.sacco_id)
    _require_sacco_active(target.sacco_id)
    if target.status is not MembershipStatus.ACTIVE:
        raise Conflict("The membership is not active.")

    account = SavingsAccount(membership_id=target.id, sacco_id=target.sacco_id)
    db.session.add(account)
    try:
        db.session.flush()
    except IntegrityError as exc:
        if _constraint(exc) == "uq_savings_account_membership":
            raise Conflict("This member already has a savings account.") from exc
        raise
    db.session.refresh(account)
    audit_log.record(
        account.sacco_id, user_id, "savings.open", f"savings_account:{account.id}"
    )
    view = AccountView(
        account.id,
        account.membership_id,
        account.sacco_id,
        account.balance,
        account.created_at,
    )
    db.session.commit()
    return view


@_atomic
def _post(
    user_id: int | None,
    account_id: int,
    kind: TransactionType,
    amount: int,
    reference: str,
    source: str = "standard",
    system: bool = False,
) -> PostedTransaction:
    """`system` is for a deposit M-Pesa itself confirmed (an auto-confirmed STK
    payment): there is no officer, so the holder's own membership is recorded
    as the actor and the audit row has no user."""
    account = db.session.get(SavingsAccount, account_id)
    if account is None:
        raise NotFound("Not found.")
    if system:
        actor = db.session.get(SaccoMembership, account.membership_id)
    else:
        actor = _actor(user_id, account.sacco_id)

    if kind is TransactionType.WITHDRAWAL:
        # SACCO lock before the account lock, the order a loan payout uses too.
        from app.saccos.rules import lock_sacco  # rules imports this module

        lock_sacco(account.sacco_id)

    # Lock the account row. A second request for this account waits here until
    # the first commits, then reads the new balance, so two withdrawals that
    # together exceed the balance cannot both succeed.
    account = lock_account(account_id)

    if not system:
        _require_not_own_account(actor, account)
    _require_sacco_active(account.sacco_id)
    holder = db.session.get(SaccoMembership, account.membership_id)
    if holder.status is not MembershipStatus.ACTIVE:
        raise Conflict("The account holder's membership is not active.")

    existing = db.session.execute(
        select(Transaction).where(
            Transaction.sacco_id == account.sacco_id,
            Transaction.reference == reference,
        )
    ).scalar_one_or_none()
    # A retry of the same request returns the original entry instead of posting
    # twice. This comes before the balance check, so a withdrawal that already
    # went through is not refused on retry because the balance has dropped since.
    if existing is not None:
        same = (existing.savings_account_id, existing.type, existing.amount) == (
            account.id,
            kind,
            amount,
        )
        reused = existing.reverses_id is not None or _is_loan_entry(existing.id)
        # A retry of an entry that was reversed since must not look like success.
        if not same or reused or _was_reversed(existing.id):
            raise Conflict(
                "That reference has already been used.", code=DUPLICATE_REFERENCE
            )
        view = PostedTransaction(
            existing.id,
            account.id,
            existing.type.value,
            existing.amount,
            existing.reference,
            account.balance,
            existing.created_at,
            replayed=True,
        )
        db.session.rollback()  # nothing to write; release the lock
        return view

    if kind in DEBIT_TYPES and amount > account.balance:
        raise Conflict("Insufficient funds.", code=INSUFFICIENT_FUNDS)
    if kind is TransactionType.WITHDRAWAL:
        from app.saccos.rules import check_withdrawal  # rules imports this module

        check_withdrawal(account, amount)

    tx = Transaction(
        sacco_id=account.sacco_id,
        savings_account_id=account.id,
        type=kind,
        amount=amount,
        reference=reference,
        created_by_membership_id=actor.id,
        source=source,
    )
    db.session.add(tx)
    try:
        db.session.flush()  # the database trigger applies the row to the balance
    except IntegrityError as exc:
        name = _constraint(exc)
        if name == "uq_transactions_sacco_reference":
            raise Conflict(
                "That reference has already been used.", code=DUPLICATE_REFERENCE
            ) from exc
        if name == "ck_savings_accounts_balance_non_negative":
            raise Conflict("Insufficient funds.", code=INSUFFICIENT_FUNDS) from exc
        raise
    db.session.refresh(tx)
    db.session.refresh(account)

    audit_log.record(
        account.sacco_id,
        user_id,
        f"savings.{kind.value}",
        f"savings_account:{account.id}/transaction:{tx.id}",
    )
    view = PostedTransaction(
        tx.id,
        account.id,
        tx.type.value,
        tx.amount,
        tx.reference,
        account.balance,
        tx.created_at,
        replayed=False,
    )
    db.session.commit()
    return view


def deposit(
    user_id: int | None,
    account_id: int,
    amount: int,
    reference: str,
    source: str = "standard",
    system: bool = False,
):
    return _post(
        user_id, account_id, TransactionType.DEPOSIT, amount, reference, source, system
    )


def withdraw(user_id: int, account_id: int, amount: int, reference: str):
    return _post(user_id, account_id, TransactionType.WITHDRAWAL, amount, reference)


def post_interest(user_id: int, account_id: int, amount: int, reference: str):
    return _post(user_id, account_id, TransactionType.INTEREST, amount, reference)


def post_fee(user_id: int, account_id: int, amount: int, reference: str):
    return _post(user_id, account_id, TransactionType.FEE, amount, reference)
