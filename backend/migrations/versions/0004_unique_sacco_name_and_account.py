"""unique sacco name (case/space-insensitive, not blank) and one savings account per membership

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13 12:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        'uq_saccos_name_lower', 'saccos', [sa.text('lower(btrim(name))')], unique=True
    )
    op.create_check_constraint('ck_saccos_name_not_blank', 'saccos', "btrim(name) <> ''")
    op.create_unique_constraint(
        'uq_savings_account_membership', 'savings_accounts', ['membership_id']
    )


def downgrade():
    op.drop_constraint('uq_savings_account_membership', 'savings_accounts', type_='unique')
    op.drop_constraint('ck_saccos_name_not_blank', 'saccos', type_='check')
    op.drop_index('uq_saccos_name_lower', table_name='saccos')
