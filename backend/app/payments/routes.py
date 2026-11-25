"""STK push (behind DARAJA_ENABLED, off by default; DARAJA_ENV picks the API).

POST /saccos/<id>/stk-push        a member asks M-Pesa to prompt their phone
GET  /saccos/<id>/stk-push/<pid>  that member, or `member.manage`: the answer
POST /daraja/callback/<token>     Safaricom's answer (the token is the secret)

A paid answer becomes a pending deposit (`savings.deposits.record_stk_payment`):
an officer confirms it, unless the SACCO set an auto-confirm limit and the
payment is below it and not flagged. Nothing else here moves money.
"""

import hmac
import re
from datetime import UTC, datetime, timedelta
from functools import wraps

from flask import current_app, g, jsonify, request
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app import daraja_bp, saccos_bp
from app.audit import log as audit_log
from app.auth.limits import money_limit, read_limit, write_limit
from app.auth.routes import _body
from app.errors import error_response
from app.extensions import db
from app.models.daraja_settings import SaccoDarajaSettings
from app.models.membership import SaccoMembership
from app.models.stk_push import StkPush
from app.payments import credentials, daraja
from app.saccos import payment_details as pay_in
from app.savings import deposits
from app.security.permissions import can
from app.security.tenant import permission_scoped

STALE_MSG = "M-Pesa did not answer in time."
STALE = timedelta(minutes=15)  # a push with no answer after this has failed
MAX_PENDING = 3  # prompts a member may have waiting at once
MAX_MINOR = 15_000_000  # KES 150,000, under M-Pesa's per-transaction limit
ACK = {"ResultCode": 0, "ResultDesc": "Accepted"}


def _feature(view):
    """Off means absent: the same 404 as a route that does not exist."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not daraja.enabled():
            return error_response(404, "Not found.")
        return view(*args, **kwargs)

    return wrapper


def _text(value, limit: int) -> str:
    """Safe for the database: no NUL or lone surrogate, cut to `limit`."""
    return str(value).encode("utf-8", "ignore").decode().replace("\x00", "")[:limit]


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _json(row: StkPush) -> dict:
    return {
        "id": row.id,
        "status": row.status,
        "amount": row.amount,
        "phone_last3": row.phone[-3:],
        "message": row.result_desc,
        "created_at": row.created_at.isoformat(),
    }


@saccos_bp.post("/<int:sacco_id>/stk-push")
@_feature
@money_limit
@permission_scoped("savings.request")
def start_stk_push(sacco_id):
    me = g.access.membership
    body = _body()
    amount, phone = body.get("amount"), daraja.msisdn(body.get("phone"))
    if (
        me is None
        or type(amount) is not int
        or amount % 100
        or not 0 < amount <= MAX_MINOR
    ):
        return error_response(
            400, "Send amount in minor units: whole shillings, KES 1 to KES 150,000."
        )
    if phone is None:
        return error_response(400, "Send a Safaricom number like 0712345678.")
    pay_in.apply_due(sacco_id)  # commits, so it comes before the lock
    details = pay_in.live_details(sacco_id)
    if details is not None and details.mpesa_type == "pochi":
        return error_response(
            409,
            "This SACCO takes Pochi la Biashara, which cannot be prompted. "
            "Send the money to the number shown, then ask for the deposit.",
        )
    try:
        creds = credentials.for_sacco(sacco_id)
    except credentials.CredentialsError as exc:
        return error_response(409, str(exc))
    if creds is None:
        return error_response(
            409, "This SACCO has not set up M-Pesa prompts yet. Pay as shown."
        )
    if creds.own and (
        details is None
        or (details.mpesa_type == "paybill" and creds.shortcode != details.mpesa_number)
    ):
        # An admin alone must not aim prompts at a number the officers did not approve.
        return error_response(
            409,
            "These M-Pesa credentials are not for the paybill the officers "
            "approved. Ask the officers to approve matching pay-in details.",
        )
    till = details.mpesa_number if details and details.mpesa_type == "till" else None
    # One request at a time per member, so the count below cannot be raced past.
    db.session.execute(
        select(SaccoMembership.id).where(SaccoMembership.id == me.id).with_for_update()
    )
    recent = db.session.scalar(
        select(func.count()).where(
            StkPush.membership_id == me.id,
            StkPush.status == "pending",
            StkPush.created_at > datetime.now(UTC) - STALE,
        )
    )
    if recent >= MAX_PENDING:
        return error_response(
            429, "You have payment prompts waiting. Answer or wait for them first."
        )
    row = StkPush(
        sacco_id=sacco_id,
        membership_id=me.id,
        phone=phone,
        amount=amount,
        result_desc="Check your phone and enter your M-Pesa PIN.",
    )
    db.session.add(row)
    # Saved before the network call: no transaction or lock is held open while
    # Safaricom answers, and a failed call is still on record.
    db.session.commit()
    try:
        row.checkout_request_id = daraja.stk_push(
            phone, amount // 100, f"NX{row.id}", creds, till
        )["CheckoutRequestID"]
    except Exception as exc:  # whatever went wrong, the row must not stay pending
        said = (
            str(exc)
            if isinstance(exc, daraja.DarajaError)
            else "M-Pesa could not start the payment."
        )
        row.status, row.result_desc = "failed", said
        db.session.commit()
        return error_response(502, said)
    db.session.commit()
    return jsonify(_json(row)), 201


@saccos_bp.get("/<int:sacco_id>/stk-push/<int:push_id>")
@_feature
@read_limit
@permission_scoped("sacco.view")
def get_stk_push(sacco_id, push_id):
    row = db.session.execute(
        select(StkPush).where(StkPush.id == push_id, StkPush.sacco_id == sacco_id)
    ).scalar_one_or_none()
    mine = (
        g.access.membership is not None
        and row
        and row.membership_id == g.access.membership.id
    )
    if row is None or not (mine or can(g.access.user, g.access.sacco, "member.manage")):
        return error_response(404, "Not found.")
    if row.status == "pending" and row.created_at < datetime.now(UTC) - STALE:
        # No answer from M-Pesa in time (a lost callback): stop showing "pending".
        # Conditional: a callback that lands meanwhile has already closed the row.
        db.session.execute(
            update(StkPush)
            .where(StkPush.id == row.id, StkPush.status == "pending")
            .values(status="failed", result_desc=STALE_MSG)
        )
        db.session.commit()
        db.session.refresh(row)
    return jsonify(_json(row))


@daraja_bp.post("/callback/<token>")
@_feature
def stk_callback(token):
    expected = current_app.config["DARAJA_CALLBACK_TOKEN"]
    if not hmac.compare_digest(token.encode(), expected.encode()):
        return error_response(404, "Not found.")
    data = _dict(
        _dict(_dict(request.get_json(silent=True)).get("Body")).get("stkCallback")
    )
    row = db.session.execute(
        select(StkPush)
        .where(StkPush.checkout_request_id == _text(data.get("CheckoutRequestID"), 64))
        .with_for_update()
    ).scalar_one_or_none()
    # A push the 15-minute rule closed may still be answered; any other answered
    # or unknown push is acknowledged and left alone.
    if row is None or (row.status != "pending" and row.result_desc != STALE_MSG):
        db.session.rollback()
        return jsonify(ACK)
    meta = _dict(data.get("CallbackMetadata")).get("Item")
    items = {
        i["Name"]: i.get("Value")
        for i in (meta if isinstance(meta, list) else [])
        if isinstance(i, dict) and isinstance(i.get("Name"), str)
    }
    code = data.get("ResultCode")
    paid = type(code) is int and code == 0
    sent = items.get("Amount")
    if paid and (
        isinstance(sent, bool)
        or not isinstance(sent, int | float)
        or sent != row.amount // 100
    ):
        paid, data["ResultDesc"] = False, "Paid amount did not match the request."
    receipt = items.get("MpesaReceiptNumber")
    receipt = _text(receipt, 20).strip().upper() if isinstance(receipt, str) else ""
    if paid and not receipt:  # no receipt number: nothing for an officer to match
        paid, data["ResultDesc"] = False, "No receipt number."
    row.status = "succeeded" if paid else "failed"
    row.result_desc = _text(data.get("ResultDesc") or "", 255)
    row.mpesa_receipt = receipt if paid else None
    row.completed_at = datetime.now(UTC)
    audit_log.record(
        row.sacco_id, None, "payment.stk_result", f"stk:{row.id} {row.status}"
    )
    row_id, sacco_id = row.id, row.sacco_id
    try:
        db.session.commit()
    except IntegrityError:  # this receipt number is already on another push
        db.session.rollback()
        db.session.execute(
            update(StkPush)
            .where(StkPush.id == row_id)
            .values(status="failed", result_desc="Duplicate receipt number.")
        )
        audit_log.record(sacco_id, None, "payment.stk_result", f"stk:{row_id} failed")
        db.session.commit()
        return jsonify(ACK)
    if paid:
        # The answer is already saved. A failure here must not make Safaricom
        # retry; the reconciliation report lists a paid push with no deposit.
        try:
            deposits.record_stk_payment(db.session.get(StkPush, row_id))
        except Exception:
            db.session.rollback()
            current_app.logger.exception("No pending deposit for stk %s", row_id)
    return jsonify(ACK)


def _secret(value, limit: int) -> str | None:
    clean = value.strip() if isinstance(value, str) else ""
    # Bytes, not characters: the encrypted column is sized for `limit` bytes.
    ok = clean.isprintable() and " " not in clean and 0 < len(clean.encode()) <= limit
    return clean if ok else None


@saccos_bp.get("/<int:sacco_id>/daraja-settings")
@_feature
@read_limit
@permission_scoped("sacco.update")
def get_daraja_settings(sacco_id):
    row = db.session.execute(
        select(SaccoDarajaSettings).where(SaccoDarajaSettings.sacco_id == sacco_id)
    ).scalar_one_or_none()
    if row is None:
        return jsonify(configured=False)
    return jsonify(
        configured=True, shortcode=row.shortcode, updated_at=row.updated_at.isoformat()
    )


@saccos_bp.put("/<int:sacco_id>/daraja-settings")
@_feature
@write_limit
@permission_scoped("sacco.update")
def put_daraja_settings(sacco_id):
    """The SACCO's own Daraja shortcode and credentials. Never echoed back."""
    body = _body()
    code = body.get("shortcode")
    fields = {
        "consumer_key": _secret(body.get("consumer_key"), 200),
        "consumer_secret": _secret(body.get("consumer_secret"), 200),
        "passkey": _secret(body.get("passkey"), 300),
    }
    bad = [k for k, v in fields.items() if v is None]
    if not (isinstance(code, str) and re.fullmatch(r"[0-9]{5,7}", code)):
        bad.insert(0, "shortcode")
    if bad:
        return error_response(
            400, f"Send {', '.join(bad)}: the shortcode is 5 to 7 digits."
        )
    try:
        sealed = {f"{k}_enc": credentials.encrypt(v) for k, v in fields.items()}
    except credentials.CredentialsError as exc:
        return error_response(503, str(exc))
    values = {
        "shortcode": code,
        "updated_by": g.access.user.id,
        "updated_at": datetime.now(UTC),
        **sealed,
    }
    # One statement, so two first saves cannot collide on the unique SACCO.
    db.session.execute(
        pg_insert(SaccoDarajaSettings)
        .values(sacco_id=sacco_id, **values)
        .on_conflict_do_update(index_elements=["sacco_id"], set_=values)
    )
    audit_log.record(
        sacco_id,
        g.access.user.id,
        "daraja.credentials_set",
        "fields:shortcode,consumer_key,consumer_secret,passkey",
    )
    actor = g.access.user.email
    db.session.commit()
    pay_in.notify_officers(
        sacco_id,
        "NEXORA: the SACCO's M-Pesa prompt credentials were changed",
        f"{actor} saved new M-Pesa (Daraja) credentials for {g.access.sacco.name}.\n\n"
        "Members' prompts now use them. If you did not expect this, tell your "
        "SACCO admin.",
    )
    return jsonify(configured=True, shortcode=code)
