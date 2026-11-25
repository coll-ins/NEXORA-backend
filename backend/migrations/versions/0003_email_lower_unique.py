"""case-insensitive unique email

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13 11:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        'uq_users_email_lower', 'users', [sa.text('lower(email)')], unique=True
    )


def downgrade():
    op.drop_index('uq_users_email_lower', table_name='users')
