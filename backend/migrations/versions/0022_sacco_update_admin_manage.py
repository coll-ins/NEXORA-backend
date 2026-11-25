"""permissions for the last role-column guards: sacco.update, admin.manage

Revision ID: 0022
Revises: 0021

Every SACCO-scoped route now asks `can()`. Two decisions still came from the
membership role column, so they become permissions:

* sacco.update (rename the SACCO): the SACCO admin, as before;
* admin.manage (make, change or remove a SACCO admin): no job title holds it,
  so only the platform owner, who holds every permission, can.
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

PERMISSIONS = {
    "sacco.update": "Rename the SACCO",
    "admin.manage": "Make, change or remove a SACCO admin",
}


def upgrade():
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
            "WHERE r.code = 'circle_admin' AND p.code = 'sacco.update' "
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
        delete = sa.text("DELETE FROM permissions WHERE code = :c")
        op.execute(delete.bindparams(c=code))
