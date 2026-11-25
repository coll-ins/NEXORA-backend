"""user phones, join codes, circle PIN columns and per-SACCO settings

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-17 10:10:00.000000

Section 1 (Neo) provides the tables Section 2 builds the join flow on: a phone
per user, a single-use join code per invited membership, the PIN state on the
membership itself, and the per-SACCO settings the loan rules are measured
against.
"""
import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_phones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phone", sa.String(20), nullable=False, unique=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("btrim(phone) <> ''", name="ck_user_phones_phone_not_blank"),
    )
    op.create_index("ix_user_phones_user_id", "user_phones", ["user_id"])

    op.create_table(
        "join_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "membership_id",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_digest", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_join_codes_attempts_non_negative"),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_join_codes_expires_after_creation"
        ),
    )
    op.create_index("ix_join_codes_membership_id", "join_codes", ["membership_id"])

    op.create_table(
        "sacco_settings",
        sa.Column(
            "sacco_id",
            sa.Integer(),
            sa.ForeignKey("saccos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("loan_cap", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "savings_multiple", sa.Integer(), nullable=False, server_default="3"
        ),
        sa.Column(
            "join_code_ttl_minutes", sa.Integer(), nullable=False, server_default="15"
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "loan_cap >= 0", name="ck_sacco_settings_loan_cap_non_negative"
        ),
        sa.CheckConstraint(
            "savings_multiple >= 0", name="ck_sacco_settings_multiple_non_negative"
        ),
        sa.CheckConstraint(
            "join_code_ttl_minutes > 0", name="ck_sacco_settings_ttl_positive"
        ),
    )

    op.add_column(
        "sacco_memberships", sa.Column("pin_hash", sa.String(255), nullable=True)
    )
    op.add_column(
        "sacco_memberships",
        sa.Column("pin_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sacco_memberships",
        sa.Column("pin_locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_memberships_pin_attempts_non_negative",
        "sacco_memberships",
        "pin_attempts >= 0",
    )


def downgrade():
    op.drop_constraint(
        "ck_memberships_pin_attempts_non_negative",
        "sacco_memberships",
        type_="check",
    )
    op.drop_column("sacco_memberships", "pin_locked_until")
    op.drop_column("sacco_memberships", "pin_attempts")
    op.drop_column("sacco_memberships", "pin_hash")
    op.drop_table("sacco_settings")
    op.drop_index("ix_join_codes_membership_id", table_name="join_codes")
    op.drop_table("join_codes")
    op.drop_index("ix_user_phones_user_id", table_name="user_phones")
    op.drop_table("user_phones")
