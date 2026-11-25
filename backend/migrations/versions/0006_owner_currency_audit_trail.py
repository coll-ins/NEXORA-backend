"""platform owner flag, currency, membership status, ledger actor/reference/reversal

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13 14:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

membership_status = postgresql.ENUM('active', 'inactive', name='membership_status', create_type=False)


def upgrade():
    # users.email is already unique through the lower(email) index.
    op.drop_constraint('users_email_key', 'users', type_='unique')
    op.add_column('users', sa.Column(
        'is_platform_owner', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('saccos', sa.Column(
        'currency', sa.String(3), server_default='KES', nullable=False))
    op.create_check_constraint('ck_saccos_currency_iso', 'saccos', "currency ~ '^[A-Z]{3}$'")

    membership_status.create(op.get_bind())
    op.add_column('sacco_memberships', sa.Column(
        'status', membership_status, server_default='active', nullable=False))

    op.create_index('ix_refresh_tokens_expires_at', 'refresh_tokens', ['expires_at'])

    # Every ledger row records who posted it, a unique reference, and what it undoes.
    op.add_column('transactions', sa.Column('reference', sa.String(64), nullable=False))
    op.add_column('transactions', sa.Column('created_by_membership_id', sa.Integer(), nullable=False))
    op.add_column('transactions', sa.Column('reverses_id', sa.Integer(), nullable=True))
    op.create_unique_constraint('uq_transactions_id_sacco', 'transactions', ['id', 'sacco_id'])
    op.create_unique_constraint('uq_transactions_sacco_reference', 'transactions', ['sacco_id', 'reference'])
    op.create_unique_constraint('transactions_reverses_id_key', 'transactions', ['reverses_id'])
    op.create_foreign_key(
        'fk_transactions_created_by_sacco', 'transactions', 'sacco_memberships',
        ['created_by_membership_id', 'sacco_id'], ['id', 'sacco_id'])
    op.create_foreign_key(
        'fk_transactions_reverses_sacco', 'transactions', 'transactions',
        ['reverses_id', 'sacco_id'], ['id', 'sacco_id'])

    op.execute("""
        CREATE FUNCTION validate_transaction() RETURNS trigger AS $$
        DECLARE orig transactions%ROWTYPE;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM sacco_memberships
                           WHERE id = NEW.created_by_membership_id AND status = 'active') THEN
                RAISE EXCEPTION 'transaction must be posted by an active membership'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.reverses_id IS NOT NULL THEN
                SELECT * INTO orig FROM transactions WHERE id = NEW.reverses_id;
                IF orig.savings_account_id <> NEW.savings_account_id
                   OR orig.amount <> NEW.amount OR orig.type = NEW.type THEN
                    RAISE EXCEPTION 'a reversal must be the opposite entry for the same account and amount'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_transactions_validate BEFORE INSERT ON transactions
        FOR EACH ROW EXECUTE FUNCTION validate_transaction();
    """)


def downgrade():
    op.execute("DROP TRIGGER trg_transactions_validate ON transactions")
    op.execute("DROP FUNCTION validate_transaction()")
    op.drop_constraint('fk_transactions_reverses_sacco', 'transactions', type_='foreignkey')
    op.drop_constraint('fk_transactions_created_by_sacco', 'transactions', type_='foreignkey')
    op.drop_constraint('transactions_reverses_id_key', 'transactions', type_='unique')
    op.drop_constraint('uq_transactions_sacco_reference', 'transactions', type_='unique')
    op.drop_constraint('uq_transactions_id_sacco', 'transactions', type_='unique')
    op.drop_column('transactions', 'reverses_id')
    op.drop_column('transactions', 'created_by_membership_id')
    op.drop_column('transactions', 'reference')
    op.drop_index('ix_refresh_tokens_expires_at', table_name='refresh_tokens')
    op.drop_column('sacco_memberships', 'status')
    membership_status.drop(op.get_bind())
    op.drop_constraint('ck_saccos_currency_iso', 'saccos', type_='check')
    op.drop_column('saccos', 'currency')
    op.drop_column('users', 'is_platform_owner')
    op.create_unique_constraint('users_email_key', 'users', ['email'])
