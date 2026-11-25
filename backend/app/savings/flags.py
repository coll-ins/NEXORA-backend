"""Deposit-control flags: what makes a payment need a second officer.

Nothing here writes. A flagged request is approved by two different officers;
the thresholds are per SACCO (`sacco_deposit_controls`), with safe defaults.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.extensions import db
from app.models.deposit_controls import DEFAULTS, SaccoDepositControls
from app.models.ledger import Transaction, TransactionType
from app.models.payment_request import PaymentRequest
from app.models.savings import SavingsAccount

LABELS = {
    "large_amount": "Large amount",
    "manual_over_limit": "Manual entry over the SACCO's limit",
    "repeated_amount": "Same amount paid again and again",
    "quick_withdrawal": "Withdrawal soon after a deposit",
}


def controls(sacco_id: int) -> dict:
    row = db.session.execute(
        select(SaccoDepositControls).where(SaccoDepositControls.sacco_id == sacco_id)
    ).scalar_one_or_none()
    if row is None:
        return dict(DEFAULTS)
    return {key: getattr(row, key) for key in DEFAULTS}


def split(stored: str | None) -> list[str]:
    return [f for f in (stored or "").split(",") if f]


def evaluate(
    sacco_id: int,
    membership_id: int,
    kind: str,
    amount: int,
    source: str,
    exclude_request_id: int | None = None,
) -> list[str]:
    """The flag codes this payment earns, in a stable order."""
    limits = controls(sacco_id)
    now = datetime.now(UTC)
    flags = []
    if amount >= limits["large_amount_limit"]:
        flags.append("large_amount")
    if source == "manual" and amount >= limits["manual_flag_limit"]:
        flags.append("manual_over_limit")

    since = now - timedelta(hours=limits["repeat_window_hours"])
    same = select(func.count()).where(
        PaymentRequest.sacco_id == sacco_id,
        PaymentRequest.membership_id == membership_id,
        PaymentRequest.kind == kind,
        PaymentRequest.amount_minor == amount,
        PaymentRequest.status != "rejected",
        PaymentRequest.created_at >= since,
    )
    if exclude_request_id is not None:
        same = same.where(PaymentRequest.id != exclude_request_id)
    repeats = db.session.scalar(same)
    if kind == "withdrawal":
        # Withdrawals posted directly leave no request, only a ledger row.
        repeats += db.session.scalar(
            select(func.count())
            .select_from(Transaction)
            .join(SavingsAccount, SavingsAccount.id == Transaction.savings_account_id)
            .where(
                SavingsAccount.membership_id == membership_id,
                SavingsAccount.sacco_id == sacco_id,
                Transaction.type == TransactionType.WITHDRAWAL,
                Transaction.reverses_id.is_(None),
                Transaction.amount == amount,
                Transaction.created_at >= since,
            )
        )
    if repeats + 1 >= limits["repeat_count"]:
        flags.append("repeated_amount")

    # Splitting one large payment into small ones: add up the window.
    total = db.session.scalar(
        select(func.coalesce(func.sum(PaymentRequest.amount_minor), 0)).where(
            PaymentRequest.sacco_id == sacco_id,
            PaymentRequest.membership_id == membership_id,
            PaymentRequest.kind == kind,
            PaymentRequest.status != "rejected",
            PaymentRequest.created_at >= since,
            *(
                [PaymentRequest.id != exclude_request_id]
                if exclude_request_id is not None
                else []
            ),
        )
    )
    if "large_amount" not in flags and total + amount >= limits["large_amount_limit"]:
        flags.append("large_amount")

    if kind == "withdrawal" and limits["quick_withdrawal_hours"]:
        recent = now - timedelta(hours=limits["quick_withdrawal_hours"])
        deposited = db.session.scalar(
            select(func.count())
            .select_from(Transaction)
            .join(SavingsAccount, SavingsAccount.id == Transaction.savings_account_id)
            .where(
                SavingsAccount.membership_id == membership_id,
                SavingsAccount.sacco_id == sacco_id,
                Transaction.type.in_(
                    (TransactionType.DEPOSIT, TransactionType.INTEREST)
                ),
                Transaction.reverses_id.is_(None),
                Transaction.created_at >= recent,
            )
        )
        if deposited:
            flags.append("quick_withdrawal")
    return flags


def join(flags: list[str]) -> str | None:
    return ",".join(flags) or None
