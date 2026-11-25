"""SACCO rules: what a SACCO's creator sets and every member accepts.

A row is one published version. Rules are never edited: a change is a new
version, adopted by member vote once the cycle has ended. Amounts are integer
minor units (100 = KES 1.00), like the rest of the ledger.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class SaccoRules(db.Model):
    __tablename__ = "sacco_rules"
    __table_args__ = (
        UniqueConstraint("sacco_id", "version", name="uq_sacco_rules_version"),
        CheckConstraint(
            "contribution_amount > 0", name="ck_rules_contribution_positive"
        ),
        CheckConstraint(
            "contribution_frequency IN ('weekly', 'twice_monthly', 'monthly')",
            name="ck_rules_frequency",
        ),
        CheckConstraint("loan_cap_multiple >= 1", name="ck_rules_multiple_positive"),
        CheckConstraint(
            "interest_percent >= 0 AND interest_percent <= 100",
            name="ck_rules_interest_range",
        ),
        CheckConstraint("repayment_period_value > 0", name="ck_rules_period_positive"),
        CheckConstraint(
            "repayment_period_unit IN ('days', 'months')", name="ck_rules_period_unit"
        ),
        CheckConstraint("late_fine_amount >= 0", name="ck_rules_fine_non_negative"),
        CheckConstraint("cycle_months BETWEEN 9 AND 12", name="ck_rules_cycle_months"),
        CheckConstraint("guarantors_required >= 0", name="ck_rules_guarantors"),
        CheckConstraint("min_membership_days >= 0", name="ck_rules_min_membership"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    contribution_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    contribution_frequency: Mapped[str] = mapped_column(String(14), nullable=False)
    loan_cap_multiple: Mapped[int] = mapped_column(Integer, nullable=False)
    interest_percent: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    repayment_period_value: Mapped[int] = mapped_column(Integer, nullable=False)
    repayment_period_unit: Mapped[str] = mapped_column(String(6), nullable=False)
    late_fine_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cycle_months: Mapped[int] = mapped_column(Integer, nullable=False)
    guarantors_required: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    min_membership_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    exit_rules: Mapped[str | None] = mapped_column(Text, nullable=True)
    loan_purposes: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cycle_starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    cycle_ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RulesAcceptance(db.Model):
    """Who accepted which version, and when. Never updated."""

    __tablename__ = "rules_acceptances"

    membership_id: Mapped[int] = mapped_column(
        ForeignKey("sacco_memberships.id"), primary_key=True
    )
    rules_id: Mapped[int] = mapped_column(
        ForeignKey("sacco_rules.id", ondelete="CASCADE"), primary_key=True
    )
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RulesProposal(db.Model):
    """A new version put to the members' vote after the cycle ends."""

    __tablename__ = "rules_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'adopted', 'rejected')", name="ck_rules_proposal_status"
        ),
        CheckConstraint("active_members >= 0", name="ck_rules_proposal_active_members"),
        # One open vote per SACCO, enforced by the database.
        Index(
            "uq_rules_one_open_proposal",
            "sacco_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Membership ids that may vote, frozen when the proposal opens.
    electorate: Mapped[list] = mapped_column(JSONB, nullable=False)
    # Every active member when it opened: a change needs more than half of these.
    active_members: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(8), nullable=False, default="open", server_default="open"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RulesVote(db.Model):
    __tablename__ = "rules_votes"

    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("rules_proposals.id", ondelete="CASCADE"), primary_key=True
    )
    membership_id: Mapped[int] = mapped_column(
        ForeignKey("sacco_memberships.id"), primary_key=True
    )
    approve: Mapped[bool] = mapped_column(Boolean, nullable=False)
    voted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
