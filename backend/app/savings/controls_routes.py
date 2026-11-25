"""Deposit controls, the flagged-payments report and the M-Pesa reconciliation.

GET/PUT /saccos/<id>/deposit-controls   thresholds (admin: `sacco.update`)
GET     /saccos/<id>/flagged-payments   flagged payments and who approved them
GET     /saccos/<id>/reconciliation     the ledger against the STK records
All read-only except the PUT, which is audited and tells the officers.
"""

from datetime import UTC, datetime, timedelta

from flask import g, jsonify
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import saccos_bp
from app.audit import log as audit_log
from app.auth.limits import read_limit, write_limit
from app.auth.routes import _body
from app.errors import error_response
from app.extensions import db
from app.models.deposit_controls import (
    DEFAULTS,
    MAX_AUTO_CONFIRM,
    SaccoDepositControls,
)
from app.models.ledger import Transaction
from app.models.membership import SaccoMembership
from app.models.payment_request import PaymentRequest
from app.models.stk_push import StkPush
from app.models.user import User
from app.saccos import payment_details as pay_in
from app.savings import flags as deposit_flags
from app.security.tenant import permission_scoped

LIMITS = {  # setting -> (lowest, highest)
    "auto_confirm_limit": (0, MAX_AUTO_CONFIRM),
    "large_amount_limit": (100, 20_000_000),  # KES 200,000: no setting hides big money
    "manual_flag_limit": (100, 5_000_000),
    "repeat_count": (2, 20),
    "repeat_window_hours": (1, 720),
    "quick_withdrawal_hours": (1, 720),
}
STALE_AFTER = timedelta(hours=24)  # a paid push still unconfirmed after this is listed


def _controls_json(sacco_id: int) -> dict:
    return {**deposit_flags.controls(sacco_id), "defaults": DEFAULTS}


@saccos_bp.get("/<int:sacco_id>/deposit-controls")
@read_limit
@permission_scoped("sacco.update", "report.view")
def get_deposit_controls(sacco_id):
    return jsonify(_controls_json(sacco_id))


@saccos_bp.put("/<int:sacco_id>/deposit-controls")
@write_limit
@permission_scoped("sacco.update")
def put_deposit_controls(sacco_id):
    """Send any of the settings; the rest keep their value."""
    body = _body()
    unknown = [k for k in body if k not in LIMITS]
    values = deposit_flags.controls(sacco_id)
    for key, (low, high) in LIMITS.items():
        if key in body:
            given = body[key]
            if type(given) is not int or not low <= given <= high:
                return error_response(
                    400, f"{key} must be a whole number from {low} to {high}."
                )
            values[key] = given
    if unknown or not body:
        return error_response(400, f"Send one or more of: {', '.join(LIMITS)}.")
    row = {**values, "updated_by": g.access.user.id, "updated_at": datetime.now(UTC)}
    db.session.execute(
        pg_insert(SaccoDepositControls)
        .values(sacco_id=sacco_id, **row)
        .on_conflict_do_update(index_elements=["sacco_id"], set_=row)
    )
    audit_log.record(
        sacco_id,
        g.access.user.id,
        "deposit_controls.set",
        ",".join(f"{k}={body[k]}" for k in LIMITS if k in body),
    )
    actor, name = g.access.user.email, g.access.sacco.name
    db.session.commit()
    pay_in.notify_officers(
        sacco_id,
        "NEXORA: the SACCO's deposit controls were changed",
        f"{actor} changed the deposit controls for {name}: "
        f"{', '.join(k for k in LIMITS if k in body)}.\n\n"
        "If you did not expect this, tell your SACCO admin.",
    )
    return jsonify(_controls_json(sacco_id))


def _emails(ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = db.session.execute(
        select(SaccoMembership.id, User.email)
        .join(User, User.id == SaccoMembership.user_id)
        .where(SaccoMembership.id.in_(ids))
    ).all()
    return dict(rows)


@saccos_bp.get("/<int:sacco_id>/flagged-payments")
@read_limit
@permission_scoped("report.view")
def flagged_payments(sacco_id):
    rows = (
        db.session.execute(
            select(PaymentRequest)
            .where(
                PaymentRequest.sacco_id == sacco_id, PaymentRequest.flags.is_not(None)
            )
            .order_by(PaymentRequest.id.desc())
            .limit(200)
        )
        .scalars()
        .all()
    )
    who = _emails(
        [r.membership_id for r in rows]
        + [r.first_approved_by for r in rows]
        + [r.decided_by for r in rows]
    )
    return jsonify(
        items=[
            {
                "id": r.id,
                "member": who.get(r.membership_id),
                "kind": r.kind,
                "source": r.source,
                "amount_minor": r.amount_minor,
                "mpesa_receipt": r.mpesa_receipt,
                "flags": deposit_flags.split(r.flags),
                "status": r.status,
                "first_approved_by": who.get(r.first_approved_by),
                "second_approved_by": who.get(r.decided_by)
                if r.status == "confirmed"
                else None,
                "decided_by": who.get(r.decided_by),
                "ledger_transaction_id": r.ledger_transaction_id,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        labels=deposit_flags.LABELS,
    )


def reconcile(sacco_id: int) -> dict:
    """The ledger against the STK records. Mismatches are things to look into,
    never things this code fixes: the ledger is not edited."""
    now = datetime.now(UTC)
    pushes = (
        db.session.execute(
            select(StkPush).where(
                StkPush.sacco_id == sacco_id, StkPush.status == "succeeded"
            )
        )
        .scalars()
        .all()
    )
    requests = {
        r.mpesa_receipt: r
        for r in db.session.execute(
            select(PaymentRequest).where(
                PaymentRequest.sacco_id == sacco_id,
                PaymentRequest.mpesa_receipt.is_not(None),
            )
        ).scalars()
    }
    entries = {
        t.id: t
        for t in db.session.execute(
            select(Transaction).where(
                Transaction.sacco_id == sacco_id,
                Transaction.id.in_(
                    [
                        r.ledger_transaction_id
                        for r in requests.values()
                        if r.ledger_transaction_id
                    ]
                ),
            )
        ).scalars()
    }
    reversed_ids = set(
        db.session.execute(
            select(Transaction.reverses_id).where(
                Transaction.sacco_id == sacco_id, Transaction.reverses_id.is_not(None)
            )
        ).scalars()
    )

    mismatches = []

    def flag(kind, push=None, req=None, detail=""):
        mismatches.append(
            {
                "kind": kind,
                "stk_push_id": push.id if push else (req.stk_push_id if req else None),
                "mpesa_receipt": (push.mpesa_receipt if push else req.mpesa_receipt),
                "stk_amount": push.amount if push else None,
                "request_id": req.id if req else None,
                "request_amount": req.amount_minor if req else None,
                "ledger_transaction_id": req.ledger_transaction_id if req else None,
                "detail": detail,
            }
        )

    for push in pushes:
        req = requests.get(push.mpesa_receipt)
        if req is None:
            flag("paid_not_recorded", push, None, "M-Pesa paid, no deposit request.")
            continue
        if req.membership_id != push.membership_id:
            flag("member_differs", push, req, "Recorded for a different member.")
        if req.amount_minor != push.amount:
            flag(
                "amount_differs",
                push,
                req,
                "The request's amount is not what M-Pesa took.",
            )
        if req.status == "rejected":
            flag("paid_but_rejected", push, req, "Money received, deposit rejected.")
        elif (
            req.status == "pending"
            and push.completed_at
            and (now - push.completed_at > STALE_AFTER)
        ):
            flag(
                "paid_not_credited",
                push,
                req,
                "Paid over a day ago, still unconfirmed.",
            )
        elif req.status == "confirmed":
            tx = entries.get(req.ledger_transaction_id)
            if tx is None:
                flag("ledger_missing", push, req, "Confirmed with no ledger entry.")
            elif tx.amount != push.amount:
                flag(
                    "ledger_amount_differs",
                    push,
                    req,
                    "Ledger amount is not what M-Pesa took.",
                )
            elif tx.id in reversed_ids:
                flag("credit_reversed", push, req, "The deposit was reversed.")

    seen = {p.mpesa_receipt for p in pushes}
    unverified = []
    for receipt, req in requests.items():
        if receipt in seen:
            continue
        if (
            req.source == "stk"
        ):  # a ledger entry that says M-Pesa answered, but no answer
            flag(
                "no_paid_push",
                None,
                req,
                "Recorded as an M-Pesa payment, no paid STK record.",
            )
        elif req.source == "manual":
            unverified.append(
                {
                    "request_id": req.id,
                    "mpesa_receipt": receipt,
                    "amount_minor": req.amount_minor,
                    "status": req.status,
                }
            )
    return {
        "checked": len(pushes),
        "ok": not mismatches,
        "mismatches": mismatches,
        "unverified_manual": unverified,
    }


@saccos_bp.get("/<int:sacco_id>/reconciliation")
@read_limit
@permission_scoped("report.view")
def reconciliation(sacco_id):
    result = reconcile(sacco_id)
    audit_log.record(
        sacco_id,
        g.access.user.id,
        "ledger.reconcile_mpesa",
        f"checked:{result['checked']}/mismatches:{len(result['mismatches'])}",
    )
    db.session.commit()
    return jsonify(result)
