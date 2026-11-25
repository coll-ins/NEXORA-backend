"""Deposit controls, M-Pesa receipts on requests, reversal requests

Revision ID: 0038
Revises: 0037

- transactions.source marks a manual or STK entry (added with a default: no row
  is updated, so the append-only trigger is not involved).
- payment_requests gain a source, the M-Pesa receipt (unique per SACCO), the STK
  push it came from, flag reasons and a first approver.
- sacco_deposit_controls holds each SACCO's thresholds.
- reversal_requests: one officer asks, a different officer approves.
"""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "transactions",
        sa.Column("source", sa.String(10), nullable=False, server_default="standard"),
    )
    op.create_check_constraint(
        "ck_transactions_source",
        "transactions",
        "source IN ('standard', 'manual', 'stk')",
    )

    op.add_column(
        "payment_requests",
        sa.Column("source", sa.String(10), nullable=False, server_default="member"),
    )
    op.add_column(
        "payment_requests", sa.Column("mpesa_receipt", sa.String(20), nullable=True)
    )
    op.add_column(
        "payment_requests",
        sa.Column(
            "stk_push_id", sa.Integer(), sa.ForeignKey("stk_pushes.id"), nullable=True
        ),
    )
    op.add_column("payment_requests", sa.Column("flags", sa.String(120), nullable=True))
    op.add_column(
        "payment_requests",
        sa.Column(
            "first_approved_by",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "payment_requests",
        sa.Column("first_approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_payment_requests_source",
        "payment_requests",
        "source IN ('member', 'stk', 'manual')",
    )
    op.create_check_constraint(
        "ck_payment_requests_two_officers",
        "payment_requests",
        "first_approved_by IS NULL OR decided_by IS NULL "
        "OR first_approved_by <> decided_by",
    )
    op.create_index(
        "uq_payment_requests_sacco_receipt",
        "payment_requests",
        ["sacco_id", "mpesa_receipt"],
        unique=True,
        postgresql_where=sa.text("mpesa_receipt IS NOT NULL"),
    )

    op.create_table(
        "sacco_deposit_controls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sacco_id", sa.Integer(), sa.ForeignKey("saccos.id"), nullable=False),
        sa.Column("auto_confirm_limit", sa.BigInteger(), nullable=False),
        sa.Column("large_amount_limit", sa.BigInteger(), nullable=False),
        sa.Column("manual_flag_limit", sa.BigInteger(), nullable=False),
        sa.Column("repeat_count", sa.Integer(), nullable=False),
        sa.Column("repeat_window_hours", sa.Integer(), nullable=False),
        sa.Column("quick_withdrawal_hours", sa.Integer(), nullable=False),
        sa.Column(
            "updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("sacco_id", name="uq_sacco_deposit_controls_sacco_id"),
        sa.CheckConstraint(
            "auto_confirm_limit BETWEEN 0 AND 500000", name="ck_deposit_controls_auto"
        ),
        sa.CheckConstraint("large_amount_limit > 0", name="ck_deposit_controls_large"),
        sa.CheckConstraint("manual_flag_limit > 0", name="ck_deposit_controls_manual"),
        sa.CheckConstraint("repeat_count >= 2", name="ck_deposit_controls_repeat"),
        sa.CheckConstraint(
            "repeat_window_hours > 0 AND quick_withdrawal_hours >= 0",
            name="ck_deposit_controls_hours",
        ),
    )

    op.create_table(
        "reversal_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sacco_id", sa.Integer(), sa.ForeignKey("saccos.id"), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column(
            "requested_by",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(140), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column(
            "decided_by",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.String(140), nullable=True),
        sa.Column(
            "reversal_transaction_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id", "sacco_id"],
            ["transactions.id", "transactions.sacco_id"],
            name="fk_reversal_requests_transaction_sacco",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_reversal_requests_status",
        ),
        sa.CheckConstraint(
            "decided_by IS NULL OR decided_by <> requested_by",
            name="ck_reversal_requests_two_officers",
        ),
    )
    op.create_index("ix_reversal_requests_sacco_id", "reversal_requests", ["sacco_id"])
    op.create_index(
        "uq_reversal_requests_open",
        "reversal_requests",
        ["transaction_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade():
    op.drop_table("reversal_requests")
    op.drop_table("sacco_deposit_controls")
    op.drop_index("uq_payment_requests_sacco_receipt", table_name="payment_requests")
    for name in ("ck_payment_requests_two_officers", "ck_payment_requests_source"):
        op.drop_constraint(name, "payment_requests", type_="check")
    for col in (
        "first_approved_at",
        "first_approved_by",
        "flags",
        "stk_push_id",
        "mpesa_receipt",
        "source",
    ):
        op.drop_column("payment_requests", col)
    op.drop_constraint("ck_transactions_source", "transactions", type_="check")
    op.drop_column("transactions", "source")
