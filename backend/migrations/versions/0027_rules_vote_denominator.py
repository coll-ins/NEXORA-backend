"""Rules proposals remember how many members were active when they opened

Revision ID: 0027
Revises: 0026

A change passes only if more than half of ALL active members approve it, not
half of those who happened to accept the current rules. The count is frozen at
proposal time like the electorate. Proposals that already exist take the size
of their electorate, which is what they were counted against until now.
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "rules_proposals",
        sa.Column("active_members", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE rules_proposals SET active_members = jsonb_array_length(electorate)"
    )
    op.alter_column("rules_proposals", "active_members", nullable=False)
    op.create_check_constraint(
        "ck_rules_proposal_active_members",
        "rules_proposals",
        "active_members >= 0",
    )


def downgrade():
    op.drop_constraint("ck_rules_proposal_active_members", "rules_proposals")
    op.drop_column("rules_proposals", "active_members")
