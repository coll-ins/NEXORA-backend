"""An officer may cancel an approved pay-in change before it goes live

Revision ID: 0031
Revises: 0030

Adds the `cancelled` status. A cancelled change keeps its approval columns.
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

TABLE = "circle_payment_detail_changes"
NAME = "ck_circle_payment_detail_changes_status"


def upgrade():
    op.drop_constraint(NAME, TABLE, type_="check")
    op.create_check_constraint(
        NAME, TABLE, "status IN ('pending', 'approved', 'superseded', 'cancelled')"
    )


def downgrade():
    # The old rule has no 'cancelled'; a cancelled change was never live, so it
    # reads as superseded.
    op.execute(f"UPDATE {TABLE} SET status = 'superseded' WHERE status = 'cancelled'")
    op.drop_constraint(NAME, TABLE, type_="check")
    op.create_check_constraint(
        NAME, TABLE, "status IN ('pending', 'approved', 'superseded')"
    )
