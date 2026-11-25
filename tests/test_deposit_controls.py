# ruff: noqa: F811  (pytest fixtures imported from savings_support)
"""Deposit controls: STK pending deposits, manual entries, unique receipts,
maker-checker reversals, flags with a second approval, and reconciliation.

These run on the migration-built schema: the ledger triggers matter here.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError

from app.extensions import db
from app.models.audit import AuditLog
from app.models.deposit_controls import SaccoDepositControls
from app.models.ledger import Transaction
from app.models.payment_request import PaymentRequest
from app.models.reversal_request import ReversalRequest
from app.models.stk_push import StkPush
from savings_support import (  # noqa: F401
    Setup,
    add_officer,
    approve_reversal,
    ask_for_reversal,
    auth_headers,
    manual_body,
    release_session,
    schema_app,
)

TOKEN = "cb-secret-token"
SAVINGS = "/api/v1/savings"


@pytest.fixture
def daraja_on(schema_app):
    keys = ("DARAJA_ENABLED", "DARAJA_CALLBACK_TOKEN")
    old = {k: schema_app.config.get(k) for k in keys}
    schema_app.config.update(DARAJA_ENABLED=True, DARAJA_CALLBACK_TOKEN=TOKEN)
    yield
    schema_app.config.update(old)


def receipt():
    return uuid.uuid4().hex[:10].upper()


def set_controls(s, **values):
    from app.models.deposit_controls import DEFAULTS

    db.session.add(
        SaccoDepositControls(
            sacco_id=s.sacco.id, updated_by=s.admin.id, **{**DEFAULTS, **values}
        )
    )
    db.session.commit()
    db.session.remove()


def pay(client, s, amount=10_000, code=None, answer=True):
    """An STK push of `amount` minor units and, by default, M-Pesa's paid answer."""
    code = code or receipt()
    checkout = f"ws_CO_{uuid.uuid4().hex[:16]}"
    row = StkPush(
        sacco_id=s.sacco.id,
        membership_id=s.member_membership.id,
        phone="254712345678",
        amount=amount,
        checkout_request_id=checkout,
    )
    db.session.add(row)
    db.session.commit()
    push_id = row.id
    db.session.remove()
    if not answer:
        return push_id, code, None
    body = {
        "Body": {
            "stkCallback": {
                "CheckoutRequestID": checkout,
                "ResultCode": 0,
                "ResultDesc": "ok",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": amount // 100},
                        {"Name": "MpesaReceiptNumber", "Value": code},
                    ]
                },
            }
        }
    }
    reply = client.post(f"/api/v1/daraja/callback/{TOKEN}", json=body)
    db.session.remove()
    return push_id, code, reply


def requests_of(s):
    db.session.remove()
    return (
        db.session.query(PaymentRequest)
        .filter_by(sacco_id=s.sacco.id)
        .order_by(PaymentRequest.id)
        .all()
    )


def ledger_of(s):
    db.session.remove()
    return (
        db.session.query(Transaction)
        .filter_by(sacco_id=s.sacco.id)
        .order_by(Transaction.id)
        .all()
    )


def balance_of(s):
    db.session.remove()
    return db.session.execute(
        text("SELECT balance FROM savings_accounts WHERE id = :a"),
        {"a": s.account.id},
    ).scalar_one()


def manual(client, s, user=None, amount=5_000, code=None, **extra):
    return client.post(
        f"{SAVINGS}/accounts/{s.account.id}/deposits",
        json=manual_body(amount, receipt=code, **extra),
        headers=auth_headers(user or s.admin),
    )


def confirm(client, user, request_id, reference=None):
    return client.post(
        f"{SAVINGS}/requests/{request_id}/confirm",
        json={"reference": reference} if reference else {},
        headers=auth_headers(user),
    )


def actions(s):
    db.session.remove()
    return [r.action for r in db.session.query(AuditLog).filter_by(sacco_id=s.sacco.id)]


# ---- 2a: a paid STK answer becomes a pending deposit --------------------------


def test_a_paid_answer_is_a_pending_deposit_and_no_money_moves(schema_app, daraja_on):
    s = Setup()
    client = schema_app.test_client()
    push_id, code, reply = pay(client, s)
    assert reply.status_code == 200
    (row,) = requests_of(s)
    assert (row.source, row.kind, row.status) == ("stk", "deposit", "pending")
    assert (row.mpesa_receipt, row.stk_push_id, row.amount_minor) == (
        code,
        push_id,
        10_000,
    )
    assert ledger_of(s) == [] and balance_of(s) == 0


def test_a_retried_callback_does_not_make_a_second_request(schema_app, daraja_on):
    s = Setup()
    client = schema_app.test_client()
    push_id, code, _ = pay(client, s)
    checkout = db.session.get(StkPush, push_id).checkout_request_id
    again = client.post(
        f"/api/v1/daraja/callback/{TOKEN}",
        json={
            "Body": {
                "stkCallback": {
                    "CheckoutRequestID": checkout,
                    "ResultCode": 0,
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": 100},
                            {"Name": "MpesaReceiptNumber", "Value": code},
                        ]
                    },
                }
            }
        },
    )
    assert again.status_code == 200
    assert len(requests_of(s)) == 1


def test_a_failed_answer_makes_no_deposit(schema_app, daraja_on):
    s = Setup()
    client = schema_app.test_client()
    _, _, _ = pay(client, s, answer=False)
    row = db.session.query(StkPush).filter_by(sacco_id=s.sacco.id).one()
    reply = client.post(
        f"/api/v1/daraja/callback/{TOKEN}",
        json={
            "Body": {
                "stkCallback": {
                    "CheckoutRequestID": row.checkout_request_id,
                    "ResultCode": 1032,
                    "ResultDesc": "Request cancelled by user",
                }
            }
        },
    )
    assert reply.status_code == 200
    assert requests_of(s) == []


def test_an_officer_confirms_the_pending_deposit_under_its_receipt(
    schema_app, daraja_on
):
    s = Setup()
    client = schema_app.test_client()
    _, code, _ = pay(client, s)
    (row,) = requests_of(s)
    done = confirm(client, s.admin, row.id, reference="ignored-by-the-server")
    assert done.status_code == 200 and done.json["status"] == "confirmed"
    (tx,) = ledger_of(s)
    assert (tx.reference, tx.source, tx.amount) == (f"mpesa-{code}", "stk", 10_000)
    assert balance_of(s) == 10_000
    assert confirm(client, s.admin, row.id).status_code == 409  # not twice


def test_auto_confirm_is_off_by_default(schema_app, daraja_on):
    s = Setup()
    pay(schema_app.test_client(), s, amount=10_000)
    assert [r.status for r in requests_of(s)] == ["pending"]
    assert balance_of(s) == 0


def test_auto_confirm_posts_below_the_limit_as_an_stk_entry(schema_app, daraja_on):
    s = Setup()
    set_controls(s, auto_confirm_limit=50_000)
    client = schema_app.test_client()
    _, code, _ = pay(client, s, amount=10_000)
    (row,) = requests_of(s)
    assert row.status == "confirmed" and row.decided_by is None
    (tx,) = ledger_of(s)
    assert (tx.source, tx.reference) == ("stk", f"mpesa-{code}")
    assert tx.created_by_membership_id == s.member_membership.id
    assert balance_of(s) == 10_000
    assert "savings.request.auto_confirm" in actions(s)
    # Above the limit it waits for an officer.
    pay(client, s, amount=60_000)
    assert [r.status for r in requests_of(s)] == ["confirmed", "pending"]


def test_auto_confirm_never_applies_to_a_flagged_payment(schema_app, daraja_on):
    s = Setup()
    set_controls(s, auto_confirm_limit=50_000, large_amount_limit=10_000)
    pay(schema_app.test_client(), s, amount=10_000)
    (row,) = requests_of(s)
    assert row.status == "pending" and row.flags == "large_amount"
    assert balance_of(s) == 0


# ---- 2b: manual deposits ------------------------------------------------------


def test_a_manual_deposit_is_marked_manual_in_the_ledger_and_history(schema_app):
    s = Setup()
    client = schema_app.test_client()
    made = manual(client, s, amount=5_000, code="MANUAL0001")
    assert made.status_code == 201
    assert made.json["source"] == "manual" and made.json["balance"] == 5_000
    (tx,) = ledger_of(s)
    assert (tx.source, tx.reference) == ("manual", "mpesa-MANUAL0001")
    shown = client.get(
        f"{SAVINGS}/accounts/{s.account.id}/transactions", headers=auth_headers(s.admin)
    )
    assert shown.json["transactions"][0]["source"] == "manual"
    (row,) = requests_of(s)
    assert (row.source, row.status) == ("manual", "confirmed")
    assert row.phone == "254712345678"


@pytest.mark.parametrize(
    "drop", ["mpesa_receipt", "phone", "amount"], ids=["receipt", "phone", "amount"]
)
def test_a_manual_deposit_needs_receipt_amount_and_phone(schema_app, drop):
    s = Setup()
    body = manual_body(5_000)
    body.pop(drop)
    refused = schema_app.test_client().post(
        f"{SAVINGS}/accounts/{s.account.id}/deposits",
        json=body,
        headers=auth_headers(s.admin),
    )
    assert refused.status_code == 400
    assert ledger_of(s) == []


def test_a_manual_deposit_with_a_bad_phone_is_400(schema_app):
    s = Setup()
    refused = manual(schema_app.test_client(), s, phone="12345")
    assert refused.status_code == 400 and ledger_of(s) == []


# ---- 2c: one receipt, one payment ---------------------------------------------


def test_a_receipt_cannot_be_entered_twice_but_a_retry_is_a_replay(schema_app):
    s = Setup()
    client = schema_app.test_client()
    first = manual(client, s, code="DUPRECEIPT")
    assert first.status_code == 201
    retry = manual(client, s, code="DUPRECEIPT")
    assert retry.status_code == 200 and retry.json["replayed"] is True
    other_amount = manual(client, s, code="DUPRECEIPT", amount=7_000)
    assert other_amount.status_code == 409
    assert other_amount.json["error"]["code"] == "DUPLICATE_RECEIPT"
    assert len(ledger_of(s)) == 1 and balance_of(s) == 5_000


def test_a_receipt_is_unique_per_sacco_not_across_saccos(schema_app):
    a, b = Setup(), Setup()
    client = schema_app.test_client()
    assert manual(client, a, code="SHAREDCODE").status_code == 201
    assert manual(client, b, code="SHAREDCODE").status_code == 201


def test_the_database_refuses_a_receipt_twice_in_one_sacco(schema_app):
    s = Setup()
    manual(schema_app.test_client(), s, code="DBUNIQUE01")
    db.session.remove()
    dup = PaymentRequest(
        sacco_id=s.sacco.id,
        membership_id=s.member_membership.id,
        kind="deposit",
        method="mpesa",
        amount_minor=100,
        phone="254712345678",
        reference="another-reference",
        mpesa_receipt="DBUNIQUE01",
    )
    db.session.add(dup)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_a_manual_entry_then_the_callback_is_linked_not_doubled(schema_app, daraja_on):
    s = Setup()
    client = schema_app.test_client()
    manual(client, s, code="LINKME0001", amount=10_000)
    push_id, _, reply = pay(client, s, amount=10_000, code="LINKME0001")
    assert reply.status_code == 200
    (row,) = requests_of(s)
    assert row.source == "manual" and row.stk_push_id == push_id
    assert len(ledger_of(s)) == 1 and balance_of(s) == 10_000
    assert "payment.stk_linked" in actions(s)


def test_the_callback_then_a_manual_entry_is_refused(schema_app, daraja_on):
    s = Setup()
    client = schema_app.test_client()
    pay(client, s, code="CALLBACK01")
    refused = manual(client, s, code="CALLBACK01")
    assert refused.status_code == 409
    assert refused.json["error"]["code"] == "DUPLICATE_RECEIPT"
    assert ledger_of(s) == []


# ---- 2d: the ledger is never edited -------------------------------------------


def test_ledger_rows_cannot_be_updated_or_deleted(schema_app):
    s = Setup(balance=100)
    for sql in (
        "UPDATE transactions SET amount = 1 WHERE sacco_id = :s",
        "UPDATE transactions SET source = 'manual' WHERE sacco_id = :s",
        "DELETE FROM transactions WHERE sacco_id = :s",
    ):
        with pytest.raises(DatabaseError, match="append-only"):
            db.session.execute(text(sql), {"s": s.sacco.id})
        db.session.rollback()


def test_no_route_edits_or_deletes_a_ledger_row(schema_app):
    for rule in schema_app.url_map.iter_rules():
        if "/transactions" in rule.rule or "/deposits" in rule.rule:
            assert not rule.methods & {"PUT", "PATCH", "DELETE"}, rule.rule


# ---- 2e: reversals take two officers ------------------------------------------


def posted(s, amount=3_000):
    from app.savings import service

    tx = service.deposit(s.admin.id, s.account.id, amount, f"d-{receipt()}")
    db.session.remove()
    return tx


def test_the_requester_cannot_approve_their_own_reversal(schema_app):
    s = Setup()
    tx = posted(s)
    client = schema_app.test_client()
    asked = ask_for_reversal(client, s.admin, tx.id)
    assert asked.status_code == 201 and asked.json["status"] == "pending"
    own = approve_reversal(client, s.admin, asked.json["id"], "self-approve")
    assert own.status_code == 403
    assert own.json["error"]["code"] == "SECOND_OFFICER_REQUIRED"
    assert balance_of(s) == 3_000  # nothing posted by asking or by the refusal


def test_a_different_officer_approves_and_the_entry_is_appended(schema_app):
    s = Setup()
    tx = posted(s)
    client = schema_app.test_client()
    asked = ask_for_reversal(client, s.admin, tx.id)
    checker = add_officer(s)
    done = approve_reversal(client, checker, asked.json["id"], "rev-ok-1")
    assert done.status_code == 201 and done.json["balance"] == 0
    row = db.session.get(ReversalRequest, asked.json["id"])
    assert row.status == "approved" and row.reversal_transaction_id == done.json["id"]
    assert row.decided_by == checker.membership_id != row.requested_by
    retry = approve_reversal(client, checker, asked.json["id"], "rev-ok-1")
    assert retry.status_code == 200 and retry.json["replayed"] is True
    late = approve_reversal(client, checker, asked.json["id"], "rev-ok-2")
    assert late.status_code == 409
    assert {"savings.reverse_request", "savings.reverse_approve"} <= set(actions(s))
    assert len(ledger_of(s)) == 2  # the deposit and its reversal


def test_one_open_request_per_entry(schema_app):
    s = Setup()
    tx = posted(s)
    client = schema_app.test_client()
    first = ask_for_reversal(client, s.admin, tx.id)
    again = ask_for_reversal(client, s.admin, tx.id)
    assert again.status_code == 200 and again.json["id"] == first.json["id"]
    other = ask_for_reversal(client, add_officer(s), tx.id)
    assert other.status_code == 409


def test_a_rejected_request_posts_nothing_and_can_be_asked_again(schema_app):
    s = Setup()
    tx = posted(s)
    client = schema_app.test_client()
    asked = ask_for_reversal(client, s.admin, tx.id)
    checker = add_officer(s)
    own = client.post(
        f"{SAVINGS}/reversal-requests/{asked.json['id']}/reject",
        json={"reason": "I changed my mind about this"},
        headers=auth_headers(s.admin),
    )
    assert own.status_code == 403
    no = client.post(
        f"{SAVINGS}/reversal-requests/{asked.json['id']}/reject",
        json={"reason": "The deposit was right after all"},
        headers=auth_headers(checker),
    )
    assert no.status_code == 200 and no.json["status"] == "rejected"
    assert balance_of(s) == 3_000
    assert ask_for_reversal(client, s.admin, tx.id).status_code == 201


def test_the_cap_check_runs_at_approval_and_the_override_is_the_approvers(
    schema_app,
):
    from test_reversal_loan_cap import _loan_setup

    s, deposit, finance = _loan_setup()
    client = schema_app.test_client()
    asked = ask_for_reversal(client, finance, deposit.id)
    assert asked.status_code == 201  # the loan cap is not checked when asking
    blocked = approve_reversal(client, s.admin, asked.json["id"], "cap-1")
    assert blocked.status_code == 409
    assert blocked.json["error"]["code"] == "LOAN_OVER_CAP"
    forced = approve_reversal(
        client,
        s.admin,
        asked.json["id"],
        "cap-2",
        override_reason="The deposit went to the wrong member",
    )
    assert forced.status_code == 201
    assert "savings.reverse_override" in actions(s)


def test_the_database_refuses_a_requester_who_approves_themselves(schema_app):
    s = Setup()
    tx = posted(s)
    asked = ask_for_reversal(schema_app.test_client(), s.admin, tx.id)
    db.session.remove()
    with pytest.raises(IntegrityError):
        db.session.execute(
            text(
                "UPDATE reversal_requests SET status = 'approved', "
                "decided_by = requested_by WHERE id = :i"
            ),
            {"i": asked.json["id"]},
        )
    db.session.rollback()


def test_a_member_cannot_ask_for_or_list_reversals(schema_app):
    s = Setup()
    tx = posted(s)
    client = schema_app.test_client()
    assert ask_for_reversal(client, s.member, tx.id).status_code == 403
    listed = client.get(
        f"{SAVINGS}/saccos/{s.sacco.id}/reversal-requests",
        headers=auth_headers(s.member),
    )
    assert listed.status_code == 403
    ask_for_reversal(client, s.admin, tx.id)
    mine = client.get(
        f"{SAVINGS}/saccos/{s.sacco.id}/reversal-requests?status=pending",
        headers=auth_headers(s.admin),
    )
    assert mine.status_code == 200 and len(mine.json["items"]) == 1
    other = Setup()
    foreign = client.get(
        f"{SAVINGS}/saccos/{s.sacco.id}/reversal-requests",
        headers=auth_headers(other.admin),
    )
    assert foreign.status_code == 404


# ---- 2f: flags need a second officer ------------------------------------------


def test_a_manual_entry_over_the_limit_waits_for_a_second_officer(schema_app):
    s = Setup()
    client = schema_app.test_client()
    first = manual(client, s, amount=2_500_000, code="BIGMANUAL1")
    assert first.status_code == 202 and first.json["queued"] is True
    request = first.json["request"]
    assert "manual_over_limit" in request["flags"]
    assert ledger_of(s) == []
    # The officer who typed it in cannot also give the second approval.
    same = confirm(client, s.admin, request["id"])
    assert same.status_code == 403
    assert same.json["error"]["code"] == "SECOND_OFFICER_REQUIRED"
    assert ledger_of(s) == []
    second = confirm(client, add_officer(s), request["id"])
    assert second.status_code == 200 and second.json["status"] == "confirmed"
    (tx,) = ledger_of(s)
    assert (tx.source, tx.amount) == ("manual", 2_500_000)


def test_a_large_deposit_request_needs_two_officers(schema_app):
    s = Setup()
    client = schema_app.test_client()
    made = client.post(
        f"{SAVINGS}/requests",
        json={
            "sacco_id": s.sacco.id,
            "kind": "deposit",
            "method": "mpesa",
            "amount": 12_000_000,
            "phone": "0712345678",
            "reference": f"big-{receipt()}",
        },
        headers=auth_headers(s.member),
    )
    assert made.status_code == 201 and made.json["flags"] == ["large_amount"]
    first = confirm(client, s.admin, made.json["id"], "big-1")
    assert first.status_code == 200 and first.json["status"] == "pending"
    assert first.json["awaiting_second_approval"] is True
    assert ledger_of(s) == []
    done = confirm(client, add_officer(s), made.json["id"], "big-1")
    assert done.json["status"] == "confirmed" and balance_of(s) == 12_000_000


def test_the_same_amount_again_and_again_is_flagged(schema_app):
    s = Setup()
    client = schema_app.test_client()
    results = [manual(client, s, amount=5_000) for _ in range(3)]
    assert [r.status_code for r in results] == [201, 201, 202]
    assert results[2].json["request"]["flags"] == ["repeated_amount"]


def test_a_withdrawal_soon_after_a_deposit_is_flagged(schema_app):
    s = Setup(balance=5_000)
    client = schema_app.test_client()
    asked = client.post(
        f"{SAVINGS}/requests",
        json={
            "sacco_id": s.sacco.id,
            "kind": "withdrawal",
            "method": "bank",
            "amount": 1_000,
            "bank_name": "Equity",
            "account_ref": "0123456",
            "reference": f"wd-{receipt()}",
        },
        headers=auth_headers(s.member),
    )
    assert asked.json["flags"] == ["quick_withdrawal"]
    first = confirm(client, s.admin, asked.json["id"], "wd-1")
    assert first.json["status"] == "pending"
    assert balance_of(s) == 5_000
    done = confirm(client, add_officer(s), asked.json["id"], "wd-1")
    assert done.json["status"] == "confirmed" and balance_of(s) == 4_000


def test_a_flagged_withdrawal_cannot_be_posted_directly_by_one_officer(schema_app):
    s = Setup(balance=5_000)
    refused = schema_app.test_client().post(
        f"{SAVINGS}/accounts/{s.account.id}/withdrawals",
        json={"amount": 1_000, "reference": "direct-wd"},
        headers=auth_headers(s.admin),
    )
    assert refused.status_code == 409
    assert refused.json["error"]["code"] == "NEEDS_SECOND_APPROVAL"
    assert balance_of(s) == 5_000
    assert "savings.withdrawal.flagged" in actions(s)


def test_the_flagged_payments_report_names_both_officers(schema_app):
    s = Setup()
    client = schema_app.test_client()
    made = manual(client, s, amount=2_500_000, code="REPORT0001")
    checker = add_officer(s)
    confirm(client, checker, made.json["request"]["id"])
    secretary = add_officer(s, "secretary")
    report = client.get(
        f"/api/v1/saccos/{s.sacco.id}/flagged-payments",
        headers=auth_headers(secretary),
    )
    assert report.status_code == 200
    (item,) = report.json["items"]
    assert item["flags"] == ["manual_over_limit"] and item["status"] == "confirmed"
    assert item["first_approved_by"].startswith("admin")
    assert item["second_approved_by"].startswith("officer")
    denied = client.get(
        f"/api/v1/saccos/{s.sacco.id}/flagged-payments", headers=auth_headers(s.member)
    )
    assert denied.status_code == 403


# ---- the thresholds are settings ----------------------------------------------


def test_the_controls_have_safe_defaults_and_only_the_admin_changes_them(schema_app):
    s = Setup()
    client = schema_app.test_client()
    url = f"/api/v1/saccos/{s.sacco.id}/deposit-controls"
    seen = client.get(url, headers=auth_headers(s.admin))
    assert seen.json["auto_confirm_limit"] == 0
    assert seen.json["large_amount_limit"] == 10_000_000
    done = client.put(
        url, json={"auto_confirm_limit": 100_000}, headers=auth_headers(s.admin)
    )
    assert done.status_code == 200 and done.json["auto_confirm_limit"] == 100_000
    assert done.json["large_amount_limit"] == 10_000_000  # untouched
    assert "deposit_controls.set" in actions(s)
    member = client.put(
        url, json={"auto_confirm_limit": 100_000}, headers=auth_headers(s.member)
    )
    assert member.status_code == 403
    finance = add_officer(s)
    assert (
        client.put(
            url, json={"auto_confirm_limit": 1}, headers=auth_headers(finance)
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "body",
    [
        {"auto_confirm_limit": 500_001},
        {"auto_confirm_limit": -1},
        {"auto_confirm_limit": "100"},
        {"auto_confirm_limit": True},
        {"repeat_count": 1},
        {"large_amount_limit": 0},
        {"nonsense": 1},
        {},
    ],
)
def test_a_bad_control_setting_is_400_and_changes_nothing(schema_app, body):
    s = Setup()
    client = schema_app.test_client()
    url = f"/api/v1/saccos/{s.sacco.id}/deposit-controls"
    assert client.put(url, json=body, headers=auth_headers(s.admin)).status_code == 400
    seen = client.get(url, headers=auth_headers(s.admin))
    assert seen.json["auto_confirm_limit"] == 0


# ---- 2g: reconciliation -------------------------------------------------------


def reconcile(client, s):
    return client.get(
        f"/api/v1/saccos/{s.sacco.id}/reconciliation", headers=auth_headers(s.admin)
    )


def kinds(reply):
    return sorted(m["kind"] for m in reply.json["mismatches"])


def test_a_clean_sacco_reconciles(schema_app, daraja_on):
    s = Setup()
    client = schema_app.test_client()
    _, _, _ = pay(client, s)
    (row,) = requests_of(s)
    confirm(client, s.admin, row.id)
    out = reconcile(client, s)
    assert out.status_code == 200
    assert out.json["ok"] and out.json["checked"] == 1 and out.json["mismatches"] == []


def test_a_paid_push_with_no_request_is_a_mismatch(schema_app, daraja_on):
    s = Setup()
    client = schema_app.test_client()
    pay(client, s)
    db.session.execute(
        text("DELETE FROM payment_requests WHERE sacco_id = :s"), {"s": s.sacco.id}
    )
    db.session.commit()
    assert kinds(reconcile(client, s)) == ["paid_not_recorded"]


def test_a_manual_amount_that_differs_from_what_mpesa_took_is_listed(
    schema_app, daraja_on
):
    s = Setup()
    client = schema_app.test_client()
    manual(client, s, code="DIFFAMOUNT", amount=4_000)
    pay(client, s, amount=10_000, code="DIFFAMOUNT")
    out = reconcile(client, s)
    assert kinds(out) == ["amount_differs", "ledger_amount_differs"]
    assert not out.json["ok"]
    assert len(ledger_of(s)) == 1  # reported, never edited


def test_a_rejected_payment_that_mpesa_took_is_listed(schema_app, daraja_on):
    s = Setup()
    client = schema_app.test_client()
    pay(client, s)
    (row,) = requests_of(s)
    rejected = client.post(
        f"{SAVINGS}/requests/{row.id}/reject",
        json={"reason": "Not ours"},
        headers=auth_headers(s.admin),
    )
    assert rejected.status_code == 200
    assert kinds(reconcile(client, s)) == ["paid_but_rejected"]


def test_a_reversed_credit_is_listed(schema_app, daraja_on):
    s = Setup()
    client = schema_app.test_client()
    pay(client, s)
    (row,) = requests_of(s)
    confirm(client, s.admin, row.id)
    tx = ledger_of(s)[0]
    asked = ask_for_reversal(client, s.admin, tx.id)
    approve_reversal(client, add_officer(s), asked.json["id"], "rev-recon")
    assert kinds(reconcile(client, s)) == ["credit_reversed"]


def test_a_manual_receipt_with_no_callback_is_unverified_not_a_mismatch(schema_app):
    s = Setup()
    client = schema_app.test_client()
    manual(client, s, code="NOCALLBACK")
    out = reconcile(client, s)
    assert out.json["ok"]
    assert [u["mpesa_receipt"] for u in out.json["unverified_manual"]] == ["NOCALLBACK"]


def test_only_officers_with_report_access_read_the_reconciliation(schema_app):
    s = Setup()
    client = schema_app.test_client()
    url = f"/api/v1/saccos/{s.sacco.id}/reconciliation"
    assert client.get(url, headers=auth_headers(s.member)).status_code == 403
    assert client.get(url).status_code == 401
    other = Setup()
    assert client.get(url, headers=auth_headers(other.admin)).status_code == 404


# ---- found by the red-team review ---------------------------------------------


def test_the_first_approver_rejecting_is_a_403_not_a_500(schema_app):
    s = Setup()
    client = schema_app.test_client()
    made = manual(client, s, amount=2_500_000, code="FIRSTREJ01")
    rid = made.json["request"]["id"]
    mine = client.post(
        f"{SAVINGS}/requests/{rid}/reject",
        json={"reason": "changed my mind"},
        headers=auth_headers(s.admin),
    )
    assert mine.status_code == 403
    assert mine.json["error"]["code"] == "SECOND_OFFICER_REQUIRED"
    other = client.post(
        f"{SAVINGS}/requests/{rid}/reject",
        json={"reason": "not ours"},
        headers=auth_headers(add_officer(s)),
    )
    assert other.status_code == 200


@pytest.mark.parametrize("ref", ["manual-1-GRIEF00001", "stk-5", "MPESA-X", "Stk-9"])
def test_a_member_cannot_take_a_reference_the_system_uses(schema_app, ref):
    s = Setup()
    made = schema_app.test_client().post(
        f"{SAVINGS}/requests",
        json={
            "sacco_id": s.sacco.id,
            "kind": "deposit",
            "method": "mpesa",
            "amount": 100,
            "phone": "0712345678",
            "reference": ref,
        },
        headers=auth_headers(s.member),
    )
    assert made.status_code == 400


def test_a_rejected_receipt_is_refused_not_queued(schema_app):
    s = Setup()
    client = schema_app.test_client()
    first = manual(client, s, amount=2_500_000, code="REJECTED01")
    client.post(
        f"{SAVINGS}/requests/{first.json['request']['id']}/reject",
        json={"reason": "wrong member"},
        headers=auth_headers(add_officer(s)),
    )
    again = manual(client, s, amount=2_500_000, code="REJECTED01")
    assert again.status_code == 409
    assert again.json["error"]["code"] == "DUPLICATE_RECEIPT"


def test_a_member_does_not_see_flags_on_their_own_requests(schema_app):
    s = Setup()
    client = schema_app.test_client()
    client.post(
        f"{SAVINGS}/requests",
        json={
            "sacco_id": s.sacco.id,
            "kind": "deposit",
            "method": "mpesa",
            "amount": 12_000_000,
            "phone": "0712345678",
            "reference": f"mine-{receipt()}",
        },
        headers=auth_headers(s.member),
    )
    mine = client.get(
        f"{SAVINGS}/requests/me?sacco_id={s.sacco.id}", headers=auth_headers(s.member)
    )
    (item,) = mine.json["items"]
    assert not {"flags", "first_approved_by", "awaiting_second_approval"} & set(item)


def test_a_large_interest_credit_is_not_posted_by_one_officer(schema_app):
    s = Setup()
    client = schema_app.test_client()
    url = f"{SAVINGS}/accounts/{s.account.id}/interest"
    big = client.post(
        url,
        json={"amount": 10_000_000, "reference": "int-big"},
        headers=auth_headers(s.admin),
    )
    assert big.status_code == 409
    assert big.json["error"]["code"] == "NEEDS_SECOND_APPROVAL"
    small = client.post(
        url,
        json={"amount": 500, "reference": "int-small"},
        headers=auth_headers(s.admin),
    )
    assert small.status_code == 201 and balance_of(s) == 500


def test_interest_counts_as_a_recent_deposit_for_the_withdrawal_flag(schema_app):
    from app.savings import service

    s = Setup()
    service.post_interest(s.admin.id, s.account.id, 5_000, "int-quick")
    db.session.remove()
    refused = schema_app.test_client().post(
        f"{SAVINGS}/accounts/{s.account.id}/withdrawals",
        json={"amount": 1_000, "reference": "wd-after-interest"},
        headers=auth_headers(s.admin),
    )
    assert refused.status_code == 409


def test_the_controls_cannot_be_set_to_hide_big_money(schema_app):
    s = Setup()
    client = schema_app.test_client()
    url = f"/api/v1/saccos/{s.sacco.id}/deposit-controls"
    for body in (
        {"large_amount_limit": 20_000_001},
        {"manual_flag_limit": 5_000_001},
        {"quick_withdrawal_hours": 0},
    ):
        bad = client.put(url, json=body, headers=auth_headers(s.admin))
        assert bad.status_code == 400, body


def test_splitting_a_large_deposit_into_small_ones_is_flagged(schema_app):
    s = Setup()
    client = schema_app.test_client()
    codes = [
        manual(client, s, amount=1_999_990 - i).status_code for i in range(6)
    ]  # each under the manual limit; together over the large-amount limit
    assert codes == [201, 201, 201, 201, 201, 202]


def test_repeated_direct_withdrawals_are_flagged(schema_app):
    s = Setup(balance=100_000)
    set_controls(s, quick_withdrawal_hours=0)  # isolate the repeat flag
    client = schema_app.test_client()
    codes = [
        client.post(
            f"{SAVINGS}/accounts/{s.account.id}/withdrawals",
            json={"amount": 777, "reference": f"rep-wd-{i}"},
            headers=auth_headers(s.admin),
        ).status_code
        for i in range(3)
    ]
    assert codes == [201, 201, 409]


def test_a_callback_does_not_link_to_another_amount_and_lowercase_receipts_match(
    schema_app, daraja_on
):
    s = Setup()
    client = schema_app.test_client()
    manual(client, s, code="MISMATCH01", amount=4_000)
    pay(client, s, amount=10_000, code="MISMATCH01")
    (row,) = requests_of(s)
    assert row.stk_push_id is None
    assert "payment.stk_link_mismatch" in actions(s)
    # a lowercase receipt from Safaricom still finds the manual entry
    manual(client, s, code="LOWERCASE1", amount=6_000)
    push_id, _, _ = pay(client, s, amount=6_000, code="lowercase1")
    linked = [r for r in requests_of(s) if r.mpesa_receipt == "LOWERCASE1"]
    assert len(linked) == 1 and linked[0].stk_push_id == push_id
