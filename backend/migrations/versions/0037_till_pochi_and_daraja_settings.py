"""Pochi la Biashara pay-in type and per-SACCO Daraja credentials

Revision ID: 0037
Revises: 0036

A pay-in can now be a Pochi la Biashara number (a phone number, record only: no
STK push). The M-Pesa number column widens to hold it. Each SACCO may also keep
its own Daraja shortcode and credentials, encrypted by the application.
"""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

TABLES = ("sacco_payment_details", "sacco_payment_detail_changes")
NEW_NUMBER = (
    "(mpesa_type = 'pochi' AND mpesa_number ~ '^254[17][0-9]{8}$') OR "
    "(mpesa_type IN ('paybill', 'till') AND mpesa_number ~ '^[0-9]{5,7}$')"
)


def _swap(table, kind, number):
    op.drop_constraint(f"ck_{table}_mpesa_type", table, type_="check")
    op.drop_constraint(f"ck_{table}_mpesa_number", table, type_="check")
    op.create_check_constraint(f"ck_{table}_mpesa_type", table, kind)
    op.create_check_constraint(f"ck_{table}_mpesa_number", table, number)


def upgrade():
    for table in TABLES:
        op.alter_column(table, "mpesa_number", type_=sa.String(12))
        _swap(table, "mpesa_type IN ('paybill', 'till', 'pochi')", NEW_NUMBER)
    op.create_table(
        "sacco_daraja_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sacco_id", sa.Integer(), sa.ForeignKey("saccos.id"), nullable=False),
        sa.Column("shortcode", sa.String(7), nullable=False),
        sa.Column("consumer_key_enc", sa.String(600), nullable=False),
        sa.Column("consumer_secret_enc", sa.String(600), nullable=False),
        sa.Column("passkey_enc", sa.String(900), nullable=False),
        sa.Column(
            "updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("sacco_id", name="uq_sacco_daraja_settings_sacco_id"),
        sa.CheckConstraint("shortcode ~ '^[0-9]{5,7}$'", name="ck_daraja_shortcode"),
    )


def downgrade():
    for table in TABLES:
        if op.get_bind().scalar(
            sa.text(f"SELECT count(*) FROM {table} WHERE mpesa_type = 'pochi'")
        ):
            raise RuntimeError(
                f"{table} still has Pochi la Biashara rows. Replace them first."
            )
    op.drop_table("sacco_daraja_settings")
    for table in TABLES:
        _swap(
            table, "mpesa_type IN ('paybill', 'till')", "mpesa_number ~ '^[0-9]{5,7}$'"
        )
        op.alter_column(table, "mpesa_number", type_=sa.String(7))
