import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class TransactionType(enum.Enum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    INTEREST = "interest"
    FEE = "fee"


class Transaction(db.Model):
    """Append-only ledger.

    Never expose update or delete methods for this model. Rows are never
    changed or removed. The database enforces this with triggers
    (migrations 0001 and 0005), and applies each row to the account balance.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["savings_account_id", "sacco_id"],
            ["savings_accounts.id", "savings_accounts.sacco_id"],
            name="fk_transactions_savings_sacco",
        ),
        UniqueConstraint("id", "sacco_id", name="uq_transactions_id_sacco"),
        # A retried request reuses its reference and cannot post twice.
        UniqueConstraint(
            "sacco_id", "reference", name="uq_transactions_sacco_reference"
        ),
        # The actor must belong to the same SACCO as the row.
        ForeignKeyConstraint(
            ["created_by_membership_id", "sacco_id"],
            ["sacco_memberships.id", "sacco_memberships.sacco_id"],
            name="fk_transactions_created_by_sacco",
        ),
        # A correction points at the row it undoes, in the same SACCO.
        ForeignKeyConstraint(
            ["reverses_id", "sacco_id"],
            ["transactions.id", "transactions.sacco_id"],
            name="fk_transactions_reverses_sacco",
        ),
        CheckConstraint("amount > 0", name="ck_transactions_amount_positive"),
        CheckConstraint(
            "source IN ('standard', 'manual', 'stk')", name="ck_transactions_source"
        ),
        Index("ix_transactions_account_created", "savings_account_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(ForeignKey("saccos.id"), nullable=False)
    savings_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[TransactionType] = mapped_column(
        Enum(
            TransactionType,
            name="transaction_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    reference: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_membership_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Set on a correction: the opposite entry (same account and amount) that undoes
    # one earlier row. A row can be reversed once.
    reverses_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # How the entry came in: `manual` was typed by an officer from an M-Pesa
    # receipt, `stk` was an M-Pesa prompt the member answered. Set at insert only.
    source: Mapped[str] = mapped_column(
        String(10), nullable=False, default="standard", server_default="standard"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
