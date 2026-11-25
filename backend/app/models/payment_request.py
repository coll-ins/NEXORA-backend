"""A member's deposit or withdrawal request (Neo, item 6).

A request changes no balance. It is a message: "I paid this" or "please send me
this". An officer confirms it, and only then does `savings.service` post the real
ledger entry, so every ledger rule stays in one place.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db

KINDS = ("deposit", "withdrawal")
METHODS = ("mpesa", "bank")
STATUSES = ("pending", "confirmed", "rejected")


class PaymentRequest(db.Model):
    __tablename__ = "payment_requests"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('deposit', 'withdrawal')", name="ck_payment_requests_kind"
        ),
        CheckConstraint(
            "method IN ('mpesa', 'bank')", name="ck_payment_requests_method"
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_payment_requests_status",
        ),
        CheckConstraint(
            "amount_minor > 0", name="ck_payment_requests_amount_positive"
        ),
        UniqueConstraint("reference", name="uq_payment_requests_reference"),
        CheckConstraint(
            "source IN ('member', 'stk', 'manual')", name="ck_payment_requests_source"
        ),
        # One M-Pesa receipt is one payment in a SACCO, however it was recorded.
        Index(
            "uq_payment_requests_sacco_receipt",
            "sacco_id",
            "mpesa_receipt",
            unique=True,
            postgresql_where=text("mpesa_receipt IS NOT NULL"),
        ),
        # The officer who gives the first approval cannot also give the second.
        CheckConstraint(
            "first_approved_by IS NULL OR decided_by IS NULL "
            "OR first_approved_by <> decided_by",
            name="ck_payment_requests_two_officers",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id"), nullable=False, index=True
    )
    membership_id: Mapped[int] = mapped_column(
        ForeignKey("sacco_memberships.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # `2547XXXXXXXX` or `2541XXXXXXXX`, required for mpesa.
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    account_ref: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # The client's idempotency key: a retry returns the original row.
    reference: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="pending", server_default="pending"
    )
    decided_by: Mapped[int | None] = mapped_column(
        ForeignKey("sacco_memberships.id"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # The ledger row a confirmation posted; NULL until confirmed.
    ledger_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )
    # member: the member asked; stk: an M-Pesa prompt was paid; manual: an officer
    # typed in a receipt.
    source: Mapped[str] = mapped_column(
        String(10), nullable=False, default="member", server_default="member"
    )
    mpesa_receipt: Mapped[str | None] = mapped_column(String(20), nullable=True)
    stk_push_id: Mapped[int | None] = mapped_column(
        ForeignKey("stk_pushes.id"), nullable=True
    )
    # Comma-separated reasons from `savings.flags`; empty means unflagged. A
    # flagged request needs two different officers.
    flags: Mapped[str | None] = mapped_column(String(120), nullable=True)
    first_approved_by: Mapped[int | None] = mapped_column(
        ForeignKey("sacco_memberships.id"), nullable=True
    )
    first_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
