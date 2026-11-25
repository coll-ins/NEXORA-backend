"""token_version on users, email verification

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15 12:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'users',
        sa.Column('token_version', sa.Integer(), server_default='0', nullable=False),
    )
    op.add_column(
        'users', sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True)
    )
    # Accounts that exist today were already vetted by whoever added them.
    op.execute("UPDATE users SET email_verified_at = now()")
    op.create_table(
        'email_verifications',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'user_id',
            sa.Integer(),
            sa.ForeignKey('users.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        'ix_email_verifications_user_id', 'email_verifications', ['user_id']
    )


def downgrade():
    op.drop_index('ix_email_verifications_user_id', table_name='email_verifications')
    op.drop_table('email_verifications')
    op.drop_column('users', 'email_verified_at')
    op.drop_column('users', 'token_version')
