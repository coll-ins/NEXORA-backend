"""Circle pay-in details and the two-officer change: circle_payment_details

Revision ID: 0028
Revises: 0027

The live table holds what members see on Deposit; the changes table holds each
proposed change until a different officer approves it. Seeds `payin.manage`
for the circle admin and the finance officer: two titles, so a circle always
has someone who can second a change.

Depends on 0027 (rules vote denominator), which follows 0026 (circle rules).
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

PERMISSION = ("payin.manage", "Set or approve where members pay in")
ROLES = ("circle_admin", "finance_officer")


def _checks(table):
    return [
        sa.CheckConstraint(
            "mpesa_type IN ('paybill', 'till')", name=f"ck_{table}_mpesa_type"
        ),
        sa.CheckConstraint(
            "(mpesa_type IS NULL) = (mpesa_number IS NULL)",
            name=f"ck_{table}_mpesa_pair",
        ),
        sa.CheckConstraint(
            "mpesa_number ~ '^[0-9]{5,7}$'", name=f"ck_{table}_mpesa_number"
        ),
        sa.CheckConstraint(
            "mpesa_number IS NULL OR display_name IS NOT NULL",
            name=f"ck_{table}_mpesa_needs_name",
        ),
        sa.CheckConstraint(
            "account_reference_hint IS NULL OR mpesa_type = 'paybill'",
            name=f"ck_{table}_hint_paybill_only",
        ),
        sa.CheckConstraint(
            "(bank_name IS NULL) = (bank_account_name IS NULL) "
            "AND (bank_name IS NULL) = (bank_account_number IS NULL)",
            name=f"ck_{table}_bank_trio",
        ),
        sa.CheckConstraint(
            "bank_account_number ~ '^[0-9]{6,20}$'",
            name=f"ck_{table}_bank_account_number",
        ),
        sa.CheckConstraint(
            "bank_branch IS NULL OR bank_name IS NOT NULL",
            name=f"ck_{table}_branch_needs_bank",
        ),
        sa.CheckConstraint(
            "bank_paybill_number ~ '^[0-9]{5,7}$'",
            name=f"ck_{table}_bank_paybill_number",
        ),
        sa.CheckConstraint(
            "bank_uses_shared_paybill = (bank_paybill_number IS NOT NULL)",
            name=f"ck_{table}_shared_paybill_pair",
        ),
        sa.CheckConstraint(
            "NOT bank_uses_shared_paybill OR bank_name IS NOT NULL",
            name=f"ck_{table}_shared_paybill_needs_bank",
        ),
        sa.CheckConstraint(
            "mpesa_number IS NOT NULL OR bank_name IS NOT NULL",
            name=f"ck_{table}_one_method",
        ),
    ]


def _value_columns():
    return [
        sa.Column("mpesa_type", sa.String(length=10)),
        sa.Column("mpesa_number", sa.String(length=7)),
        sa.Column("account_reference_hint", sa.String(length=60)),
        sa.Column("display_name", sa.String(length=60)),
        sa.Column("bank_name", sa.String(length=60)),
        sa.Column("bank_account_name", sa.String(length=80)),
        sa.Column("bank_account_number", sa.String(length=20)),
        sa.Column("bank_branch", sa.String(length=60)),
        sa.Column(
            "bank_uses_shared_paybill",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("bank_paybill_number", sa.String(length=7)),
    ]


def upgrade():
    op.create_table(
        "circle_payment_details",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sacco_id",
            sa.Integer(),
            sa.ForeignKey("saccos.id"),
            nullable=False,
            unique=True,
        ),
        *_value_columns(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        *_checks("circle_payment_details"),
    )
    op.create_table(
        "circle_payment_detail_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sacco_id", sa.Integer(), sa.ForeignKey("saccos.id"), nullable=False),
        *_value_columns(),
        sa.Column(
            "status", sa.String(length=12), nullable=False, server_default="pending"
        ),
        sa.Column(
            "proposed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "proposed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("approved_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        *_checks("circle_payment_detail_changes"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'superseded')",
            name="ck_circle_payment_detail_changes_status",
        ),
        sa.CheckConstraint(
            "(status = 'approved') = "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_circle_payment_detail_changes_approval_pair",
        ),
        sa.CheckConstraint(
            "approved_by IS NULL OR approved_by <> proposed_by",
            name="ck_circle_payment_detail_changes_two_people",
        ),
    )
    op.create_index(
        "ix_circle_payment_detail_changes_sacco_id",
        "circle_payment_detail_changes",
        ["sacco_id"],
    )
    op.create_index(
        "uq_circle_payment_detail_changes_one_pending",
        "circle_payment_detail_changes",
        ["sacco_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # When a title was given, so a change can only be approved by someone who was
    # already an officer when it was proposed. Existing rows get the migration time.
    op.add_column(
        "membership_roles",
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    code, description = PERMISSION
    op.execute(
        sa.text(
            "INSERT INTO permissions (code, description) VALUES (:c, :d) "
            "ON CONFLICT (code) DO NOTHING"
        ).bindparams(c=code, d=description)
    )
    for role in ROLES:
        op.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "SELECT r.id, p.id FROM roles r, permissions p "
                "WHERE r.code = :role AND p.code = :c ON CONFLICT DO NOTHING"
            ).bindparams(role=role, c=code)
        )


def downgrade():
    op.drop_column("membership_roles", "granted_at")
    code = PERMISSION[0]
    op.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id = "
            "(SELECT id FROM permissions WHERE code = :c)"
        ).bindparams(c=code)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE code = :c").bindparams(c=code))
    op.drop_index(
        "uq_circle_payment_detail_changes_one_pending",
        "circle_payment_detail_changes",
    )
    op.drop_index(
        "ix_circle_payment_detail_changes_sacco_id", "circle_payment_detail_changes"
    )
    op.drop_table("circle_payment_detail_changes")
    op.drop_table("circle_payment_details")
