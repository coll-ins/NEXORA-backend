"""reports and the SACCO account list by permission

Revision ID: 0019
Revises: 0018

The report routes and the SACCO-wide account list used to trust the legacy
membership role column, so any "staff" membership could read every account.
They now ask `can()` for:

* report.view: SACCO admin, secretary, finance officer, loans officer;
* savings.view_all: SACCO admin, secretary, finance officer (docs/roles-permissions.md).
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

PERMISSIONS = {
    "report.view": "Read the SACCO's reports",
    "savings.view_all": "Read every savings account in the SACCO",
}
GRANTS = {
    "report.view": ("circle_admin", "secretary", "finance_officer", "loans_officer"),
    "savings.view_all": ("circle_admin", "secretary", "finance_officer"),
}


def upgrade():
    for code, description in PERMISSIONS.items():
        op.execute(
            sa.text(
                "INSERT INTO permissions (code, description) VALUES (:c, :d) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(c=code, d=description)
        )
    for permission, roles in GRANTS.items():
        for role in roles:
            op.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "SELECT r.id, p.id FROM roles r, permissions p "
                    "WHERE r.code = :r AND p.code = :p ON CONFLICT DO NOTHING"
                ).bindparams(r=role, p=permission)
            )


def downgrade():
    for code in PERMISSIONS:
        op.execute(
            sa.text(
                "DELETE FROM role_permissions WHERE permission_id = "
                "(SELECT id FROM permissions WHERE code = :c)"
            ).bindparams(c=code)
        )
        delete = sa.text("DELETE FROM permissions WHERE code = :c")
        op.execute(delete.bindparams(c=code))
