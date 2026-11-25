"""Rename "circle" to "SACCO": three tables, their constraints and indexes, and
the SACCO admin job title

Revision ID: 0032
Revises: 0031

The product word is SACCO. Migrations 0010 to 0031 keep the old names because
they ran under them; this one moves a database that has them to the new names.

- tables circle_rules, circle_payment_details, circle_payment_detail_changes
- every constraint, index, sequence and trigger on them that carries the name
- the `circle_admin` job title (roles.code and name)

`audit_logs` rows are append-only and keep the action they were written with
(`member.circle_login`); new rows use `member.sacco_login`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

OLD_NEW = (
    ("circle_rules", "sacco_rules"),
    ("circle_payment_details", "sacco_payment_details"),
    ("circle_payment_detail_changes", "sacco_payment_detail_changes"),
)
# Longest first, so circle_payment_detail_changes is never half-renamed.
TOKENS = (
    ("circle_payment_detail", "sacco_payment_detail"),
    ("circle_rules", "sacco_rules"),
)


def _swap(name: str, pairs) -> str:
    for old, new in pairs:
        name = name.replace(old, new)
    return name


def _rename(tables, pairs):
    """Rename `tables`, then every constraint, index, sequence and trigger on
    them whose name carries an old token. Names are our own, never user input."""
    conn = op.get_bind()
    for old, new in tables:
        op.rename_table(old, new)
    for _, table in tables:
        for (name,) in conn.execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = CAST(:t AS regclass)"
            ),
            {"t": table},
        ).all():
            new = _swap(name, pairs)
            if new != name:
                op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{name}" TO "{new}"')
        for (name,) in conn.execute(
            sa.text(
                "SELECT i.indexrelid::regclass::text FROM pg_index i "
                "WHERE i.indrelid = CAST(:t AS regclass)"
            ),
            {"t": table},
        ).all():
            new = _swap(name, pairs)
            if new != name:  # a constraint's own index was renamed with it
                op.execute(f'ALTER INDEX IF EXISTS "{name}" RENAME TO "{new}"')
        for (name,) in conn.execute(
            sa.text(
                "SELECT s.relname FROM pg_class s "
                "JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i') "
                "WHERE s.relkind = 'S' AND d.refobjid = CAST(:t AS regclass)"
            ),
            {"t": table},
        ).all():
            new = _swap(name, pairs)
            if new != name:
                op.execute(f'ALTER SEQUENCE "{name}" RENAME TO "{new}"')
        for (name,) in conn.execute(
            sa.text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = CAST(:t AS regclass) AND NOT tgisinternal"
            ),
            {"t": table},
        ).all():
            new = _swap(name, pairs)
            if new != name:
                op.execute(f'ALTER TRIGGER "{name}" ON {table} RENAME TO "{new}"')


def upgrade():
    _rename(OLD_NEW, TOKENS)
    op.execute(
        "UPDATE roles SET code = 'sacco_admin', name = 'SACCO administrator' "
        "WHERE code = 'circle_admin'"
    )


def downgrade():
    op.execute(
        "UPDATE roles SET code = 'circle_admin', name = 'Circle administrator' "
        "WHERE code = 'sacco_admin'"
    )
    _rename(
        [(new, old) for old, new in OLD_NEW],
        [(new, old) for old, new in TOKENS],
    )
