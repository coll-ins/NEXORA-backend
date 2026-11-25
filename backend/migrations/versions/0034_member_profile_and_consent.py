"""Member personal details, share capital and data-protection consent

Revision ID: 0034
Revises: 0033

All fields are optional. Personal data (full name, national ID, gender, date of
birth) is kept only while the member's `member_profile` consent is active and
is erased when it is withdrawn (Data Protection Act 2019). The national ID is
unique within a SACCO.
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

TABLE = "sacco_memberships"


def upgrade():
    op.add_column(TABLE, sa.Column("full_name", sa.String(120)))
    op.add_column(TABLE, sa.Column("national_id", sa.String(8)))
    op.add_column(TABLE, sa.Column("gender", sa.String(10)))
    op.add_column(TABLE, sa.Column("date_of_birth", sa.Date()))
    op.add_column(TABLE, sa.Column("share_capital", sa.BigInteger()))
    op.create_check_constraint(
        "ck_memberships_gender", TABLE, "gender IN ('female', 'male', 'other')"
    )
    op.create_check_constraint(
        "ck_memberships_national_id", TABLE, "national_id ~ '^[0-9]{7,8}$'"
    )
    op.create_check_constraint(
        "ck_memberships_date_of_birth", TABLE, "date_of_birth >= DATE '1900-01-01'"
    )
    op.create_check_constraint(
        "ck_memberships_share_capital_non_negative", TABLE, "share_capital >= 0"
    )
    op.create_index(
        "uq_memberships_sacco_national_id",
        TABLE,
        ["sacco_id", "national_id"],
        unique=True,
        postgresql_where=sa.text("national_id IS NOT NULL"),
    )
    op.create_table(
        "member_data_consents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "membership_id",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("notice_version", sa.Integer(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("purpose IN ('member_profile')", name="ck_consents_purpose"),
        sa.CheckConstraint(
            "withdrawn_at IS NULL OR withdrawn_at >= granted_at",
            name="ck_consents_withdrawn_after_granted",
        ),
    )
    op.create_index(
        "ix_member_data_consents_membership_id", "member_data_consents", ["membership_id"]
    )
    op.create_index(
        "uq_consents_one_active",
        "member_data_consents",
        ["membership_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("withdrawn_at IS NULL"),
    )


def downgrade():
    op.drop_table("member_data_consents")
    op.drop_index("uq_memberships_sacco_national_id", TABLE)
    for name in (
        "ck_memberships_share_capital_non_negative",
        "ck_memberships_date_of_birth",
        "ck_memberships_national_id",
        "ck_memberships_gender",
    ):
        op.drop_constraint(name, TABLE, type_="check")
    for column in ("share_capital", "date_of_birth", "gender", "national_id", "full_name"):
        op.drop_column(TABLE, column)
