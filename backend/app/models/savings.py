from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class SavingsAccount(db.Model):
    """One savings account per membership. Balance is in integer minor units.

    Balance is owned by the database: a trigger applies each ledger row to it,
    and direct changes are rejected. Never write to it from the app.
    """

    __tablename__ = "savings_accounts"
    __table_args__ = (
        # One savings account per membership.
        UniqueConstraint("membership_id", name="uq_savings_account_membership"),
        # Lets the ledger (transactions) reference (id, sacco_id) together.
        UniqueConstraint("id", "sacco_id", name="uq_savings_account_id_sacco"),
        # The account's SACCO must match its membership's SACCO.
        ForeignKeyConstraint(
            ["membership_id", "sacco_id"],
            ["sacco_memberships.id", "sacco_memberships.sacco_id"],
            name="fk_savings_membership_sacco",
        ),
        CheckConstraint(
            "balance >= 0", name="ck_savings_accounts_balance_non_negative"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    membership_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id"), nullable=False, index=True
    )
    balance: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
