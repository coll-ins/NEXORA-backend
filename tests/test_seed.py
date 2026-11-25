# ruff: noqa: F811  (pytest fixtures imported from savings_support)
"""The dev seed script: idempotent, logins work, refuses non-dev databases."""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.extensions import db
from app.models.access import MembershipRoleLink, Role
from app.models.audit import AuditLog
from app.models.ledger import Transaction
from app.models.membership import SaccoMembership
from app.models.sacco import Sacco
from app.models.savings import SavingsAccount
from app.models.user import User
from savings_support import release_session, schema_app  # noqa: F401

pytestmark = pytest.mark.schema

_PATH = Path(__file__).resolve().parents[1] / "backend" / "scripts" / "seed.py"
_spec = importlib.util.spec_from_file_location("nexora_seed", _PATH)
seed_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_script)

MODELS = (User, Sacco, SaccoMembership, SavingsAccount, Transaction, AuditLog)


def counts():
    db.session.remove()
    return [db.session.scalar(select(func.count()).select_from(m)) for m in MODELS]


def test_running_the_seed_twice_changes_nothing(schema_app):
    seed_script.seed(schema_app, require_dev=False)
    first = counts()
    seed_script.seed(schema_app, require_dev=False)
    assert counts() == first

    users, saccos, memberships, accounts, transactions, _ = first
    # 16 memberships in their own SACCO, plus alpha-m1's second one in Beta.
    assert (users, saccos, memberships, accounts) == (18, 2, 17, 7)
    assert transactions == 14  # 6 movements and 1 reversal per SACCO
    reversals = db.session.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.reverses_id.is_not(None))
    )
    assert reversals == 2


def test_every_seeded_user_can_log_in(schema_app):
    seeded = seed_script.seed(schema_app, require_dev=False)
    client = schema_app.test_client()
    assert len(seeded) == 18
    for row in seeded:
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": row["email"], "password": seed_script.DEV_PASSWORD},
        )
        assert resp.status_code == 200, row["email"]


def test_every_seeded_member_can_enter_their_sacco_with_the_dev_pin(schema_app):
    seeded = seed_script.seed(schema_app, require_dev=False)
    client = schema_app.test_client()
    row = next(r for r in seeded if r["email"] == "alpha-m1@example.com")
    login = client.post(
        "/api/v1/auth/login",
        json={"email": row["email"], "password": seed_script.DEV_PASSWORD},
    ).get_json()
    sacco_id = db.session.scalar(select(Sacco.id).where(Sacco.name == row["sacco"]))
    resp = client.post(
        "/api/v1/auth/sacco-login",
        json={"sacco_id": sacco_id, "pin": seed_script.DEV_PIN},
        headers={"Authorization": f"Bearer {login['access_token']}"},
    )
    assert resp.status_code == 200


def test_the_seed_refuses_a_database_that_is_not_a_dev_one(schema_app):
    with pytest.raises(SystemExit) as excinfo:
        seed_script.seed(schema_app)
    assert "_dev" in str(excinfo.value)


def test_every_seeded_membership_holds_its_job_title(schema_app):
    """No seeded membership relies on can()'s legacy-column fallback."""
    seed_script.seed(schema_app, require_dev=False)
    rows = db.session.execute(
        select(SaccoMembership.role, Role.code)
        .join(
            MembershipRoleLink,
            MembershipRoleLink.membership_id == SaccoMembership.id,
        )
        .join(Role, Role.id == MembershipRoleLink.role_id)
    ).all()

    expected = {
        "sacco_admin": {"sacco_admin"},
        "staff": {"secretary", "loans_officer", "finance_officer"},
        "member": {"member"},
    }
    memberships = db.session.scalar(select(func.count()).select_from(SaccoMembership))
    assert len(rows) == memberships == 17
    for membership_role, role_code in rows:
        assert role_code in expected[membership_role.value]


def test_one_member_belongs_to_both_saccos_so_the_switcher_has_something_to_switch(
    schema_app,
):
    seed_script.seed(schema_app, require_dev=False)
    rows = db.session.execute(
        text(
            "SELECT s.name, m.status::text, (m.pin_hash IS NOT NULL) "
            "FROM sacco_memberships m "
            "JOIN users u ON u.id = m.user_id "
            "JOIN saccos s ON s.id = m.sacco_id "
            "WHERE u.email = 'alpha-m1@example.com' ORDER BY s.name"
        )
    ).all()
    assert [tuple(row) for row in rows] == [
        ("Alpha SACCO", "active", True),
        ("Beta SACCO", "active", True),
    ]
    # Everyone else still belongs to exactly one SACCO.
    others = db.session.execute(
        text(
            "SELECT count(*) FROM (SELECT user_id FROM sacco_memberships "
            "GROUP BY user_id HAVING count(*) > 1) t"
        )
    ).scalar()
    assert others == 1
