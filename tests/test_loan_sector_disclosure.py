# ruff: noqa: F811  (pytest fixtures imported from savings_support)
"""Optional loan sector and disclosure acknowledgement, recorded at the request."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.auth.tokens import create_access_token
from app.extensions import db
from app.models.loan import Loan
from app.savings.validation import LOAN_SECTORS
from savings_support import Setup, release_session, schema_app  # noqa: F401

pytestmark = pytest.mark.schema


def request_loan(client, s, **extra):
    token = create_access_token(s.member.id, getattr(s.member, "token_version", 0))
    return client.post(
        f"/api/v1/loans/saccos/{s.sacco.id}",
        json={"principal": 1_000, "term_months": 3, **extra},
        headers={"Authorization": f"Bearer {token}"},
    )


def stored_loans(s):
    db.session.remove()
    return db.session.query(Loan).filter_by(sacco_id=s.sacco.id).all()


def test_neither_field_is_required(schema_app):
    s = Setup(balance=10_000)
    done = request_loan(schema_app.test_client(), s)
    assert done.status_code == 201, done.json
    assert done.json["sector"] is None
    assert done.json["disclosure_acknowledged_at"] is None


def test_a_sector_and_an_acknowledgement_are_recorded_on_the_loan(schema_app):
    s = Setup(balance=10_000)
    client = schema_app.test_client()
    done = request_loan(client, s, sector="agriculture", disclosure_acknowledged=True)
    assert done.status_code == 201, done.json
    assert done.json["sector"] == "agriculture"
    stamp = datetime.fromisoformat(done.json["disclosure_acknowledged_at"])
    assert abs(datetime.now(UTC) - stamp) < timedelta(minutes=1)
    (loan,) = stored_loans(s)
    assert loan.sector == "agriculture" and loan.disclosure_acknowledged_at is not None


@pytest.mark.parametrize("sector", LOAN_SECTORS)
def test_every_listed_sector_is_accepted(schema_app, sector):
    s = Setup(balance=10_000)
    assert request_loan(schema_app.test_client(), s, sector=sector).status_code == 201


@pytest.mark.parametrize("empty", [None, ""])
def test_an_empty_sector_means_not_given(schema_app, empty):
    s = Setup(balance=10_000)
    done = request_loan(
        schema_app.test_client(), s, sector=empty, disclosure_acknowledged=False
    )
    assert done.status_code == 201
    assert done.json["sector"] is None
    assert done.json["disclosure_acknowledged_at"] is None


@pytest.mark.parametrize(
    "extra",
    [
        {"sector": "gambling"},
        {"sector": "Agriculture"},
        {"sector": 3},
        {"sector": ["trade"]},
        {"disclosure_acknowledged": "yes"},
        {"disclosure_acknowledged": 1},
    ],
)
def test_a_bad_value_is_400_and_no_loan_is_created(schema_app, extra):
    s = Setup(balance=10_000)
    done = request_loan(schema_app.test_client(), s, **extra)
    assert done.status_code == 400, done.json
    assert stored_loans(s) == []


def test_the_loan_reads_back_with_both_fields(schema_app):
    s = Setup(balance=10_000)
    client = schema_app.test_client()
    made = request_loan(client, s, sector="trade", disclosure_acknowledged=True).json
    token = create_access_token(s.member.id, getattr(s.member, "token_version", 0))
    got = client.get(
        f"/api/v1/loans/{made['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert got.status_code == 200, got.json
    assert got.json["sector"] == "trade"
    assert got.json["disclosure_acknowledged_at"] is not None


def test_the_database_refuses_an_unknown_sector(schema_app):
    s = Setup(balance=10_000)
    request_loan(schema_app.test_client(), s)
    with pytest.raises(Exception, match="ck_loans_sector"):
        db.session.execute(
            text("UPDATE loans SET sector = 'gambling' WHERE sacco_id = :s"),
            {"s": s.sacco.id},
        )
    db.session.rollback()
