"""the ledger refuses a reversal of a reversal

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-14 10:00:00.000000

"""
# ruff: noqa: E501
from alembic import op

# revision identifiers, used by Alembic.
revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

# Same function as 0006, plus the one extra rule.
BODY = """
    CREATE OR REPLACE FUNCTION validate_transaction() RETURNS trigger AS $$
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
            {RULE}
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
"""

RULE = """IF orig.reverses_id IS NOT NULL THEN
                RAISE EXCEPTION 'a reversal cannot be reversed'
                    USING ERRCODE = 'check_violation';
            END IF;"""


def upgrade():
    op.execute(BODY.replace("{RULE}", RULE))


def downgrade():
    op.execute(BODY.replace("{RULE}", ""))
