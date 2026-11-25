"""Optional registration details on a SACCO: create, edit, validate, audit."""

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models.sacco import Sacco
from test_sacco_detail import (
    ADMIN,
    BASE,
    MEMBER,
    audit_rows,
    auth,
    make_sacco,
    make_user,
    person,
)

GOOD = {
    "registration_number": "CS/12345",
    "sasra_licence_number": "SASRA/DT/0042/2024",
    "registration_date": "2019-03-28",
}


@pytest.fixture(autouse=True)
def clean_tables(app):
    def truncate():
        db.session.remove()
        db.session.execute(
            text("TRUNCATE refresh_tokens, users, saccos RESTART IDENTITY CASCADE")
        )
        db.session.commit()

    truncate()
    yield
    truncate()


def row(sacco_id):
    db.session.expire_all()
    return db.session.get(Sacco, sacco_id)


def test_a_sacco_has_no_registration_details_until_it_gives_them(client):
    sacco_id = make_sacco()
    seen = client.get(f"{BASE}/{sacco_id}", headers=person(sacco_id, MEMBER)).json
    assert seen["registration_number"] is None
    assert seen["sasra_licence_number"] is None
    assert seen["registration_date"] is None


def test_the_platform_owner_creates_a_sacco_with_or_without_them(client):
    owner = auth(make_user(owner=True))
    bare = client.post(BASE, json={"name": "Bare SACCO"}, headers=owner)
    assert bare.status_code == 201 and bare.json["registration_number"] is None
    full = client.post(BASE, json={"name": "Full SACCO", **GOOD}, headers=owner)
    assert full.status_code == 201, full.json
    assert {k: full.json[k] for k in GOOD} == GOOD
    stored = row(full.json["id"])
    assert stored.registration_date.isoformat() == "2019-03-28"


def test_a_sacco_admin_records_and_clears_them_and_it_is_audited(client):
    sacco_id = make_sacco()
    admin = person(sacco_id, ADMIN)
    done = client.patch(f"{BASE}/{sacco_id}", json=GOOD, headers=admin)
    assert done.status_code == 200, done.json
    assert {k: done.json[k] for k in GOOD} == GOOD
    (entry,) = audit_rows()
    assert entry.action == "sacco.registration_update"
    assert "registration_number" in entry.target
    # The trail names the fields, never the numbers.
    assert "CS/12345" not in entry.target and "SASRA" not in entry.target

    cleared = client.patch(
        f"{BASE}/{sacco_id}",
        json={"registration_number": None, "sasra_licence_number": ""},
        headers=admin,
    )
    assert cleared.json["registration_number"] is None
    assert cleared.json["sasra_licence_number"] is None
    assert cleared.json["registration_date"] == "2019-03-28"  # not sent, so kept


def test_a_rename_and_registration_in_one_request_write_two_audit_rows(client):
    sacco_id = make_sacco("Before")
    done = client.patch(
        f"{BASE}/{sacco_id}",
        json={"name": "After", "registration_number": "CS/777"},
        headers=person(sacco_id, ADMIN),
    )
    assert done.status_code == 200
    assert [r.action for r in audit_rows()] == [
        "sacco.rename",
        "sacco.registration_update",
    ]


def test_sending_the_same_values_again_changes_and_logs_nothing(client):
    sacco_id = make_sacco()
    admin = person(sacco_id, ADMIN)
    client.patch(f"{BASE}/{sacco_id}", json=GOOD, headers=admin)
    before = len(audit_rows())
    again = client.patch(f"{BASE}/{sacco_id}", json=GOOD, headers=admin)
    assert again.status_code == 200
    assert len(audit_rows()) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("registration_number", "A"),
        ("registration_number", "x" * 41),
        ("registration_number", "CS#12345"),
        ("registration_number", "CS/12\u200b345"),
        ("registration_number", 12345),
        ("sasra_licence_number", "<b>1</b>"),
        ("registration_date", "2019/03/28"),
        ("registration_date", "20190328"),
        ("registration_date", "2019-02-30"),
        ("registration_date", "2999-01-01"),
        ("registration_date", "1800-01-01"),
        ("registration_date", 20190328),
    ],
)
def test_a_bad_value_is_400_with_the_field_named_and_nothing_changes(
    client, field, value
):
    sacco_id = make_sacco()
    resp = client.patch(
        f"{BASE}/{sacco_id}", json={field: value}, headers=person(sacco_id, ADMIN)
    )
    assert resp.status_code == 400
    assert resp.json["error"]["code"] == "validation_error"
    assert field in resp.json["error"]["fields"]
    assert audit_rows() == []
    assert getattr(row(sacco_id), field) is None


def test_a_bad_value_on_create_is_400_and_creates_nothing(client):
    owner = auth(make_user(owner=True))
    resp = client.post(
        BASE, json={"name": "Nope", "registration_number": "#"}, headers=owner
    )
    assert resp.status_code == 400
    assert db.session.query(Sacco).count() == 0


def test_a_registration_number_belongs_to_one_sacco_in_any_case(client):
    first = make_sacco("First")
    client.patch(f"{BASE}/{first}", json=GOOD, headers=person(first, ADMIN))
    second = make_sacco("Second")
    clash = client.patch(
        f"{BASE}/{second}",
        json={"registration_number": "cs/12345"},
        headers=person(second, ADMIN),
    )
    assert clash.status_code == 409
    assert row(second).registration_number is None
    # The same SASRA licence number is refused on create too.
    owner = auth(make_user(owner=True))
    dup = client.post(
        BASE,
        json={"name": "Third", "sasra_licence_number": GOOD["sasra_licence_number"]},
        headers=owner,
    )
    assert dup.status_code == 409
    assert "registration number or SASRA" in dup.json["error"]["message"]
    # A duplicate name is still reported as a duplicate name.
    name_dup = client.post(BASE, json={"name": "first"}, headers=owner)
    assert name_dup.status_code == 409
    assert "name" in name_dup.json["error"]["message"]


def test_only_the_sacco_admin_or_platform_owner_may_record_them(client):
    sacco_id = make_sacco()
    member = person(sacco_id, MEMBER)
    assert (
        client.patch(f"{BASE}/{sacco_id}", json=GOOD, headers=member).status_code == 403
    )
    outsider = auth(make_user())
    assert client.patch(
        f"{BASE}/{sacco_id}", json=GOOD, headers=outsider
    ).status_code in (
        403,
        404,
    )
    owner = auth(make_user(owner=True))
    assert (
        client.patch(f"{BASE}/{sacco_id}", json=GOOD, headers=owner).status_code == 200
    )


def test_status_and_currency_are_still_not_editable_here(client):
    sacco_id = make_sacco()
    for body in ({"status": "suspended"}, {"currency": "USD"}, {**GOOD, "id": 9}):
        resp = client.patch(
            f"{BASE}/{sacco_id}", json=body, headers=person(sacco_id, ADMIN)
        )
        assert resp.status_code == 400
    assert row(sacco_id).currency == "KES"


def test_the_database_refuses_a_bad_value_even_if_the_app_does_not(app):
    sacco_id = make_sacco()
    for column, value in (
        ("registration_number", "#"),
        ("sasra_licence_number", "x"),
        ("registration_date", "1800-01-01"),
    ):
        with pytest.raises(Exception, match="ck_saccos"):
            db.session.execute(
                text(f"UPDATE saccos SET {column} = :v WHERE id = :i"),
                {"v": value, "i": sacco_id},
            )
        db.session.rollback()
