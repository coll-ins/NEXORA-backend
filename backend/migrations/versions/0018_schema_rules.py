"""loan rules from the agreed schema: terms, reasons, one open loan, append-only

Revision ID: 0018
Revises: 0017

The agreed rules (docs/roles-permissions.md) that earlier migrations did not hold:

* a SACCO admin never approves loans and cannot read the audit log, and a
  secretary cannot manage members (role_permissions rows removed);
* a loan has a term in months (> 0);
* a rejection carries a reason;
* one open loan (requested, approved or disbursed) per member;
* loan decisions and loan repayments are append only, like the ledger.
"""
import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

REMOVED = (
    ("circle_admin", "loan.approve"),
    ("circle_admin", "audit.view"),
    ("secretary", "member.manage"),
)
GRANT = (
    "INSERT INTO role_permissions (role_id, permission_id) "
    "SELECT r.id, p.id FROM roles r, permissions p "
    "WHERE r.code = :role AND p.code = :permission ON CONFLICT DO NOTHING"
)
REVOKE = (
    "DELETE FROM role_permissions WHERE "
    "role_id = (SELECT id FROM roles WHERE code = :role) AND "
    "permission_id = (SELECT id FROM permissions WHERE code = :permission)"
)
APPEND_ONLY = ("loan_decisions", "loan_repayments")


def upgrade():
    # Link disbursements made before 0017 to their ledger row (from the audit row
    # "loan:<id>/transaction:<tx>"), so the reversal route refuses them too.
    op.execute(
        r"""
        UPDATE loans l
        SET disbursement_transaction_id = m.parts[2]::int
        FROM (
            SELECT regexp_match(target, '^loan:(\d+)/transaction:(\d+)$') AS parts
            FROM audit_logs
            WHERE action = 'loan.disburse'
        ) m
        WHERE m.parts IS NOT NULL
          AND l.id = m.parts[1]::int
          AND l.disbursement_transaction_id IS NULL
        """
    )
    for role, permission in REMOVED:
        op.execute(sa.text(REVOKE).bindparams(role=role, permission=permission))

    op.add_column("loans", sa.Column("term_months", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_loans_term_months_positive",
        "loans",
        "term_months IS NULL OR term_months > 0",
    )
    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT membership_id FROM loans "
            "WHERE status IN ('requested', 'approved', 'disbursed') "
            "GROUP BY membership_id HAVING count(*) > 1"
        )
    ).scalars().all()
    if duplicates:
        raise RuntimeError(
            "Migration 0018 allows one open loan per member, but these memberships "
            f"hold more than one: {sorted(duplicates)}. Close or reject the extra "
            "loans, then run the upgrade again."
        )
    op.create_index(
        "uq_loans_one_open_per_member",
        "loans",
        ["membership_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('requested', 'approved', 'disbursed')"),
    )

    op.add_column("loan_decisions", sa.Column("reason", sa.Text(), nullable=True))
    # Rejections recorded before this migration carry no reason; mark them so the
    # check below can be added to a database that already has some.
    op.execute(
        "UPDATE loan_decisions "
        "SET reason = 'No reason recorded (before migration 0018)' "
        "WHERE decision = 'reject' AND btrim(coalesce(reason, '')) = ''"
    )
    op.create_check_constraint(
        "ck_loan_decisions_reject_has_reason",
        "loan_decisions",
        "decision <> 'reject' OR btrim(coalesce(reason, '')) <> ''",
    )

    op.execute(
        """
        CREATE FUNCTION forbid_loan_history_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'rows in % are append only', TG_TABLE_NAME
                USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_loan_history_change()"
        )


def downgrade():
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION forbid_loan_history_change()")
    op.drop_constraint(
        "ck_loan_decisions_reject_has_reason", "loan_decisions", type_="check"
    )
    op.drop_column("loan_decisions", "reason")
    op.drop_index("uq_loans_one_open_per_member", table_name="loans")
    op.drop_constraint("ck_loans_term_months_positive", "loans", type_="check")
    op.drop_column("loans", "term_months")
    for role, permission in REMOVED:
        op.execute(sa.text(GRANT).bindparams(role=role, permission=permission))
