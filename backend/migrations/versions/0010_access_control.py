"""roles, permissions, membership_roles and platform_grants

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-17 10:00:00.000000

Section 1 (Neo). The access model becomes data: five job-title roles as rows,
their permissions, the roles each membership holds, and the platform grants that
let an owner act without a membership.

The catalogue is repeated literally here on purpose: a migration must keep
working after the application code moves on. `tests/test_access_schema.py`
fails if this seed and `app/security/permissions.py` ever disagree.
"""
import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

ROLE_NAMES = {
    "circle_admin": "Circle administrator",
    "secretary": "Secretary",
    "finance_officer": "Finance officer",
    "loans_officer": "Loans officer",
    "member": "Member",
}

PERMISSIONS = {
    "sacco.view": "Read this SACCO's details",
    "sacco.manage": "Create, edit, activate or deactivate a SACCO",
    "member.view": "Read the SACCO's member list",
    "member.manage": "Add a member, invite a phone, change a role or status",
    "savings.view": "Read savings accounts and their history",
    "savings.post": "Post deposits, withdrawals, interest and fees",
    "savings.reverse": "Reverse a posted ledger entry",
    "loan.view": "Read loan requests and their decisions",
    "loan.request": "Request a loan",
    "loan.approve": "Approve or reject one step of a loan",
    "loan.disburse": "Disburse an approved loan",
    "loan.repay": "Repay a loan",
    "audit.view": "Read the audit trail",
    "dashboard.view": "See the dashboard for the caller's role",
}

ROLE_PERMISSIONS = {
    "circle_admin": (
        "sacco.view",
        "member.view",
        "member.manage",
        "savings.view",
        "savings.post",
        "savings.reverse",
        "loan.view",
        "loan.approve",
        "audit.view",
        "dashboard.view",
    ),
    "secretary": (
        "sacco.view",
        "member.view",
        "member.manage",
        "savings.view",
        "loan.view",
        "audit.view",
        "dashboard.view",
    ),
    "finance_officer": (
        "sacco.view",
        "savings.view",
        "savings.post",
        "savings.reverse",
        "loan.view",
        "loan.disburse",
        "loan.repay",
        "dashboard.view",
    ),
    "loans_officer": (
        "sacco.view",
        "member.view",
        "savings.view",
        "loan.view",
        "loan.approve",
        "dashboard.view",
    ),
    "member": (
        "sacco.view",
        "savings.view",
        "loan.request",
        "loan.repay",
        "dashboard.view",
    ),
}

# The legacy enum column maps onto a job title for existing rows.
LEGACY_BACKFILL = (
    ("sacco_admin", "circle_admin"),
    ("staff", "secretary"),
    ("member", "member"),
)


def upgrade():
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.String(255), nullable=True),
    )
    op.create_table(
        "role_permissions",
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "permission_id",
            sa.Integer(),
            sa.ForeignKey("permissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "membership_roles",
        sa.Column(
            "membership_id",
            sa.Integer(),
            sa.ForeignKey("sacco_memberships.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "platform_grants",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "granted_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True
        ),
    )

    for code, name in ROLE_NAMES.items():
        op.execute(
            sa.text(
                "INSERT INTO roles (code, name) VALUES (:code, :name) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(code=code, name=name)
        )
    for code, description in PERMISSIONS.items():
        op.execute(
            sa.text(
                "INSERT INTO permissions (code, description) VALUES (:code, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(code=code, description=description)
        )
    for role_code, permission_codes in ROLE_PERMISSIONS.items():
        for permission_code in permission_codes:
            op.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "SELECT r.id, p.id FROM roles r, permissions p "
                    "WHERE r.code = :role AND p.code = :permission "
                    "ON CONFLICT DO NOTHING"
                ).bindparams(role=role_code, permission=permission_code)
            )

    # Existing memberships keep their access: enum role -> job title.
    for legacy, job_title in LEGACY_BACKFILL:
        op.execute(
            sa.text(
                "INSERT INTO membership_roles (membership_id, role_id) "
                "SELECT m.id, r.id FROM sacco_memberships m, roles r "
                "WHERE m.role::text = :legacy AND r.code = :role "
                "ON CONFLICT DO NOTHING"
            ).bindparams(legacy=legacy, role=job_title)
        )

    # Existing platform owners hold a grant, so `can()` needs no membership.
    op.execute(
        "INSERT INTO platform_grants (user_id) "
        "SELECT id FROM users WHERE is_platform_owner "
        "ON CONFLICT DO NOTHING"
    )


def downgrade():
    op.drop_table("platform_grants")
    op.drop_table("membership_roles")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
