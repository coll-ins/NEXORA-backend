"""loans, loan decisions and loan repayments

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-17 10:20:00.000000

Section 1 (Neo) provides the tables Section 3 posts into. `required_approvals`
is frozen on the loan when it is requested, each approval is one row with a
unique step, and each repayment points at the ledger row that posted it.
"""
import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

LOAN_STATUS = sa.Enum(
    "requested",
    "approved",
    "rejected",
    "disbursed",
    "repaid",
    name="loan_status",
)
LOAN_DECISION = sa.Enum("approve", "reject", name="loan_decision_kind")


def upgrade():
    op.create_table(
        "loans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sacco_id",
            sa.Integer(),
            sa.ForeignKey("saccos.id"),
            nullable=False,
        ),
        sa.Column("membership_id", sa.Integer(), nullable=False),
        sa.Column("principal", sa.BigInteger(), nullable=False),
        sa.Column("required_approvals", sa.Integer(), nullable=False),
        sa.Column("status", LOAN_STATUS, nullable=False, server_default="requested"),
        sa.Column(
            "requested_by_membership_id",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id"),
            nullable=True,
        ),
        sa.Column(
            "disbursed_by_membership_id",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id"),
            nullable=True,
        ),
        sa.Column("disbursed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # A loan belongs to a membership of its own SACCO.
        sa.ForeignKeyConstraint(
            ["membership_id", "sacco_id"],
            ["sacco_memberships.id", "sacco_memberships.sacco_id"],
            name="fk_loans_membership_sacco",
        ),
        sa.UniqueConstraint("id", "sacco_id", name="uq_loans_id_sacco"),
        sa.CheckConstraint("principal > 0", name="ck_loans_principal_positive"),
        sa.CheckConstraint(
            "required_approvals >= 1", name="ck_loans_required_approvals_positive"
        ),
    )
    op.create_index("ix_loans_sacco_id", "loans", ["sacco_id"])
    op.create_index("ix_loans_membership_id", "loans", ["membership_id"])

    op.create_table(
        "loan_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "loan_id",
            sa.Integer(),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("decision", LOAN_DECISION, nullable=False),
        sa.Column(
            "decided_by_membership_id",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("loan_id", "step", name="uq_loan_decisions_loan_step"),
        sa.CheckConstraint("step >= 1", name="ck_loan_decisions_step_positive"),
    )
    op.create_index("ix_loan_decisions_loan_id", "loan_decisions", ["loan_id"])

    op.create_table(
        "loan_repayments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "loan_id",
            sa.Integer(),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "transaction_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("amount > 0", name="ck_loan_repayments_amount_positive"),
    )
    op.create_index("ix_loan_repayments_loan_id", "loan_repayments", ["loan_id"])


def downgrade():
    op.drop_index("ix_loan_repayments_loan_id", table_name="loan_repayments")
    op.drop_table("loan_repayments")
    op.drop_index("ix_loan_decisions_loan_id", table_name="loan_decisions")
    op.drop_table("loan_decisions")
    op.drop_index("ix_loans_membership_id", table_name="loans")
    op.drop_index("ix_loans_sacco_id", table_name="loans")
    op.drop_table("loans")
    LOAN_DECISION.drop(op.get_bind(), checkfirst=True)
    LOAN_STATUS.drop(op.get_bind(), checkfirst=True)
