"""Section 1 done-when, checked against a migration-built database.

The migration must run clean on an empty database, the seeded catalogue must
match the application's constants, the audit table must reject changes, and the
loan and join tables must hold their constraints.

These are `schema` tests: they rebuild a separate `*_schema_test` database from
migrations, so they run with `pytest -m schema` and inside the CI selection.
"""

from pathlib import Path

import psycopg
import pytest
from flask_migrate import upgrade
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import create_app
from app.extensions import db
from app.models.access import Role
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.security.permissions import PERMISSIONS, ROLE_PERMISSIONS
from config import TestConfig

pytestmark = pytest.mark.schema

MIGRATIONS = str(Path(__file__).resolve().parents[1] / "backend" / "migrations")
_counter = iter(range(1, 100_000))


def _schema_test_url():
    """`nexora_test` -> `nexora_schema_test`, the database only schema tests touch."""
    url = make_url(TestConfig.SQLALCHEMY_DATABASE_URI)
    name = url.database or ""
    if not name.endswith("_test"):
        raise RuntimeError(
            "TEST_DATABASE_URL must point at a database whose name ends in _test."
        )
    return url.set(database=f"{name[: -len('_test')]}_schema_test")


def _ensure_database(url):
    admin = TestConfig.SQLALCHEMY_DATABASE_URI
    dsn = (
        make_url(admin)
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
        pytest.fail(f"Could not create database {url.database!r}: {exc}")

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


def make_user(s):
    from app.models.user import User

    user = User(email=f"access{next(_counter)}@example.com", password_hash="x")
    s.add(user)
    s.flush()
    return user


def make_sacco(s):
    from app.models.sacco import Sacco

    sacco = Sacco(name=f"Access Sacco {next(_counter)}")
    s.add(sacco)
    s.flush()
    return sacco


def make_membership(s, sacco, role=MembershipRole.MEMBER):
    membership = SaccoMembership(
        user_id=make_user(s).id,
        sacco_id=sacco.id,
        role=role,
        status=MembershipStatus.ACTIVE,
    )
    s.add(membership)
    s.flush()
    return membership


# --------------------------------------------------------------- catalogue

def test_the_five_job_titles_are_seeded(session):
    codes = {role.code for role in session.query(Role).all()}
    assert codes == set(ROLE_PERMISSIONS)


def test_the_seeded_catalogue_matches_the_application(session):
    permissions = {
        row[0] for row in session.execute(text("SELECT code FROM permissions"))
    }
    assert permissions == set(PERMISSIONS)

    pairs = {
        (role, permission)
        for role, permission in session.execute(
            text(
                "SELECT r.code, p.code FROM role_permissions rp "
                "JOIN roles r ON r.id = rp.role_id "
                "JOIN permissions p ON p.id = rp.permission_id"
            )
        )
    }
    expected = {
        (role, permission)
        for role, allowed in ROLE_PERMISSIONS.items()
        for permission in allowed
    }
    assert pairs == expected


def test_every_seeded_role_has_at_least_one_permission(session):
    for role_code in ROLE_PERMISSIONS:
        count = session.execute(
            text(
                "SELECT count(*) FROM role_permissions rp "
                "JOIN roles r ON r.id = rp.role_id WHERE r.code = :code"
            ),
            {"code": role_code},
        ).scalar_one()
        assert count > 0, role_code


def test_the_schema_creates_every_section_one_table(session):
    expected = {
        "roles",
        "permissions",
        "role_permissions",
        "membership_roles",
        "platform_grants",
        "user_phones",
        "join_codes",
        "sacco_settings",
        "loans",
        "loan_decisions",
        "loan_repayments",
    }
    present = {
        row[0]
        for row in session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
    }
    assert expected <= present


# ------------------------------------------------------------ platform grants

def test_platform_grants_exist_without_a_membership(session):
    user = make_user(session)
    session.execute(
        text("INSERT INTO platform_grants (user_id) VALUES (:id)"), {"id": user.id}
    )
    granted = session.execute(
        text("SELECT count(*) FROM platform_grants WHERE user_id = :id"),
        {"id": user.id},
    ).scalar_one()
    memberships = session.execute(
        text("SELECT count(*) FROM sacco_memberships WHERE user_id = :id"),
        {"id": user.id},
    ).scalar_one()
    assert granted == 1
    assert memberships == 0


def test_a_platform_grant_needs_a_real_user(session):
    with pytest.raises(IntegrityError):
        session.execute(text("INSERT INTO platform_grants (user_id) VALUES (999999)"))
        session.flush()
    session.rollback()


# ---------------------------------------------------------------- audit table

def test_the_audit_log_rejects_update_and_delete(session):
    """Section 1 done-when: an UPDATE on audit_logs fails."""
    for statement in (
        "UPDATE audit_logs SET action = 'tampered'",
        "DELETE FROM audit_logs",
        "TRUNCATE audit_logs CASCADE",
    ):
        session.execute(text("INSERT INTO audit_logs (action) VALUES ('login')"))
        with pytest.raises(DBAPIError):
            session.execute(text(statement))
        session.rollback()


# ------------------------------------------------------------- membership roles

def test_a_membership_cannot_hold_the_same_role_twice(session):
    sacco = make_sacco(session)
    membership = make_membership(session, sacco)
    role_id = session.query(Role).filter_by(code="member").one().id
    insert = text(
        "INSERT INTO membership_roles (membership_id, role_id) VALUES (:m, :r)"
    )
    session.execute(insert, {"m": membership.id, "r": role_id})
    with pytest.raises(IntegrityError):
        session.execute(insert, {"m": membership.id, "r": role_id})
        session.flush()
    session.rollback()


def test_a_membership_role_needs_a_real_membership(session):
    role_id = session.query(Role).filter_by(code="member").one().id
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO membership_roles (membership_id, role_id) "
                "VALUES (999999, :r)"
            ),
            {"r": role_id},
        )
        session.flush()
    session.rollback()


# ------------------------------------------------------------------ identity

def test_the_pin_columns_default_to_unset(session):
    membership = make_membership(session, make_sacco(session))
    session.refresh(membership)
    assert membership.pin_hash is None
    assert membership.pin_attempts == 0
    assert membership.pin_locked_until is None


def test_join_codes_store_a_digest_and_start_unused(session):
    membership = make_membership(session, make_sacco(session))
    session.execute(
        text(
            "INSERT INTO join_codes (membership_id, code_digest, expires_at) "
            "VALUES (:m, 'digest', now() + interval '15 minutes')"
        ),
        {"m": membership.id},
    )
    row = session.execute(
        text("SELECT code_digest, attempts, used_at FROM join_codes")
    ).one()
    assert row[0] == "digest"
    assert row[1] == 0
    assert row[2] is None


def test_a_join_code_cannot_expire_before_it_is_created(session):
    membership = make_membership(session, make_sacco(session))
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO join_codes (membership_id, code_digest, expires_at) "
                "VALUES (:m, 'digest', now() - interval '1 minute')"
            ),
            {"m": membership.id},
        )
        session.flush()
    session.rollback()


def test_sacco_settings_default_to_the_decided_values(session):
    sacco = make_sacco(session)
    session.execute(
        text("INSERT INTO sacco_settings (sacco_id) VALUES (:s)"), {"s": sacco.id}
    )
    row = session.execute(
        text(
            "SELECT loan_cap, savings_multiple, join_code_ttl_minutes "
            "FROM sacco_settings WHERE sacco_id = :s"
        ),
        {"s": sacco.id},
    ).one()
    assert row == (0, 3, 15)


# --------------------------------------------------------------------- loans

def test_a_loan_must_belong_to_a_membership_of_its_own_sacco(session):
    ours, theirs = make_sacco(session), make_sacco(session)
    membership = make_membership(session, ours)
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO loans "
                "(sacco_id, membership_id, principal, required_approvals) "
                "VALUES (:s, :m, 1000, 1)"
            ),
            {"s": theirs.id, "m": membership.id},
        )
        session.flush()
    session.rollback()


def test_one_approval_step_cannot_be_recorded_twice(session):
    sacco = make_sacco(session)
    membership = make_membership(session, sacco, MembershipRole.SACCO_ADMIN)
    session.execute(
        text(
            "INSERT INTO loans "
            "(sacco_id, membership_id, principal, required_approvals) "
            "VALUES (:s, :m, 1000, 2)"
        ),
        {"s": sacco.id, "m": membership.id},
    )
    loan_id = session.execute(text("SELECT id FROM loans")).scalar_one()
    decide = text(
        "INSERT INTO loan_decisions (loan_id, step, decision, "
        "decided_by_membership_id) VALUES (:l, 1, 'approve', :m)"
    )
    session.execute(decide, {"l": loan_id, "m": membership.id})
    with pytest.raises(IntegrityError):
        session.execute(decide, {"l": loan_id, "m": membership.id})
        session.flush()
    session.rollback()


def test_a_loan_needs_at_least_one_approval(session):
    sacco = make_sacco(session)
    membership = make_membership(session, sacco)
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO loans (sacco_id, membership_id, principal, "
                "required_approvals) VALUES (:s, :m, 1000, 0)"
            ),
            {"s": sacco.id, "m": membership.id},
        )
        session.flush()
    session.rollback()


def test_a_loan_repayment_points_at_one_ledger_row(session):
    """The unique transaction_id is what stops one ledger row repaying twice."""
    sacco = make_sacco(session)
    membership = make_membership(session, sacco, MembershipRole.SACCO_ADMIN)
    session.execute(
        text(
            "INSERT INTO loans "
            "(sacco_id, membership_id, principal, required_approvals) "
            "VALUES (:s, :m, 1000, 1)"
        ),
        {"s": sacco.id, "m": membership.id},
    )
    loan_id = session.execute(text("SELECT id FROM loans")).scalar_one()
    session.execute(
        text(
            "INSERT INTO savings_accounts (membership_id, sacco_id) VALUES (:m, :s)"
        ),
        {"m": membership.id, "s": sacco.id},
    )
    account_id = session.execute(text("SELECT id FROM savings_accounts")).scalar_one()
    session.execute(
        text(
            "INSERT INTO transactions (sacco_id, savings_account_id, type, amount, "
            "reference, created_by_membership_id) "
            "VALUES (:s, :a, 'deposit', 1000, 'loan-repay-1', :m)"
        ),
        {"s": sacco.id, "a": account_id, "m": membership.id},
    )
    transaction_id = session.execute(
        text("SELECT id FROM transactions WHERE reference = 'loan-repay-1'")
    ).scalar_one()
    repayment = text(
        "INSERT INTO loan_repayments (loan_id, transaction_id, amount) "
        "VALUES (:l, :t, 1000)"
    )
    session.execute(repayment, {"l": loan_id, "t": transaction_id})
    with pytest.raises(IntegrityError):
        session.execute(repayment, {"l": loan_id, "t": transaction_id})
        session.flush()
    session.rollback()
