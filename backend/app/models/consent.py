"""Consent to keep a member's personal data (Data Protection Act 2019).

One row per grant. Withdrawing sets `withdrawn_at`; the row stays as the record
that consent was given and when it ended. Granting again adds a new row.
"""

from datetime import datetime

from sqlalchemy import (
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

PURPOSES = ("member_profile",)


class MemberDataConsent(db.Model):
    __tablename__ = "member_data_consents"
    __table_args__ = (
        CheckConstraint("purpose IN ('member_profile')", name="ck_consents_purpose"),
        CheckConstraint(
            "withdrawn_at IS NULL OR withdrawn_at >= granted_at",
            name="ck_consents_withdrawn_after_granted",
        ),
        # At most one live consent per member and purpose.
        Index(
            "uq_consents_one_active",
            "membership_id",
            "purpose",
            unique=True,
            postgresql_where=text("withdrawn_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    membership_id: Mapped[int] = mapped_column(
        ForeignKey("sacco_memberships.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    # Which wording the member agreed to (app.members.profile.NOTICE).
    notice_version: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
