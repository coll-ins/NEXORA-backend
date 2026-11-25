"""Indexes for paging the audit log newest-first

Revision ID: 0041
Revises: 0040

The audit log is read newest-first (created_at DESC, id DESC), per SACCO or
platform-wide. Without a matching index each page sorts every row it matches.
The per-SACCO index starts with sacco_id, so it replaces the single-column one.
"""

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX ix_audit_logs_sacco_created "
        "ON audit_logs (sacco_id, created_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX ix_audit_logs_created ON audit_logs (created_at DESC, id DESC)"
    )
    op.drop_index("ix_audit_logs_sacco_id", table_name="audit_logs")


def downgrade():
    op.create_index("ix_audit_logs_sacco_id", "audit_logs", ["sacco_id"])
    op.drop_index("ix_audit_logs_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_sacco_created", table_name="audit_logs")
