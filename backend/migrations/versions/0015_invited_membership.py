"""invited memberships: status 'invited', nullable user_id, phone on the membership

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE membership_status ADD VALUE IF NOT EXISTS 'invited'")
    op.add_column("sacco_memberships", sa.Column("phone", sa.String(20), nullable=True))
    op.alter_column("sacco_memberships", "user_id", nullable=True)
    op.create_unique_constraint(
        "uq_membership_sacco_phone", "sacco_memberships", ["sacco_id", "phone"]
    )
    op.create_check_constraint(
        "ck_membership_user_or_phone",
        "sacco_memberships",
        "user_id IS NOT NULL OR phone IS NOT NULL",
    )


def downgrade():
    op.drop_constraint("ck_membership_user_or_phone", "sacco_memberships", type_="check")
    op.drop_constraint("uq_membership_sacco_phone", "sacco_memberships", type_="unique")
    op.execute("DELETE FROM sacco_memberships WHERE user_id IS NULL")
    op.alter_column("sacco_memberships", "user_id", nullable=False)
    op.drop_column("sacco_memberships", "phone")
    # Postgres cannot drop an enum value, so 'invited' stays in the type.