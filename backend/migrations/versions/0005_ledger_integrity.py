"""ledger drives balances, append-only audit log, value checks, lookup indexes

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13 13:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

INDEXES = [
    ('ix_transactions_account_created', 'transactions', ['savings_account_id', 'created_at']),
    ('ix_sacco_memberships_sacco_id', 'sacco_memberships', ['sacco_id']),
    ('ix_savings_accounts_sacco_id', 'savings_accounts', ['sacco_id']),
    ('ix_audit_logs_sacco_id', 'audit_logs', ['sacco_id']),
    ('ix_audit_logs_actor_id', 'audit_logs', ['actor_id']),
]


def upgrade():
    # Rows that must never change or vanish: no UPDATE, DELETE or TRUNCATE.
    op.execute("""
        CREATE FUNCTION append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % not allowed', TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_transactions_no_truncate BEFORE TRUNCATE ON transactions
        FOR EACH STATEMENT EXECUTE FUNCTION append_only();
    """)
    op.execute("""
        CREATE TRIGGER trg_audit_logs_append_only BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION append_only();
    """)
    op.execute("""
        CREATE TRIGGER trg_audit_logs_no_truncate BEFORE TRUNCATE ON audit_logs
        FOR EACH STATEMENT EXECUTE FUNCTION append_only();
    """)

    # The ledger is the only way to change a balance. The CHECK balance >= 0
    # then rejects overdrafts, and the row lock serialises concurrent writes.
    op.execute("""
        CREATE FUNCTION apply_transaction_to_balance() RETURNS trigger AS $$
        BEGIN
            UPDATE savings_accounts
            SET balance = balance + CASE NEW.type WHEN 'deposit' THEN NEW.amount ELSE -NEW.amount END
            WHERE id = NEW.savings_account_id;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_transactions_apply_balance AFTER INSERT ON transactions
        FOR EACH ROW EXECUTE FUNCTION apply_transaction_to_balance();
    """)
    # pg_trigger_depth() > 1 means the change came from the trigger above.
    op.execute("""
        CREATE FUNCTION guard_balance() RETURNS trigger AS $$
        BEGIN
            IF (TG_OP = 'INSERT' AND NEW.balance <> 0) OR (TG_OP = 'UPDATE' AND pg_trigger_depth() < 2) THEN
                RAISE EXCEPTION 'balance changes only through transactions' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_savings_accounts_guard_balance
        BEFORE INSERT OR UPDATE OF balance ON savings_accounts
        FOR EACH ROW EXECUTE FUNCTION guard_balance();
    """)

    # A membership owns a savings account and its ledger, so it cannot change hands.
    op.execute("""
        CREATE FUNCTION forbid_membership_user_change() RETURNS trigger AS $$
        BEGIN
            IF NEW.user_id <> OLD.user_id THEN
                RAISE EXCEPTION 'sacco_memberships.user_id cannot change' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_membership_user_immutable BEFORE UPDATE OF user_id ON sacco_memberships
        FOR EACH ROW EXECUTE FUNCTION forbid_membership_user_change();
    """)

    op.create_check_constraint(
        'ck_users_failed_login_count_non_negative', 'users', 'failed_login_count >= 0'
    )
    op.create_check_constraint(
        'ck_audit_logs_action_not_blank', 'audit_logs', "btrim(action) <> ''"
    )
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade():
    for name, table, _ in INDEXES:
        op.drop_index(name, table_name=table)
    op.drop_constraint('ck_audit_logs_action_not_blank', 'audit_logs', type_='check')
    op.drop_constraint('ck_users_failed_login_count_non_negative', 'users', type_='check')
    op.execute("DROP TRIGGER trg_membership_user_immutable ON sacco_memberships")
    op.execute("DROP FUNCTION forbid_membership_user_change()")
    op.execute("DROP TRIGGER trg_savings_accounts_guard_balance ON savings_accounts")
    op.execute("DROP FUNCTION guard_balance()")
    op.execute("DROP TRIGGER trg_transactions_apply_balance ON transactions")
    op.execute("DROP FUNCTION apply_transaction_to_balance()")
    op.execute("DROP TRIGGER trg_audit_logs_no_truncate ON audit_logs")
    op.execute("DROP TRIGGER trg_audit_logs_append_only ON audit_logs")
    op.execute("DROP TRIGGER trg_transactions_no_truncate ON transactions")
    op.execute("DROP FUNCTION append_only()")
