"""Member deposit and withdrawal requests: payment_requests

Revision ID: 0025
Revises: 0024

A member asks to deposit or withdraw by M-Pesa or Bank; the request changes no
balance. An officer confirms or rejects it, and confirming posts the real ledger
entry through the existing savings service.

This also seeds the permission the member needs: `savings.request`, held by the
`member` job title only. Officers keep `savings.post` to decide the queue.
"""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

PERMISSIONS = {
    "savings.request": "Send a deposit or withdrawal request for my own account",
}


def upgrade():
    op.create_table(
        "payment_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sacco_id",
            sa.Integer(),
            sa.ForeignKey("saccos.id"),
            nullable=False,
        ),
        sa.Column(
            "membership_id",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("bank_name", sa.String(length=60), nullable=True),
        sa.Column("account_ref", sa.String(length=40), nullable=True),
        sa.Column("reference", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=12), nullable=False, server_default="pending"
        ),
        sa.Column(
            "decided_by",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.String(length=500), nullable=True),
        sa.Column(
            "ledger_transaction_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('deposit', 'withdrawal')", name="ck_payment_requests_kind"
        ),
        sa.CheckConstraint(
            "method IN ('mpesa', 'bank')", name="ck_payment_requests_method"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_payment_requests_status",
        ),
        sa.CheckConstraint(
            "amount_minor > 0", name="ck_payment_requests_amount_positive"
        ),
        sa.UniqueConstraint("reference", name="uq_payment_requests_reference"),
    )
    op.create_index("ix_payment_requests_sacco_id", "payment_requests", ["sacco_id"])
    op.create_index(
        "ix_payment_requests_membership_id", "payment_requests", ["membership_id"]
    )

    for code, description in PERMISSIONS.items():
        op.execute(
            sa.text(
                "INSERT INTO permissions (code, description) VALUES (:c, :d) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(c=code, d=description)
        )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            "WHERE r.code = 'member' AND p.code = 'savings.request' "
            "ON CONFLICT DO NOTHING"
        )
    )


def downgrade():
    for code in PERMISSIONS:
        op.execute(
            sa.text(
                "DELETE FROM role_permissions WHERE permission_id = "
                "(SELECT id FROM permissions WHERE code = :c)"
            ).bindparams(c=code)
        )
        op.execute(
            sa.text("DELETE FROM permissions WHERE code = :c").bindparams(c=code)
        )
    op.drop_index("ix_payment_requests_membership_id", "payment_requests")
    op.drop_index("ix_payment_requests_sacco_id", "payment_requests")
    op.drop_table("payment_requests")
