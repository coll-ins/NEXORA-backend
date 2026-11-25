"""STK push records (Safaricom sandbox)

Revision ID: 0036
Revises: 0035

One row per STK push asked of M-Pesa. It records the answer and moves no money.
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stk_pushes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sacco_id", sa.Integer(), nullable=False),
        sa.Column("membership_id", sa.Integer(), nullable=False),
        sa.Column("phone", sa.String(12), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("checkout_request_id", sa.String(64)),
        sa.Column("result_desc", sa.String(255)),
        sa.Column("mpesa_receipt", sa.String(20)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["membership_id", "sacco_id"],
            ["sacco_memberships.id", "sacco_memberships.sacco_id"],
            name="fk_stk_pushes_membership_sacco",
        ),
        sa.CheckConstraint("amount > 0", name="ck_stk_pushes_amount_positive"),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')", name="ck_stk_pushes_status"
        ),
    )
    op.create_index("ix_stk_pushes_sacco_id", "stk_pushes", ["sacco_id"])
    op.create_index("ix_stk_pushes_membership_id", "stk_pushes", ["membership_id"])
    op.create_index(
        "uq_stk_pushes_checkout",
        "stk_pushes",
        ["checkout_request_id"],
        unique=True,
        postgresql_where=sa.text("checkout_request_id IS NOT NULL"),
    )
    op.create_index(
        "uq_stk_pushes_receipt",
        "stk_pushes",
        ["mpesa_receipt"],
        unique=True,
        postgresql_where=sa.text("mpesa_receipt IS NOT NULL"),
    )


def downgrade():
    op.drop_table("stk_pushes")
