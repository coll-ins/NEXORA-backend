"""ledger rows append only; loan terms and steps enforced by the database

Revision ID: 0020
Revises: 0019

The agreed rules (docs/roles-permissions.md) say ledger rows are never changed
or removed and a loan's terms never change after the request. 0005 only blocked
TRUNCATE on `transactions`, and loans were protected by the application alone.
Now the database refuses:

* any UPDATE or DELETE of a ledger row (corrections are reversal rows);
* a change to a loan's SACCO, borrower, principal, term or approval count;
* a status move other than requested -> approved | rejected,
  approved -> disbursed, disbursed -> repaid;
* changing the disbursement link once it is set.
"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TRIGGER trg_transactions_append_only "
        "BEFORE UPDATE OR DELETE ON transactions "
        "FOR EACH ROW EXECUTE FUNCTION append_only()"
    )
    op.execute(
        """
        CREATE FUNCTION guard_loan_update() RETURNS trigger AS $$
        BEGIN
            IF NEW.sacco_id <> OLD.sacco_id
               OR NEW.membership_id <> OLD.membership_id
               OR NEW.principal <> OLD.principal
               OR NEW.required_approvals <> OLD.required_approvals
               OR NEW.term_months IS DISTINCT FROM OLD.term_months THEN
                RAISE EXCEPTION 'loan terms cannot change after the request'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.disbursement_transaction_id IS NOT NULL
               AND NEW.disbursement_transaction_id
                   IS DISTINCT FROM OLD.disbursement_transaction_id THEN
                RAISE EXCEPTION 'the disbursement row is set once'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status <> OLD.status AND NOT (
                   (OLD.status = 'requested' AND NEW.status IN ('approved', 'rejected'))
                OR (OLD.status = 'approved' AND NEW.status = 'disbursed')
                OR (OLD.status = 'disbursed' AND NEW.status = 'repaid')
            ) THEN
                RAISE EXCEPTION 'loan % -> % is not allowed', OLD.status, NEW.status
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_loans_guard_update BEFORE UPDATE ON loans "
        "FOR EACH ROW EXECUTE FUNCTION guard_loan_update()"
    )


def downgrade():
    op.execute("DROP TRIGGER trg_loans_guard_update ON loans")
    op.execute("DROP FUNCTION guard_loan_update()")
    op.execute("DROP TRIGGER trg_transactions_append_only ON transactions")
