"""Where a SACCO's members pay in, and the changes waiting for a second officer.

`sacco_payment_details` holds the live values members see (one row per SACCO).
`sacco_payment_detail_changes` holds every proposed change. A change goes live
only when a different officer approves it, so one person alone can never point
members at a new paybill or bank account.

Numbers are text: a bank account can start with 0.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db

MPESA_TYPES = ("paybill", "till", "pochi")
CHANGE_STATUSES = ("pending", "approved", "superseded", "cancelled")


def value_checks(table: str) -> list[CheckConstraint]:
    """The rules both tables share; the application validates the same things."""
    return [
        CheckConstraint(
            "mpesa_type IN ('paybill', 'till', 'pochi')", name=f"ck_{table}_mpesa_type"
        ),
        CheckConstraint(
            "(mpesa_type IS NULL) = (mpesa_number IS NULL)",
            name=f"ck_{table}_mpesa_pair",
        ),
        CheckConstraint(
            "(mpesa_type = 'pochi' AND mpesa_number ~ '^254[17][0-9]{8}$') OR "
            "(mpesa_type IN ('paybill', 'till') AND mpesa_number ~ '^[0-9]{5,7}$')",
            name=f"ck_{table}_mpesa_number",
        ),
        CheckConstraint(
            "mpesa_number IS NULL OR display_name IS NOT NULL",
            name=f"ck_{table}_mpesa_needs_name",
        ),
        CheckConstraint(
            "account_reference_hint IS NULL OR mpesa_type = 'paybill'",
            name=f"ck_{table}_hint_paybill_only",
        ),
        CheckConstraint(
            "(bank_name IS NULL) = (bank_account_name IS NULL) "
            "AND (bank_name IS NULL) = (bank_account_number IS NULL)",
            name=f"ck_{table}_bank_trio",
        ),
        CheckConstraint(
            "bank_account_number ~ '^[0-9]{6,20}$'",
            name=f"ck_{table}_bank_account_number",
        ),
        CheckConstraint(
            "bank_branch IS NULL OR bank_name IS NOT NULL",
            name=f"ck_{table}_branch_needs_bank",
        ),
        CheckConstraint(
            "bank_paybill_number ~ '^[0-9]{5,7}$'",
            name=f"ck_{table}_bank_paybill_number",
        ),
        CheckConstraint(
            "bank_uses_shared_paybill = (bank_paybill_number IS NOT NULL)",
            name=f"ck_{table}_shared_paybill_pair",
        ),
        CheckConstraint(
            "NOT bank_uses_shared_paybill OR bank_name IS NOT NULL",
            name=f"ck_{table}_shared_paybill_needs_bank",
        ),
        CheckConstraint(
            "mpesa_number IS NOT NULL OR bank_name IS NOT NULL",
            name=f"ck_{table}_one_method",
        ),
    ]


class _Values:
    """The pay-in columns, identical on the live row and on a proposed change."""

    mpesa_type: Mapped[str | None] = mapped_column(String(10))
    mpesa_number: Mapped[str | None] = mapped_column(String(12))
    account_reference_hint: Mapped[str | None] = mapped_column(String(60))
    display_name: Mapped[str | None] = mapped_column(String(60))
    bank_name: Mapped[str | None] = mapped_column(String(60))
    bank_account_name: Mapped[str | None] = mapped_column(String(80))
    bank_account_number: Mapped[str | None] = mapped_column(String(20))
    bank_branch: Mapped[str | None] = mapped_column(String(60))
    bank_uses_shared_paybill: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    bank_paybill_number: Mapped[str | None] = mapped_column(String(7))


class SaccoPaymentDetails(_Values, db.Model):
    __tablename__ = "sacco_payment_details"
    __table_args__ = tuple(value_checks("sacco_payment_details"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id"), nullable=False, unique=True
    )
    # The moment these values went live (the second officer's approval).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # The officer who proposed the values now live.
    updated_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)


class SaccoPaymentDetailsChange(_Values, db.Model):
    __tablename__ = "sacco_payment_detail_changes"
    __table_args__ = (
        *value_checks("sacco_payment_detail_changes"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'superseded', 'cancelled')",
            name="ck_sacco_payment_detail_changes_status",
        ),
        # An approved change replaced or cancelled before it went live keeps its
        # approval but becomes 'superseded' or 'cancelled', so only "approved"
        # requires the pair.
        CheckConstraint(
            "(approved_by IS NULL) = (approved_at IS NULL) "
            "AND (status <> 'approved' OR approved_by IS NOT NULL)",
            name="ck_sacco_payment_detail_changes_approval_pair",
        ),
        CheckConstraint(
            "(approved_at IS NULL) = (go_live_at IS NULL) "
            "AND (go_live_at IS NULL OR go_live_at >= approved_at)",
            name="ck_sacco_payment_detail_changes_go_live",
        ),
        # Two officers, always: the database refuses a self-approval too.
        CheckConstraint(
            "approved_by IS NULL OR approved_by <> proposed_by",
            name="ck_sacco_payment_detail_changes_two_people",
        ),
        # At most one change waits for approval per SACCO.
        Index(
            "uq_sacco_payment_detail_changes_one_pending",
            "sacco_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="pending", server_default="pending"
    )
    proposed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The moment the values went live: the same instant as approved_at.
    go_live_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
