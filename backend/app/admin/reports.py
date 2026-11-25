"""SACCO-scoped reports. Read-only, and every query filters by sacco_id.

Reversals: a reversal is an ordinary ledger row that points at the entry it
cancels (`reverses_id`). Deposit and withdrawal totals leave out both the
reversal rows and the originals they cancelled, so they show what stood.
Reversals are counted separately as `reversal_count`.
"""

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import aliased

from app.extensions import db
from app.models.ledger import Transaction, TransactionType
from app.models.membership import SaccoMembership
from app.models.savings import SavingsAccount
from app.models.user import User


def _bounds(start: date | None, end: date | None):
    """Inclusive date range as [lo, hi) datetimes in UTC, either may be None."""
    lo = datetime.combine(start, time.min, tzinfo=UTC) if start else None
    # date.max has no next day: leave the upper end open instead of overflowing.
    hi = (
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
        if end and end < date.max
        else None
    )
    return lo, hi


def sacco_summary(sacco_id: int, start: date | None, end: date | None) -> dict:
    lo, hi = _bounds(start, end)

    total_balance = db.session.execute(
        select(func.coalesce(func.sum(SavingsAccount.balance), 0)).where(
            SavingsAccount.sacco_id == sacco_id
        )
    ).scalar_one()
    accounts = db.session.execute(
        select(func.count())
        .select_from(SavingsAccount)
        .where(SavingsAccount.sacco_id == sacco_id)
    ).scalar_one()

    reversal = aliased(Transaction)
    in_range = [Transaction.sacco_id == sacco_id]
    if lo is not None:
        in_range.append(Transaction.created_at >= lo)
    if hi is not None:
        in_range.append(Transaction.created_at < hi)

    def total(kind: TransactionType) -> int:
        stmt = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            *in_range,
            Transaction.type == kind,
            Transaction.reverses_id.is_(None),
            ~select(reversal.id)
            .where(
                and_(
                    reversal.sacco_id == sacco_id,
                    reversal.reverses_id == Transaction.id,
                )
            )
            .exists(),
        )
        return int(db.session.execute(stmt).scalar_one())

    reversal_count = db.session.execute(
        select(func.count())
        .select_from(Transaction)
        .where(*in_range, Transaction.reverses_id.is_not(None))
    ).scalar_one()

    return {
        "sacco_id": sacco_id,
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        "account_count": int(accounts),
        "total_balance": int(total_balance),
        "deposit_total": total(TransactionType.DEPOSIT),
        "withdrawal_total": total(TransactionType.WITHDRAWAL),
        "reversal_count": int(reversal_count),
    }


def member_activity(sacco_id: int, page: int, per_page: int) -> list[dict]:
    """Per account holder: transaction count and last transaction time, paged."""
    rows = db.session.execute(
        select(
            SaccoMembership.id,
            User.email,
            SavingsAccount.id,
            func.count(Transaction.id),
            func.max(Transaction.created_at),
        )
        .select_from(SavingsAccount)
        .join(SaccoMembership, SaccoMembership.id == SavingsAccount.membership_id)
        .join(User, User.id == SaccoMembership.user_id)
        .outerjoin(
            Transaction,
            and_(
                Transaction.savings_account_id == SavingsAccount.id,
                Transaction.sacco_id == sacco_id,
            ),
        )
        .where(
            SavingsAccount.sacco_id == sacco_id,
            SaccoMembership.sacco_id == sacco_id,
        )
        .group_by(SaccoMembership.id, User.email, SavingsAccount.id)
        .order_by(SaccoMembership.id)
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()
    return [
        {
            "membership_id": m_id,
            "email": email,
            "account_id": acc_id,
            "transaction_count": count,
            "last_transaction_at": last.isoformat() if last else None,
        }
        for m_id, email, acc_id, count, last in rows
    ]
