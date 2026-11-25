"""The least-privilege `nexora_app` role (migration 0014).

Two kinds of proof:

1. the catalogue says the role holds no UPDATE, DELETE or TRUNCATE on the
   history tables, and does hold SELECT and INSERT;
2. a real connection as that role is refused an UPDATE and a DELETE, and can
   still append an audit row.

The second matters because the append-only trigger from `0005` would refuse the
write anyway. The privilege check is what proves the grant itself is right.

Migration 0014 skips the role when the migrating user may not create roles (a
plain schema owner without CREATEROLE). On such a machine the role has to be
provisioned once with `backend/scripts/nexora_app_role.sql`, so these tests skip
with that instruction instead of failing the suite.
"""

from pathlib import Path

import psycopg
import pytest
from flask_migrate import upgrade
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app import create_app
from app.extensions import db
from config import TestConfig

pytestmark = pytest.mark.schema

MIGRATIONS = str(Path(__file__).resolve().parents[1] / "backend" / "migrations")
APP_ROLE = "nexora_app"
TEST_PASSWORD = "app-role-test-only"
HISTORY_TABLES = ("transactions", "audit_logs")
PROVISION_HINT = (
    f"{APP_ROLE} is not provisioned in this cluster and the migrating user may "
    "not create roles. Run backend/scripts/nexora_app_role.sql as an "
    "administrator, then run the suite again."
)


def _schema_test_url():
    url = make_url(TestConfig.SQLALCHEMY_DATABASE_URI)
    name = url.database or ""
    if not name.endswith("_test"):
        raise RuntimeError(
            "TEST_DATABASE_URL must point at a database whose name ends in _test."
        )
    return url.set(database=f"{name[: -len('_test')]}_schema_test")


def _ensure_database(url):
    dsn = (
        make_url(TestConfig.SQLALCHEMY_DATABASE_URI)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{url.database}"')


@pytest.fixture(scope="module")
def schema_app():
    url = _schema_test_url()
    try:
        _ensure_database(url)
    except psycopg.Error as exc:
        pytest.fail(
            f"Could not create database {url.database!r}. Create it once with: "
            f"psql -U postgres -c 'CREATE DATABASE {url.database} "
            f"OWNER {url.username};'  ({exc})"
        )

    class SchemaConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = url.render_as_string(hide_password=False)

    app = create_app(SchemaConfig)
    with app.app_context():
        db.session.remove()
        db.session.execute(text("DROP SCHEMA public CASCADE"))
        db.session.execute(text("CREATE SCHEMA public"))
        db.session.commit()
        upgrade(directory=MIGRATIONS, revision="head")
        yield app
        db.session.remove()


@pytest.fixture
def session(schema_app):
    yield db.session
    db.session.rollback()


@pytest.fixture
def app_role(session):
    """The provisioned role, or a skip explaining how to provision it."""
    found = session.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": APP_ROLE}
    ).scalar_one_or_none()
    if found is None:
        pytest.skip(PROVISION_HINT)
    return APP_ROLE


@pytest.fixture
def can_manage_roles(session):
    return session.execute(
        text(
            "SELECT rolsuper OR rolcreaterole FROM pg_roles "
            "WHERE rolname = current_user"
        )
    ).scalar_one()


def test_the_role_exists(app_role):
    assert app_role == APP_ROLE


def test_the_role_holds_no_rewrite_privilege_on_history(session, app_role):
    for table in HISTORY_TABLES:
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            held = session.execute(
                text("SELECT has_table_privilege(:role, :table, :privilege)"),
                {"role": app_role, "table": table, "privilege": privilege},
            ).scalar_one()
            assert held is False, f"{app_role} must not hold {privilege} on {table}"

        for privilege in ("SELECT", "INSERT"):
            held = session.execute(
                text("SELECT has_table_privilege(:role, :table, :privilege)"),
                {"role": app_role, "table": table, "privilege": privilege},
            ).scalar_one()
            assert held is True, f"{app_role} needs {privilege} on {table}"


def test_the_role_can_still_run_the_application(session, app_role):
    """Ordinary tables are read/write, and the balance column is updatable."""
    for table, privilege in (
        ("users", "INSERT"),
        ("savings_accounts", "UPDATE"),
        ("sacco_memberships", "DELETE"),
        ("roles", "SELECT"),
    ):
        held = session.execute(
            text("SELECT has_table_privilege(:role, :table, :privilege)"),
            {"role": app_role, "table": table, "privilege": privilege},
        ).scalar_one()
        assert held is True, f"{app_role} needs {privilege} on {table}"


def test_the_role_cannot_change_the_schema(session, app_role):
    held = session.execute(
        text("SELECT has_schema_privilege(:role, 'public', 'CREATE')"),
        {"role": app_role},
    ).scalar_one()
    assert held is False


def test_a_connection_as_the_role_is_refused_a_rewrite(
    session, app_role, can_manage_roles
):
    """Log in as the role, then try to rewrite history for real."""
    if not can_manage_roles:
        pytest.skip(
            f"The migrating user may not ALTER ROLE {app_role}, so it cannot be "
            "given a login here."
        )

    session.execute(
        text(f"ALTER ROLE {app_role} LOGIN PASSWORD '{TEST_PASSWORD}'")
    )
    session.commit()
    url = _schema_test_url().set(username=app_role, password=TEST_PASSWORD)
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)

    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for statement in (
                "UPDATE transactions SET amount = 1",
                "DELETE FROM transactions",
                "UPDATE audit_logs SET action = 'tampered'",
                "DELETE FROM audit_logs",
                "TRUNCATE audit_logs CASCADE",
            ):
                with pytest.raises(
                    (psycopg.errors.InsufficientPrivilege, psycopg.Error)
                ):
                    conn.execute(statement)

            # Reading and appending still work.
            conn.execute("SELECT count(*) FROM transactions").fetchone()
            conn.execute("INSERT INTO audit_logs (action) VALUES ('role.test')")
    finally:
        session.rollback()
        session.execute(text(f"ALTER ROLE {app_role} NOLOGIN PASSWORD NULL"))
        session.commit()
