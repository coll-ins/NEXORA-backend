"""Optional loan sector and disclosure acknowledgement

Revision ID: 0035
Revises: 0034

`sector` says what a loan is for (for the SACCO's reporting) and
`disclosure_acknowledged_at` records when the borrower confirmed they were shown
the cost of the loan. Both are nullable: neither is required yet.
"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("loans", sa.Column("sector", sa.String(20)))
    op.add_column(
        "loans", sa.Column("disclosure_acknowledged_at", sa.DateTime(timezone=True))
    )
    op.create_check_constraint(
        "ck_loans_sector",
        "loans",
        "sector IN ('agriculture', 'trade', 'manufacturing', 'transport', "
        "'housing', 'education', 'health', 'other')",
    )


def downgrade():
    op.drop_constraint("ck_loans_sector", "loans", type_="check")
    op.drop_column("loans", "disclosure_acknowledged_at")
    op.drop_column("loans", "sector")
