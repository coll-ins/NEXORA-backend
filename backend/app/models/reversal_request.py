"""A reversal one officer asks for and a different officer approves (maker-checker).

Nothing here touches the ledger. Approving appends the opposite ledger entry
through `ledger.reversals`; the request keeps the link to it.
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class ReversalRequest(db.Model):
    __tablename__ = "reversal_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["transaction_id", "sacco_id"],
            ["transactions.id", "transactions.sacco_id"],
            name="fk_reversal_requests_transaction_sacco",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_reversal_requests_status",
        ),
        # The requester cannot approve their own request, whatever the code does.
        CheckConstraint(
            "decided_by IS NULL OR decided_by <> requested_by",
            name="ck_reversal_requests_two_officers",
        ),
        Index(
            "uq_reversal_requests_open",
            "transaction_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id"), nullable=False, index=True
    )
    transaction_id: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_by: Mapped[int] = mapped_column(
        ForeignKey("sacco_memberships.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(140), nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pending", server_default="pending"
    )
    decided_by: Mapped[int | None] = mapped_column(
        ForeignKey("sacco_memberships.id"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reject_reason: Mapped[str | None] = mapped_column(String(140), nullable=True)
    reversal_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
