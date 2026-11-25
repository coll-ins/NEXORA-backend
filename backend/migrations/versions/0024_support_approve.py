"""the SACCO admin's consent permission: support.approve

Revision ID: 0024
Revises: 0023

The screens decide by permission name, and the Access requests page is gated on
`support.approve` (frontend `lib/perms.js`, docs/frontend/README.md). The name
was documented but never in the catalogue, so `GET /members/me/permissions`
would report it for nobody and the page would 403 for the only person allowed to
see it. The support routes already authorise with `is_admin_membership`, so this
adds no new power: it puts the existing rule in the catalogue under its name.

Member deposit and withdrawal requests (Neo's item 6) therefore become 0025.
"""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

PERMISSIONS = {
    "support.approve": "Approve or revoke a platform owner's support access",
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
            "WHERE r.code = 'circle_admin' AND p.code = 'support.approve' "
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
