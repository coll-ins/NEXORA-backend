"""Consent-based support access (migration 0023)."""

import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class SupportScope(enum.Enum):
    AUDIT = "audit"
    MEMBERS = "members"
    SAVINGS = "savings"
    LOANS = "loans"


class SupportStatus(enum.Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"
    REVOKED = "revoked"
    EXPIRED = "expired"


def _enum(kind, name):
    return Enum(kind, name=name, values_callable=lambda e: [m.value for m in e])


class SupportAccessRequest(db.Model):
    """A platform owner's request to read one scope of one SACCO's data.

    The database trigger (migration 0023) decides who may move it between
    statuses; the application only reads `approved` rows whose expiry is ahead.
    """

    __tablename__ = "support_access_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["decided_by_membership_id", "sacco_id"],
            ["sacco_memberships.id", "sacco_memberships.sacco_id"],
            name="fk_support_decider_same_sacco",
        ),
        ForeignKeyConstraint(
            ["revoked_by_membership_id", "sacco_id"],
            ["sacco_memberships.id", "sacco_memberships.sacco_id"],
            name="fk_support_revoker_same_sacco",
        ),
        CheckConstraint("btrim(reason) <> ''", name="ck_support_reason_not_blank"),
        CheckConstraint(
            "status <> 'approved' "
            "OR (expires_at IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_support_approved_has_expiry",
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at <= decided_at + interval '24 hours'",
            name="ck_support_at_most_24_hours",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(ForeignKey("saccos.id"), nullable=False)
    requested_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[SupportScope] = mapped_column(
        _enum(SupportScope, "support_scope"), nullable=False
    )
    status: Mapped[SupportStatus] = mapped_column(
        _enum(SupportStatus, "support_status"),
        nullable=False,
        default=SupportStatus.REQUESTED,
        server_default=SupportStatus.REQUESTED.value,
    )
    decided_by_membership_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by_membership_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
