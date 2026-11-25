"""One STK push asked of M-Pesa. Records the answer; a deposit is made separately."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class StkPush(db.Model):
    __tablename__ = "stk_pushes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["membership_id", "sacco_id"],
            ["sacco_memberships.id", "sacco_memberships.sacco_id"],
            name="fk_stk_pushes_membership_sacco",
        ),
        CheckConstraint("amount > 0", name="ck_stk_pushes_amount_positive"),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')",
            name="ck_stk_pushes_status",
        ),
        Index(
            "uq_stk_pushes_checkout",
            "checkout_request_id",
            unique=True,
            postgresql_where=text("checkout_request_id IS NOT NULL"),
        ),
        Index(
            "uq_stk_pushes_receipt",
            "mpesa_receipt",
            unique=True,
            postgresql_where=text("mpesa_receipt IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    membership_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(12), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # minor units
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pending", server_default="pending"
    )
    checkout_request_id: Mapped[str | None] = mapped_column(String(64))
    result_desc: Mapped[str | None] = mapped_column(String(255))
    mpesa_receipt: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
