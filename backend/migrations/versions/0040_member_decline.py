"""A member may decline a SACCO invitation, with a reason officers can read

Revision ID: 0040
Revises: 0039

`declined_at` and `decline_reason` are set together, and only on a membership the
member refused. A declined membership never becomes active: the join code is
spent, and an officer who invites again starts a fresh invitation.

Postgres cannot drop an enum value, so `declined` stays in the type on downgrade.
"""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

TABLE = "sacco_memberships"
DECLINE_PAIR = "ck_memberships_decline_pair"


def upgrade():
    # A new enum value cannot be used in the same transaction that adds it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE membership_status ADD VALUE IF NOT EXISTS 'declined'")
    op.add_column(TABLE, sa.Column("declined_at", sa.DateTime(timezone=True)))
    op.add_column(TABLE, sa.Column("decline_reason", sa.String(500)))
    op.create_check_constraint(
        DECLINE_PAIR,
        TABLE,
        "(declined_at IS NULL) = (decline_reason IS NULL) "
        "AND (status <> 'declined' OR declined_at IS NOT NULL)",
    )


def downgrade():
    op.drop_constraint(DECLINE_PAIR, TABLE, type_="check")
    op.drop_column(TABLE, "decline_reason")
    op.drop_column(TABLE, "declined_at")
