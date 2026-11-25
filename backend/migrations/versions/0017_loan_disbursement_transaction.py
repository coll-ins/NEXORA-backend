"""link a loan to its disbursement row; per-SACCO approval count

Revision ID: 0017
Revises: 0016

A disbursement is posted as a deposit and a repayment as a withdrawal. Without
this link a disbursement row looks like any other deposit, so the reversal
route could undo loan money while the loan still says it was paid out.

`sacco_settings.required_approvals` is read by the loan service and declared on
the model, but no earlier migration created it, so a migrated database answered
500 on every loan request.
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sacco_settings",
        sa.Column(
            "required_approvals", sa.Integer(), nullable=False, server_default="2"
        ),
    )
    op.create_check_constraint(
        "ck_sacco_settings_required_approvals_positive",
        "sacco_settings",
        "required_approvals >= 1",
    )
    op.add_column(
        "loans",
        sa.Column(
            "disbursement_transaction_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id", name="fk_loans_disbursement_transaction"),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "uq_loans_disbursement_transaction", "loans", ["disbursement_transaction_id"]
    )


def downgrade():
    op.drop_constraint("uq_loans_disbursement_transaction", "loans", type_="unique")
    op.drop_constraint("fk_loans_disbursement_transaction", "loans", type_="foreignkey")
    op.drop_column("loans", "disbursement_transaction_id")
    op.drop_constraint(
        "ck_sacco_settings_required_approvals_positive", "sacco_settings", type_="check"
    )
    op.drop_column("sacco_settings", "required_approvals")
