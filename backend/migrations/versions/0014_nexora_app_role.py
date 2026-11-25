"""least-privilege application role: nexora_app

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-17 12:00:00.000000

Section 1 (Neo). The application has been connecting as the database owner,
which can do anything. This creates a separate role for it:

* read and write on ordinary tables, so the app keeps working;
* read and insert only on `transactions` and `audit_logs`, because history is
  never rewritten — an UPDATE, DELETE or TRUNCATE is refused by privilege;
* usage on the sequences, so inserts get ids;
* no DDL: the role cannot create, alter or drop anything, and migrations stay
  the owner's job.

The role is created NOLOGIN with no password on purpose: a migration must not
carry a credential. A deployment (or a developer's `.env`) enables login and
sets the password out of band:

    ALTER ROLE nexora_app LOGIN PASSWORD '...';
    # then point DATABASE_URL at nexora_app instead of the owner

Role creation needs a privileged connection. A migration is often run by an
ordinary database user that owns the schema but not the cluster (CREATEROLE is
missing), so this migration degrades instead of failing: when it cannot create
the role it logs what to do and leaves the rest of the migration chain alone.
An administrator provisions the role once, per cluster, with:

    psql -U postgres -d <database> -v owner=<migration role> \\
        -f backend/scripts/nexora_app_role.sql

Running `flask db upgrade` again afterwards attaches the grants from here.
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

APP_ROLE = "nexora_app"
# Tables whose rows must never change once written.
HISTORY_TABLES = "transactions, audit_logs"
PROVISION_HINT = (
    f"Provision it once as an administrator with "
    f"'psql -U postgres -d <database> -v owner=<migration role> "
    f"-f backend/scripts/nexora_app_role.sql', then run 'flask db upgrade' again."
)

log = logging.getLogger("alembic.runtime.migration")


def _scalar(sql: str):
    return op.get_bind().execute(sa.text(sql)).scalar()


def _role_exists() -> bool:
    return (
        _scalar(f"SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}'") is not None
    )


def _can_create_roles() -> bool:
    """True when the connected role may create or alter other roles."""
    return bool(
        _scalar(
            "SELECT rolsuper OR rolcreaterole FROM pg_roles "
            "WHERE rolname = current_user"
        )
    )


def _can_grant_on_database() -> bool:
    """True when the connected role owns the database or is a superuser."""
    return bool(
        _scalar(
            """
            SELECT r.rolsuper OR EXISTS (
                SELECT 1 FROM pg_database d
                WHERE d.datname = current_database() AND d.datdba = r.oid
            )
            FROM pg_roles r
            WHERE r.rolname = current_user
            """
        )
    )


def upgrade():
    if not _role_exists():
        if not _can_create_roles():
            log.warning(
                "Skipping the %s role: the current database user may not create "
                "roles. %s",
                APP_ROLE,
                PROVISION_HINT,
            )
            return
        op.execute(f"CREATE ROLE {APP_ROLE} NOLOGIN")

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        f"TO {APP_ROLE}"
    )
    # Take history back: read and append only, never rewrite.
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE ON {HISTORY_TABLES} FROM {APP_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")

    # Tables added by later migrations inherit the same baseline.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )

    if _can_grant_on_database():
        op.execute(
            f"""
            DO $$
            BEGIN
                EXECUTE format('GRANT CONNECT ON DATABASE %I TO {APP_ROLE}',
                               current_database());
            END
            $$;
            """
        )
    else:
        log.warning(
            "Skipping the database CONNECT grant for %s: the current database "
            "user does not own this database. Add it from an administrator "
            "connection if the role must connect directly.",
            APP_ROLE,
        )


def downgrade():
    if not _role_exists():
        return

    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE ALL ON SEQUENCES FROM {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE ALL ON TABLES FROM {APP_ROLE}"
    )
    if _can_grant_on_database():
        op.execute(
            f"""
            DO $$
            BEGIN
                EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {APP_ROLE}',
                               current_database());
            END
            $$;
            """
        )
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {APP_ROLE}")

    if not _can_create_roles():
        log.warning(
            "Left the %s role in place: the current database user may not drop "
            "roles. The grants this database made are gone; an operator can "
            "remove the role with DROP OWNED BY in each database that used it, "
            "then DROP ROLE.",
            APP_ROLE,
        )
        return

    # Roles are cluster-wide but grants are per database. Dropping the role from
    # here would fail (and break the downgrade of every other database) as soon
    # as it still holds privileges somewhere else -- a dev database, the test
    # database. So the role is dropped only once this database was the last one
    # using it; otherwise the grants above are all this migration owns, and the
    # role itself is left for an operator to remove with DROP OWNED BY.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}')
               AND NOT EXISTS (
                   SELECT 1
                   FROM pg_shdepend d
                   JOIN pg_roles r ON r.oid = d.refobjid
                   WHERE r.rolname = '{APP_ROLE}'
                     AND d.dbid <> 0
                     AND d.dbid <> (
                         SELECT oid FROM pg_database WHERE datname = current_database()
                     )
               )
            THEN
                DROP ROLE {APP_ROLE};
            END IF;
        END
        $$;
        """
    )
