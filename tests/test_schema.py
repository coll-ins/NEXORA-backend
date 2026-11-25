from pathlib import Path

import psycopg
import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import create_app
from app.extensions import db
from app.models.ledger import Transaction, TransactionType
from app.models.membership import MembershipRole, SaccoMembership
from app.models.sacco import Sacco, SaccoStatus
from app.models.savings import SavingsAccount
from app.models.user import User
from config import TestConfig

pytestmark = pytest.mark.schema

MIGRATIONS = str(Path(__file__).resolve().parents[1] / "backend" / "migrations")
_counter = iter(range(1, 100_000))


def _reset_schema():
    db.session.remove()
    db.session.execute(text("DROP SCHEMA public CASCADE"))
    db.session.execute(text("CREATE SCHEMA public"))
    db.session.commit()


def _schema_test_url():
    """`nexora_test` -> `nexora_schema_test`: a database only these tests touch.

    They drop and rebuild the whole schema, so sharing nexora_test with the other
    tests (or with anything else connected to it) can block or wreck both.
    """
    url = make_url(TestConfig.SQLALCHEMY_DATABASE_URI)
    name = url.database or ""
    if not name.endswith("_test"):
        raise RuntimeError(
            "TEST_DATABASE_URL must point at a database whose name ends in _test."
        )
    return url.set(database=f"{name[: -len('_test')]}_schema_test")


def _ensure_database(url):
    """Create the schema-test database if it does not exist yet."""
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
        pytest.fail(
            f"Could not create database {url.database!r}. Create it once with: "
            f"psql -U postgres -c 'CREATE DATABASE {url.database} "
            f"OWNER {url.username};'  ({exc})"
        )

    class SchemaConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = url.render_as_string(hide_password=False)

    app = create_app(SchemaConfig)
    with app.app_context():
        _reset_schema()
        upgrade(directory=MIGRATIONS, revision="head")
        yield app
        db.session.remove()
        downgrade(directory=MIGRATIONS, revision="base")


@pytest.fixture
def session(schema_app):
    yield db.session
    db.session.rollback()


# Helpers

def make_user(s):
    u = User(email=f"user{next(_counter)}@example.com", password_hash="x")
    s.add(u)
    s.flush()
    return u


def make_sacco(s, status=SaccoStatus.ACTIVE):
    sacco = Sacco(name=f"Sacco {next(_counter)}", status=status)
    s.add(sacco)
    s.flush()
    return sacco


def make_membership(s, user, sacco, role=MembershipRole.MEMBER):
    m = SaccoMembership(user_id=user.id, sacco_id=sacco.id, role=role)
    s.add(m)
    s.flush()
    return m


def make_savings(s, membership, sacco, balance=0):
    acc = SavingsAccount(
        membership_id=membership.id, sacco_id=sacco.id, balance=balance
    )
    s.add(acc)
    s.flush()
    return acc


def post(s, acc, type_, amount, **extra):
    """Post a ledger row as the account's own membership."""
    fields = {
        "sacco_id": acc.sacco_id,
        "savings_account_id": acc.id,
        "type": type_,
        "amount": amount,
        "reference": f"ref-{next(_counter)}",
        "created_by_membership_id": acc.membership_id,
    }
    tx = Transaction(**{**fields, **extra})
    s.add(tx)
    s.flush()
    return tx


def make_ledger_row(s, amount=100):
    sacco = make_sacco(s)
    m = make_membership(s, make_user(s), sacco)
    acc = make_savings(s, m, sacco)
    return post(s, acc, TransactionType.DEPOSIT, amount)


# Constraint tests

def test_negative_balance_rejected(session):
    sacco = make_sacco(session)
    m = make_membership(session, make_user(session), sacco)
    with pytest.raises(IntegrityError):
        make_savings(session, m, sacco, balance=-1)


def test_duplicate_membership_rejected(session):
    sacco = make_sacco(session)
    user = make_user(session)
    make_membership(session, user, sacco)
    with pytest.raises(IntegrityError):
        make_membership(session, user, sacco)


def test_suspended_is_valid_status(session):
    sacco = make_sacco(session, status=SaccoStatus.SUSPENDED)
    session.refresh(sacco)
    assert sacco.status is SaccoStatus.SUSPENDED


def test_unknown_sacco_status_rejected(session):
    with pytest.raises(DBAPIError):
        session.execute(
            text("INSERT INTO saccos (name, status) VALUES ('X', 'frozen')")
        )


def test_savings_account_must_match_membership_sacco(session):
    a = make_sacco(session)
    b = make_sacco(session)
    m = make_membership(session, make_user(session), a)
    with pytest.raises(IntegrityError):
        make_savings(session, m, b)


def test_zero_amount_ledger_row_rejected(session):
    with pytest.raises(IntegrityError):
        make_ledger_row(session, amount=0)


def test_ledger_row_cannot_be_updated(session):
    tx = make_ledger_row(session)
    with pytest.raises(DBAPIError):
        session.execute(
            text("UPDATE transactions SET amount = 999 WHERE id = :id"),
            {"id": tx.id},
        )


def test_ledger_row_cannot_be_deleted(session):
    tx = make_ledger_row(session)
    with pytest.raises(DBAPIError):
        session.execute(
            text("DELETE FROM transactions WHERE id = :id"),
            {"id": tx.id},
        )

def test_sacco_names_must_be_unique_ignoring_case_and_spaces(session):
    session.add(Sacco(name="Unique Test Sacco"))
    session.flush()
    session.add(Sacco(name=" UNIQUE test sacco "))
    with pytest.raises(IntegrityError):
        session.flush()


def test_blank_sacco_name_rejected(session):
    session.add(Sacco(name="   "))
    with pytest.raises(IntegrityError):
        session.flush()


def test_membership_can_have_only_one_savings_account(session):
    sacco = make_sacco(session)
    membership = make_membership(session, make_user(session), sacco)
    make_savings(session, membership, sacco)
    session.add(SavingsAccount(membership_id=membership.id, sacco_id=sacco.id))
    with pytest.raises(IntegrityError):
        session.flush()


def test_ledger_rows_drive_the_balance(session):
    tx = make_ledger_row(session, amount=500)
    acc = session.get(SavingsAccount, tx.savings_account_id)
    post(session, acc, TransactionType.WITHDRAWAL, 200)
    session.refresh(acc)
    assert acc.balance == 300


def test_withdrawal_over_balance_rejected(session):
    tx = make_ledger_row(session, amount=100)
    acc = session.get(SavingsAccount, tx.savings_account_id)
    with pytest.raises(IntegrityError):
        post(session, acc, TransactionType.WITHDRAWAL, 101)


def test_balance_cannot_be_edited_directly(session):
    tx = make_ledger_row(session)
    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE savings_accounts SET balance = 1000000 WHERE id = :id"),
            {"id": tx.savings_account_id},
        )


def test_account_cannot_start_with_a_balance(session):
    sacco = make_sacco(session)
    m = make_membership(session, make_user(session), sacco)
    with pytest.raises(IntegrityError):
        make_savings(session, m, sacco, balance=500)


@pytest.mark.parametrize("table", ["transactions", "audit_logs"])
def test_append_only_tables_cannot_be_truncated(session, table):
    with pytest.raises(DBAPIError):
        session.execute(text(f"TRUNCATE {table} CASCADE"))


def test_audit_log_cannot_be_changed(session):
    for sql in ("UPDATE audit_logs SET action = 'x'", "DELETE FROM audit_logs"):
        session.execute(text("INSERT INTO audit_logs (action) VALUES ('login')"))
        with pytest.raises(DBAPIError):
            session.execute(text(sql))
        session.rollback()


def test_blank_audit_action_rejected(session):
    with pytest.raises(IntegrityError):
        session.execute(text("INSERT INTO audit_logs (action) VALUES (' ')"))


def test_negative_failed_login_count_rejected(session):
    with pytest.raises(IntegrityError):
        session.add(
            User(email="neg@example.com", password_hash="x", failed_login_count=-1)
        )
        session.flush()


def test_membership_cannot_change_user(session):
    sacco = make_sacco(session)
    m = make_membership(session, make_user(session), sacco)
    other = make_user(session)
    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE sacco_memberships SET user_id = :u WHERE id = :id"),
            {"u": other.id, "id": m.id},
        )

def test_reference_cannot_be_reused_within_a_sacco(session):
    tx = make_ledger_row(session)
    acc = session.get(SavingsAccount, tx.savings_account_id)
    with pytest.raises(IntegrityError):
        post(session, acc, TransactionType.DEPOSIT, 5, reference=tx.reference)


def test_reversal_must_mirror_the_original_and_works_once(session):
    tx = make_ledger_row(session, amount=100)
    acc = session.get(SavingsAccount, tx.savings_account_id)
    with pytest.raises(IntegrityError):  # same direction
        post(session, acc, TransactionType.DEPOSIT, 100, reverses_id=tx.id)
    session.rollback()
    tx = make_ledger_row(session, amount=100)
    acc = session.get(SavingsAccount, tx.savings_account_id)
    post(session, acc, TransactionType.WITHDRAWAL, 100, reverses_id=tx.id)
    session.refresh(acc)
    assert acc.balance == 0
    with pytest.raises(IntegrityError):  # already reversed
        post(session, acc, TransactionType.WITHDRAWAL, 100, reverses_id=tx.id)


def test_reversal_cannot_cross_accounts(session):
    tx = make_ledger_row(session, amount=100)
    other_id = make_ledger_row(session, amount=100).savings_account_id
    other = session.get(SavingsAccount, other_id)
    with pytest.raises(IntegrityError):
        post(session, other, TransactionType.WITHDRAWAL, 100, reverses_id=tx.id)


def test_inactive_membership_cannot_post(session):
    tx = make_ledger_row(session)
    acc = session.get(SavingsAccount, tx.savings_account_id)
    session.execute(
        text("UPDATE sacco_memberships SET status = 'inactive' WHERE id = :m"),
        {"m": acc.membership_id},
    )
    with pytest.raises(IntegrityError):
        post(session, acc, TransactionType.DEPOSIT, 5)


def test_actor_must_belong_to_the_same_sacco(session):
    tx = make_ledger_row(session)
    acc = session.get(SavingsAccount, tx.savings_account_id)
    outsider = make_membership(session, make_user(session), make_sacco(session))
    with pytest.raises(IntegrityError):
        post(
            session,
            acc,
            TransactionType.DEPOSIT,
            5,
            created_by_membership_id=outsider.id,
        )


def test_sacco_currency_must_be_a_three_letter_code(session):
    session.add(Sacco(name="Bad Currency Sacco", currency="ks1"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_new_users_are_not_platform_owners(session):
    assert make_user(session).is_platform_owner is False

# Migration test: KEEP LAST, it drops and rebuilds the schema

def test_downgrade_removes_all_tables(schema_app):
    downgrade(directory=MIGRATIONS, revision="base")
    tables = set(inspect(db.engine).get_table_names()) - {"alembic_version"}
    assert tables == set()
    upgrade(directory=MIGRATIONS, revision="head")


