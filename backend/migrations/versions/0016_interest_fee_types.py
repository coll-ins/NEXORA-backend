"""add interest and fee transaction types

Revision ID: 0016
Revises: 0015
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE transaction_type ADD VALUE IF NOT EXISTS 'interest'")
        op.execute("ALTER TYPE transaction_type ADD VALUE IF NOT EXISTS 'fee'")

    op.execute("""
        CREATE OR REPLACE FUNCTION apply_transaction_to_balance() RETURNS trigger AS $$
        BEGIN
            UPDATE savings_accounts
            SET balance = balance + CASE
                WHEN NEW.type IN ('deposit', 'interest') THEN NEW.amount
                ELSE -NEW.amount
            END
            WHERE id = NEW.savings_account_id;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION validate_transaction() RETURNS trigger AS $$
        DECLARE
            orig transactions%ROWTYPE;
            expected_opposite transaction_type;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM sacco_memberships
                WHERE id = NEW.created_by_membership_id AND status = 'active'
            ) THEN
                RAISE EXCEPTION 'transaction must be posted by an active membership'
                    USING ERRCODE = 'check_violation';
            END IF;

            IF NEW.reverses_id IS NOT NULL THEN
                SELECT * INTO orig FROM transactions WHERE id = NEW.reverses_id;
                IF orig.reverses_id IS NOT NULL THEN
                    RAISE EXCEPTION 'a reversal cannot be reversed'
                        USING ERRCODE = 'check_violation';
                END IF;

                expected_opposite := CASE orig.type
                    WHEN 'deposit' THEN 'withdrawal'::transaction_type
                    WHEN 'withdrawal' THEN 'deposit'::transaction_type
                    WHEN 'interest' THEN 'fee'::transaction_type
                    WHEN 'fee' THEN 'interest'::transaction_type
                END;

                IF orig.savings_account_id <> NEW.savings_account_id
                   OR orig.amount <> NEW.amount
                   OR NEW.type <> expected_opposite THEN
                    RAISE EXCEPTION 'a reversal must be the opposite entry for the same account and amount'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)


def downgrade():
    # Postgres enum values cannot be dropped; restore original balance trigger function.
    op.execute("""
        CREATE OR REPLACE FUNCTION apply_transaction_to_balance() RETURNS trigger AS $$
        BEGIN
            UPDATE savings_accounts
            SET balance = balance + CASE NEW.type WHEN 'deposit' THEN NEW.amount ELSE -NEW.amount END
            WHERE id = NEW.savings_account_id;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)
