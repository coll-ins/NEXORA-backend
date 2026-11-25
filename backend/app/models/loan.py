"""Loans and per-SACCO settings (Section 1 schema, Section 3 behaviour).

The rules live in Section 3; the tables here are the shape it builds against:

* `sacco_settings` holds the loan cap and savings multiple a request is measured
  against, plus the join-code lifetime. One row per SACCO.
* `loans` stores `required_approvals`, which Section 3 computes when the request
  is made, so an approval count cannot drift if settings change later.
* `loan_decisions` records one approval per step, with a unique (loan_id, step),
  so the same step cannot be approved twice.
* `loan_repayments` points at the ledger row that posted it, one ledger row per
  repayment.

Every loan row carries its SACCO, and a composite foreign key ties it to a
membership of that same SACCO: a loan can never belong to another SACCO's member.
"""

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
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class LoanStatus(enum.Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISBURSED = "disbursed"
    REPAID = "repaid"


class LoanDecisionKind(enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"


class SaccoSettings(db.Model):
    """The numbers a SACCO runs by. Defaults match the decisions section."""

    __tablename__ = "sacco_settings"

    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id", ondelete="CASCADE"), primary_key=True
    )
    # 0 means no loan may exceed the cap yet: the team sets it per SACCO.
    loan_cap: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    savings_multiple: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    join_code_ttl_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15, server_default="15"
    )
    required_approvals: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "loan_cap >= 0", name="ck_sacco_settings_loan_cap_non_negative"
        ),
        CheckConstraint(
            "savings_multiple >= 0", name="ck_sacco_settings_multiple_non_negative"
        ),
        CheckConstraint(
            "join_code_ttl_minutes > 0",
            name="ck_sacco_settings_ttl_positive",
        ),
        CheckConstraint(
            "required_approvals >= 1",
            name="ck_sacco_settings_required_approvals_positive",
        ),
    )


class Loan(db.Model):
    __tablename__ = "loans"
    __table_args__ = (
        # A loan belongs to a membership of its own SACCO.
        ForeignKeyConstraint(
            ["membership_id", "sacco_id"],
            ["sacco_memberships.id", "sacco_memberships.sacco_id"],
            name="fk_loans_membership_sacco",
        ),
        UniqueConstraint("id", "sacco_id", name="uq_loans_id_sacco"),
        UniqueConstraint(
            "disbursement_transaction_id", name="uq_loans_disbursement_transaction"
        ),
        CheckConstraint("principal > 0", name="ck_loans_principal_positive"),
        CheckConstraint(
            "required_approvals >= 1", name="ck_loans_required_approvals_positive"
        ),
        CheckConstraint(
            "term_months IS NULL OR term_months > 0",
            name="ck_loans_term_months_positive",
        ),
        CheckConstraint(
            "sector IN ('agriculture', 'trade', 'manufacturing', 'transport', "
            "'housing', 'education', 'health', 'other')",
            name="ck_loans_sector",
        ),
        # One open loan per member (agreed schema rule).
        Index(
            "uq_loans_one_open_per_member",
            "membership_id",
            unique=True,
            postgresql_where=text("status IN ('requested', 'approved', 'disbursed')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id"), nullable=False, index=True
    )
    membership_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    principal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    term_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Computed from sacco_settings when the request is made, then frozen.
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[LoanStatus] = mapped_column(
        Enum(
            LoanStatus,
            name="loan_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=LoanStatus.REQUESTED,
        server_default=LoanStatus.REQUESTED.value,
    )
    requested_by_membership_id: Mapped[int | None] = mapped_column(
        ForeignKey("sacco_memberships.id"), nullable=True
    )
    # Set by disbursement; must differ from every approver (Section 3 rule).
    disbursed_by_membership_id: Mapped[int | None] = mapped_column(
        ForeignKey("sacco_memberships.id"), nullable=True
    )
    disbursed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The ledger row that paid the loan out; reversals refuse it.
    disbursement_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", name="fk_loans_disbursement_transaction"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # What the loan is for, for the SACCO's reporting. Optional for now.
    sector: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # When the borrower confirmed they were shown the cost of this loan (interest,
    # fines, term) before asking. Recorded only; optional for now.
    disclosure_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LoanDecision(db.Model):
    __tablename__ = "loan_decisions"
    __table_args__ = (
        UniqueConstraint("loan_id", "step", name="uq_loan_decisions_loan_step"),
        CheckConstraint("step >= 1", name="ck_loan_decisions_step_positive"),
        CheckConstraint(
            "decision <> 'reject' OR btrim(coalesce(reason, '')) <> ''",
            name="ck_loan_decisions_reject_has_reason",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loan_id: Mapped[int] = mapped_column(
        ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[LoanDecisionKind] = mapped_column(
        Enum(
            LoanDecisionKind,
            name="loan_decision_kind",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    decided_by_membership_id: Mapped[int] = mapped_column(
        ForeignKey("sacco_memberships.id"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LoanRepayment(db.Model):
    __tablename__ = "loan_repayments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_loan_repayments_amount_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loan_id: Mapped[int] = mapped_column(
        ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # One ledger row per repayment, and each ledger row repays at most one loan.
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id"), nullable=False, unique=True
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
