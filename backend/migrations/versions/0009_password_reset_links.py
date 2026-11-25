"""one-time links carry a purpose (verify an email, reset a password)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-15 18:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'email_verifications',
        sa.Column('purpose', sa.String(16), server_default='verify', nullable=False),
    )
    op.create_check_constraint(
        'ck_email_verifications_purpose',
        'email_verifications',
        "purpose IN ('verify', 'reset')",
    )


def downgrade():
    op.drop_constraint(
        'ck_email_verifications_purpose', 'email_verifications', type_='check'
    )
    op.drop_column('email_verifications', 'purpose')
