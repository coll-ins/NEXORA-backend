"""Circle rules: circle_rules, rules_acceptances, rules_proposals, rules_votes

Revision ID: 0026
Revises: 0025

Rules are versioned rows, never edited. Acceptances and votes record who agreed
to which version. Existing circles have no rules row and keep running as before.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "circle_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sacco_id",
            sa.Integer(),
            sa.ForeignKey("saccos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("contribution_amount", sa.BigInteger(), nullable=False),
        sa.Column("contribution_frequency", sa.String(14), nullable=False),
        sa.Column("loan_cap_multiple", sa.Integer(), nullable=False),
        sa.Column("interest_percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("repayment_period_value", sa.Integer(), nullable=False),
        sa.Column("repayment_period_unit", sa.String(6), nullable=False),
        sa.Column("late_fine_amount", sa.BigInteger(), nullable=False),
        sa.Column("cycle_months", sa.Integer(), nullable=False),
        sa.Column(
            "guarantors_required", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "min_membership_days", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("exit_rules", sa.Text(), nullable=True),
        sa.Column("loan_purposes", sa.String(200), nullable=True),
        sa.Column("cycle_starts_on", sa.Date(), nullable=False),
        sa.Column("cycle_ends_on", sa.Date(), nullable=False),
        sa.Column(
            "created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("sacco_id", "version", name="uq_circle_rules_version"),
        sa.CheckConstraint(
            "contribution_amount > 0", name="ck_rules_contribution_positive"
        ),
        sa.CheckConstraint(
            "contribution_frequency IN ('weekly', 'twice_monthly', 'monthly')",
            name="ck_rules_frequency",
        ),
        sa.CheckConstraint("loan_cap_multiple >= 1", name="ck_rules_multiple_positive"),
        sa.CheckConstraint(
            "interest_percent >= 0 AND interest_percent <= 100",
            name="ck_rules_interest_range",
        ),
        sa.CheckConstraint(
            "repayment_period_value > 0", name="ck_rules_period_positive"
        ),
        sa.CheckConstraint(
            "repayment_period_unit IN ('days', 'months')", name="ck_rules_period_unit"
        ),
        sa.CheckConstraint("late_fine_amount >= 0", name="ck_rules_fine_non_negative"),
        sa.CheckConstraint(
            "cycle_months BETWEEN 9 AND 12", name="ck_rules_cycle_months"
        ),
        sa.CheckConstraint("guarantors_required >= 0", name="ck_rules_guarantors"),
        sa.CheckConstraint("min_membership_days >= 0", name="ck_rules_min_membership"),
    )
    op.create_index("ix_circle_rules_sacco_id", "circle_rules", ["sacco_id"])
    op.create_table(
        "rules_acceptances",
        sa.Column(
            "membership_id",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id"),
            primary_key=True,
        ),
        sa.Column(
            "rules_id",
            sa.Integer(),
            sa.ForeignKey("circle_rules.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "rules_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sacco_id",
            sa.Integer(),
            sa.ForeignKey("saccos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("electorate", postgresql.JSONB(), nullable=False),
        sa.Column(
            "proposed_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(8), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'adopted', 'rejected')", name="ck_rules_proposal_status"
        ),
    )
    op.create_index("ix_rules_proposals_sacco_id", "rules_proposals", ["sacco_id"])
    op.create_index(
        "uq_rules_one_open_proposal",
        "rules_proposals",
        ["sacco_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "rules_votes",
        sa.Column(
            "proposal_id",
            sa.Integer(),
            sa.ForeignKey("rules_proposals.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "membership_id",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id"),
            primary_key=True,
        ),
        sa.Column("approve", sa.Boolean(), nullable=False),
        sa.Column(
            "voted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade():
    op.drop_table("rules_votes")
    op.drop_index("ix_rules_proposals_sacco_id", table_name="rules_proposals")
    op.drop_table("rules_proposals")
    op.drop_table("rules_acceptances")
    op.drop_index("ix_circle_rules_sacco_id", table_name="circle_rules")
    op.drop_table("circle_rules")
