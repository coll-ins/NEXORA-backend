"""A SACCO admin may override the loan-cap block on a reversal

Revision ID: 0030
Revises: 0029

`savings.reverse_override` is held by the SACCO admin title only.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

CODE = "savings.reverse_override"
DESCRIPTION = "Override the loan-cap block on a reversal"
ROLE = "circle_admin"


def upgrade():
    op.execute(
        sa.text(
            "INSERT INTO permissions (code, description) VALUES (:c, :d) "
            "ON CONFLICT (code) DO NOTHING"
        ).bindparams(c=CODE, d=DESCRIPTION)
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            "WHERE r.code = :role AND p.code = :c ON CONFLICT DO NOTHING"
        ).bindparams(role=ROLE, c=CODE)
    )


def downgrade():
    op.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id = "
            "(SELECT id FROM permissions WHERE code = :c)"
        ).bindparams(c=CODE)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE code = :c").bindparams(c=CODE))
