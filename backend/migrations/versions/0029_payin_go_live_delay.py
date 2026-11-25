"""Pay-in changes go live 24 hours after the second officer approves

Revision ID: 0029
Revises: 0028

`go_live_at` is approved_at plus the delay. Members and officers are alerted
during the window, so a change nobody expected can be questioned before money
moves. Rows approved before this migration were live at once: go_live_at is
set to approved_at for them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

TABLE = "circle_payment_detail_changes"
PAIR = "ck_circle_payment_detail_changes_approval_pair"
GO_LIVE = "ck_circle_payment_detail_changes_go_live"


def upgrade():
    op.add_column(TABLE, sa.Column("go_live_at", sa.DateTime(timezone=True)))
    op.execute(f"UPDATE {TABLE} SET go_live_at = approved_at")
    op.drop_constraint(PAIR, TABLE, type_="check")
    op.create_check_constraint(
        PAIR,
        TABLE,
        "(approved_by IS NULL) = (approved_at IS NULL) "
        "AND (status <> 'approved' OR approved_by IS NOT NULL)",
    )
    op.create_check_constraint(
        GO_LIVE,
        TABLE,
        "(approved_at IS NULL) = (go_live_at IS NULL) "
        "AND (go_live_at IS NULL OR go_live_at >= approved_at)",
    )


def downgrade():
    # A superseded change that kept its approval would break the old pair rule.
    op.execute(
        f"UPDATE {TABLE} SET approved_by = NULL, approved_at = NULL, "
        "go_live_at = NULL WHERE status = 'superseded'"
    )
    op.drop_constraint(GO_LIVE, TABLE, type_="check")
    op.drop_constraint(PAIR, TABLE, type_="check")
    op.create_check_constraint(
        PAIR,
        TABLE,
        "(status = 'approved') = "
        "(approved_by IS NOT NULL AND approved_at IS NOT NULL)",
    )
    op.drop_column(TABLE, "go_live_at")
