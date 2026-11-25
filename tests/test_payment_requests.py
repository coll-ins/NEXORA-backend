"""Item 6: member deposit and withdrawal requests.

One test per case in the plan (a to n). A request changes no balance; only a
confirmation posts the ledger entry, through the existing savings service.
"""

import uuid

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models.access import MembershipRoleLink, Role
from savings_support import Setup, release_session, schema_app  # noqa: F401
from test_schema_rules import auth

pytestmark = pytest.mark.schema

BASE = "/api/v1/savings"


def create(
    client,
    user,
    s,
    kind="deposit",
    method="mpesa",
    amount=1000,
    reference=None,
    **extra,
):
    body = {
        "sacco_id": s.sacco.id,
        "kind": kind,
        "method": method,
        "amount": amount,
        "reference": reference or uuid.uuid4().hex,
    }
    if method == "mpesa":
        body["phone"] = extra.pop("phone", "0712 345 678")
    else:
        body["bank_name"] = extra.pop("bank_name", "Equity Bank")
        body["account_ref"] = extra.pop("account_ref", "12345678")
    body.update(extra)
    return client.post(f"{BASE}/requests", json=body, headers=auth(user))


def confirm(client, user, request_id, reference=None):
    return client.post(
        f"{BASE}/requests/{request_id}/confirm",
        json={"reference": reference or uuid.uuid4().hex},
        headers=auth(user),
    )


def reject(client, user, request_id, reason="No such payment"):
    return client.post(
        f"{BASE}/requests/{request_id}/reject",
        json={"reason": reason},
        headers=auth(user),
    )


def balance(membership_id):
    db.session.expire_all()
    return db.session.execute(
        text("SELECT balance FROM savings_accounts WHERE membership_id = :m"),
        {"m": membership_id},
    ).scalar_one()


def rows(reference):
    db.session.expire_all()
    return db.session.execute(
        text("SELECT count(*) FROM payment_requests WHERE reference = :r"),
        {"r": reference},
    ).scalar_one()


# ---------------------------------------------------------------- (a) create


def test_a_a_member_creates_mpesa_and_bank_deposits(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    mpesa = create(client, s.member, s)
    bank = create(client, s.member, s, method="bank")
    assert mpesa.status_code == 201, mpesa.json
    assert bank.status_code == 201, bank.json
    assert mpesa.json["phone"] == "254712345678"
    assert mpesa.json["status"] == "pending"
    assert mpesa.json["email"].startswith("member")
    assert bank.json["bank_name"] == "Equity Bank"
    assert bank.json["account_ref"] == "12345678"


# ------------------------------------------------------------- (b) same ref


def test_b_the_same_reference_returns_the_original_and_one_row(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    reference = uuid.uuid4().hex
    first = create(client, s.member, s, reference=reference)
    again = create(client, s.member, s, reference=reference)
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json["id"] == first.json["id"]
    assert rows(reference) == 1


# ------------------------------------------------------------- (c) bad phone


def test_c_a_bad_phone_is_400_with_the_sentence(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    resp = create(client, s.member, s, phone="0123")
    assert resp.status_code == 400
    assert resp.json["error"]["message"] == (
        "Enter a Safaricom number like 0712 345 678."
    )


# --------------------------------------------------------- (d) over balance


def test_d_a_withdrawal_above_the_balance_is_refused(schema_app):  # noqa: F811
    s = Setup(balance=5000)
    client = schema_app.test_client()
    resp = create(client, s.member, s, kind="withdrawal", amount=6000)
    assert resp.status_code == 409
    assert resp.json["error"]["code"] == "INSUFFICIENT_FUNDS"


# ------------------------------------------------------- (e) an officer asks


def test_e_an_officer_cannot_create_a_request(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    assert create(client, s.admin, s).status_code == 403


# ------------------------------------------------------ (f) a member lists


def test_f_a_member_cannot_list_the_sacco_queue(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    resp = client.get(
        f"{BASE}/saccos/{s.sacco.id}/requests", headers=auth(s.member)
    )
    assert resp.status_code == 403


# --------------------------------------------------------- (g) officer queue


def test_g_an_officer_lists_the_pending_requests(schema_app):  # noqa: F811
    s = Setup(balance=5000)
    client = schema_app.test_client()
    first = create(client, s.member, s)
    second = create(client, s.member, s, kind="withdrawal", amount=500)
    assert (first.status_code, second.status_code) == (201, 201)
    first, second = first.json, second.json
    resp = client.get(
        f"{BASE}/saccos/{s.sacco.id}/requests?status=pending", headers=auth(s.admin)
    )
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json["items"]]
    assert ids == [second["id"], first["id"]]  # newest first
    assert resp.json["items"][0]["email"]


# ------------------------------------------------------ (h) confirm a deposit


def test_h_confirming_a_deposit_posts_exactly_its_amount(schema_app):  # noqa: F811
    s = Setup(balance=1000)
    client = schema_app.test_client()
    request_id = create(client, s.member, s, amount=2500).json["id"]
    before = balance(s.member_membership.id)

    resp = confirm(client, s.admin, request_id)
    assert resp.status_code == 200, resp.json
    assert resp.json["status"] == "confirmed"
    assert resp.json["ledger_transaction_id"]
    assert balance(s.member_membership.id) == before + 2500


# ------------------------------------------------ (i) confirming twice


def test_i_confirming_twice_is_409_and_moves_money_once(schema_app):  # noqa: F811
    s = Setup(balance=1000)
    client = schema_app.test_client()
    request_id = create(client, s.member, s, amount=700).json["id"]
    before = balance(s.member_membership.id)

    assert confirm(client, s.admin, request_id).status_code == 200
    after = balance(s.member_membership.id)
    second = confirm(client, s.admin, request_id)
    assert second.status_code == 409
    assert second.json["error"]["code"] == "REQUEST_ALREADY_DECIDED"
    assert balance(s.member_membership.id) == after
    assert after == before + 700


# ------------------------------------------------- (j) deciding your own


def test_j_an_officer_cannot_decide_their_own_request(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    # The same person, now also a SACCO admin: they may post money, so they may
    # reach the confirm route, and it must refuse them.
    for code in ("member", "sacco_admin"):
        role = db.session.query(Role).filter_by(code=code).one()
        db.session.add(
            MembershipRoleLink(
                membership_id=s.member_membership.id, role_id=role.id
            )
        )
    db.session.commit()

    created = create(client, s.member, s)
    assert created.status_code == 201, created.json
    resp = confirm(client, s.member, created.json["id"])
    assert resp.status_code == 403
    assert resp.json["error"]["code"] == "OWN_REQUEST"


# --------------------------------------------------------------- (k) reject


def test_k_reject_needs_a_reason_and_leaves_the_balance_alone(schema_app):  # noqa: F811
    s = Setup(balance=1000)
    client = schema_app.test_client()
    request_id = create(client, s.member, s, amount=400).json["id"]
    before = balance(s.member_membership.id)

    missing = client.post(
        f"{BASE}/requests/{request_id}/reject", json={}, headers=auth(s.admin)
    )
    assert missing.status_code == 400
    assert missing.json["error"]["message"] == (
        "Write a reason before you reject this request."
    )

    decided = reject(client, s.admin, request_id)
    assert decided.status_code == 200
    assert decided.json["status"] == "rejected"
    assert decided.json["reject_reason"] == "No such payment"
    assert balance(s.member_membership.id) == before


# --------------------------------------------------------- (l) another SACCO


def test_l_another_saccos_staff_cannot_see_or_decide(schema_app):  # noqa: F811
    s = Setup()
    other = Setup()
    client = schema_app.test_client()
    request_id = create(client, s.member, s).json["id"]

    listed = client.get(
        f"{BASE}/saccos/{s.sacco.id}/requests", headers=auth(other.admin)
    )
    assert listed.status_code in (403, 404)
    decided = confirm(client, other.admin, request_id)
    assert decided.status_code in (403, 404)


# ------------------------------------------------------------ (m) suspended


def test_m_a_suspended_sacco_answers_423(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    db.session.execute(
        text("UPDATE saccos SET status = 'suspended' WHERE id = :s"),
        {"s": s.sacco.id},
    )
    db.session.commit()
    assert create(client, s.member, s).status_code == 423


# ------------------------------------------------- (n) catalogue agreement


def test_n_savings_request_is_published_for_members_only():
    """The catalogue the migration seeds says who may ask (see test_access_schema)."""
    from app.security.permissions import PERMISSIONS, ROLE_PERMISSIONS

    assert "savings.request" in PERMISSIONS
    assert "savings.request" in ROLE_PERMISSIONS["member"]
    for title in ("sacco_admin", "secretary", "finance_officer", "loans_officer"):
        assert "savings.request" not in ROLE_PERMISSIONS[title]
