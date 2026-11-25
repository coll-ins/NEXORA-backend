"""revoke UPDATE and DELETE on audit_logs

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-17 10:30:00.000000

Section 1 (Neo): "Make audit_logs insert-only by revoking UPDATE and DELETE."

Two layers, on purpose:

* the trigger from 0005 raises on any UPDATE, DELETE or TRUNCATE, which holds
  even for the table owner the application connects as;
* this migration revokes the privileges from PUBLIC, so a role that is only
  granted on this table cannot rewrite history either.

Both are needed: a privilege check is the right code, and the trigger is what
still holds when the connection is the owner.
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM PUBLIC")
    op.execute(
        "COMMENT ON TABLE audit_logs IS "
        "'Append-only. UPDATE and DELETE are revoked from PUBLIC and rejected by "
        "trg_audit_logs_append_only (migration 0005).'"
    )


def downgrade():
    # Restores the default (no explicit grant), not a grant: nothing had one.
    op.execute("GRANT UPDATE, DELETE, TRUNCATE ON audit_logs TO PUBLIC")
    op.execute("COMMENT ON TABLE audit_logs IS NULL")
