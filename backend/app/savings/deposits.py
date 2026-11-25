"""Deposits that carry an M-Pesa receipt: an answered STK prompt, or an officer
typing in a receipt. Both become one `payment_requests` row, and the receipt is
unique per SACCO, so a manual entry and a later callback for the same payment
are linked, never doubled.
"""

import logging
import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.audit import log as audit_log
from app.errors import DUPLICATE_RECEIPT, NEEDS_SECOND_APPROVAL
from app.extensions import db
from app.models.ledger import Transaction
from app.models.payment_request import PaymentRequest
from app.models.savings import SavingsAccount
from app.models.stk_push import StkPush
from app.savings import flags as deposit_flags
from app.savings import request_service, service
from app.savings.request_validation import normalize_phone
from app.savings.service import Conflict, SavingsError, _require_sacco_active
from app.savings.validation import InvalidRequest

log = logging.getLogger(__name__)

RECEIPT_RE = re.compile(r"[A-Z0-9]{8,12}")


def parse_receipt(value) -> str:
    """M-Pesa receipt codes are 10 capital letters and digits, like `QGH7X2K9LM`."""
    receipt = value.strip().upper() if isinstance(value, str) else ""
    if not RECEIPT_RE.fullmatch(receipt):
        raise InvalidRequest(
            "mpesa_receipt is required: the 10-character code from the M-Pesa message."
        )
    return receipt


def _by_receipt(sacco_id: int, receipt: str) -> PaymentRequest | None:
    return db.session.execute(
        select(PaymentRequest).where(
            PaymentRequest.sacco_id == sacco_id,
            PaymentRequest.mpesa_receipt == receipt,
        )
    ).scalar_one_or_none()


def manual_deposit(
    user_id: int, account_id: int, amount: int, receipt: str, phone: str
) -> tuple[dict, bool]:
    """(result, posted). An officer records a deposit from an M-Pesa receipt.

    `result` is the posted entry (see `posted_json`) when `posted`, else the
    pending request.

    Posted at once, as a "manual" ledger entry, unless a flag applies: then it
    waits as a pending request with this officer's first approval, and a second
    officer confirms it through the request queue.
    """
    account = db.session.get(SavingsAccount, account_id)
    if account is None:
        raise service.NotFound("Not found.")
    officer = service._actor(user_id, account.sacco_id)
    service._require_not_own_account(officer, account)
    _require_sacco_active(account.sacco_id)
    phone = normalize_phone(phone)
    sacco_id, member_id = account.sacco_id, account.membership_id

    held = _by_receipt(sacco_id, receipt)
    if held is not None:
        same = (held.source, held.membership_id, held.amount_minor) == (
            "manual",
            member_id,
            amount,
        )
        db.session.rollback()
        if not same:
            raise Conflict(
                "This M-Pesa receipt is already recorded in this SACCO.",
                code=DUPLICATE_RECEIPT,
            )
        if held.status == "rejected":
            raise Conflict(
                "This M-Pesa receipt was rejected by an officer.",
                code=DUPLICATE_RECEIPT,
            )
        if held.status == "confirmed":
            return _posted(held.id, replayed=True), True
        return request_service.item_by_id(held.id), False

    flags = deposit_flags.evaluate(sacco_id, member_id, "deposit", amount, "manual")
    stk = db.session.execute(
        select(StkPush).where(
            StkPush.sacco_id == sacco_id, StkPush.mpesa_receipt == receipt
        )
    ).scalar_one_or_none()
    row = PaymentRequest(
        sacco_id=sacco_id,
        membership_id=member_id,
        kind="deposit",
        method="mpesa",
        amount_minor=amount,
        phone=phone,
        reference=f"manual-{sacco_id}-{receipt}",
        source="manual",
        mpesa_receipt=receipt,
        stk_push_id=stk.id if stk else None,
        flags=deposit_flags.join(flags),
        status="pending",
    )
    db.session.add(row)
    try:
        db.session.flush()
    except IntegrityError:  # the same receipt was entered a moment ago
        db.session.rollback()
        raise Conflict(
            "This M-Pesa receipt is already recorded in this SACCO.",
            code=DUPLICATE_RECEIPT,
        ) from None
    if flags:
        row.first_approved_by = officer.id
        row.first_approved_at = func.now()
        audit_log.record(
            sacco_id,
            user_id,
            "savings.manual_deposit.flagged",
            f"payment_request:{row.id} flags:{row.flags}",
        )
        db.session.commit()
        return request_service.item_by_id(row.id), False

    row.status = "confirmed"
    row.decided_by = officer.id
    row.decided_at = func.now()
    audit_log.record(
        sacco_id, user_id, "savings.manual_deposit", f"payment_request:{row.id}"
    )
    try:
        posted = service.deposit(
            user_id, account_id, amount, f"mpesa-{receipt}", source="manual"
        )
    except SavingsError:
        db.session.rollback()
        raise
    row.ledger_transaction_id = posted.id
    db.session.commit()
    return _posted(row.id, replayed=False), True


def _posted(request_id: int, replayed: bool) -> dict:
    row = db.session.get(PaymentRequest, request_id)
    tx = db.session.get(Transaction, row.ledger_transaction_id)
    account = db.session.get(SavingsAccount, tx.savings_account_id)
    return {
        "id": tx.id,
        "account_id": tx.savings_account_id,
        "type": tx.type.value,
        "amount": tx.amount,
        "reference": tx.reference,
        "balance": account.balance,
        "created_at": tx.created_at.isoformat(),
        "replayed": replayed,
        "source": tx.source,
        "request_id": row.id,
        "mpesa_receipt": row.mpesa_receipt,
    }


def refuse_large_interest(user_id: int, account_id: int, amount: int) -> None:
    """Interest is a credit with no M-Pesa receipt: above the SACCO's large-amount
    limit one officer cannot post it alone."""
    account = db.session.get(SavingsAccount, account_id)
    if account is None:
        return
    try:
        service._actor(user_id, account.sacco_id)
    except SavingsError:
        return
    if amount >= deposit_flags.controls(account.sacco_id)["large_amount_limit"]:
        audit_log.record(
            account.sacco_id,
            user_id,
            "savings.interest.flagged",
            f"savings_account:{account.id}",
        )
        db.session.commit()
        raise Conflict(
            "This credit is over the SACCO's large-amount limit and cannot be "
            "posted by one officer.",
            code=NEEDS_SECOND_APPROVAL,
        )


def refuse_flagged_withdrawal(user_id: int, account_id: int, amount: int) -> None:
    """A withdrawal one officer would post directly must not slip past the flags.

    A flagged one is refused here; it goes through the member's withdrawal
    request instead, which two officers approve.
    """
    account = db.session.get(SavingsAccount, account_id)
    if account is None:
        return  # the service answers with its own 404
    try:
        service._actor(user_id, account.sacco_id)
    except SavingsError:
        return  # and its own 403
    flags = deposit_flags.evaluate(
        account.sacco_id, account.membership_id, "withdrawal", amount, "direct"
    )
    if flags:
        audit_log.record(
            account.sacco_id,
            user_id,
            "savings.withdrawal.flagged",
            f"savings_account:{account.id} flags:{','.join(flags)}",
        )
        db.session.commit()
        raise Conflict(
            "This withdrawal is flagged and needs two officers. Ask the member "
            "to send a withdrawal request, which a second officer must approve.",
            code=NEEDS_SECOND_APPROVAL,
        )


def record_stk_payment(push: StkPush, _retry: bool = True) -> None:
    """A paid STK push becomes one pending deposit for an officer to confirm.

    Safaricom retries callbacks and an officer may already have typed the
    receipt in: an existing row for this receipt is linked, never duplicated.
    Below the SACCO's auto-confirm limit (default none) and with no flag, the
    deposit posts at once.
    """
    held = _by_receipt(push.sacco_id, push.mpesa_receipt)
    if held is not None:
        if (held.membership_id, held.amount_minor) != (push.membership_id, push.amount):
            # Never link a payment to a different member or amount: reconciliation
            # lists it instead.
            audit_log.record(
                push.sacco_id,
                None,
                "payment.stk_link_mismatch",
                f"stk:{push.id} payment_request:{held.id}",
            )
            db.session.commit()
            return
        if held.stk_push_id is None:
            held.stk_push_id = push.id
            audit_log.record(
                push.sacco_id,
                None,
                "payment.stk_linked",
                f"stk:{push.id} payment_request:{held.id}",
            )
            db.session.commit()
        return

    flags = deposit_flags.evaluate(
        push.sacco_id, push.membership_id, "deposit", push.amount, "stk"
    )
    row = PaymentRequest(
        sacco_id=push.sacco_id,
        membership_id=push.membership_id,
        kind="deposit",
        method="mpesa",
        amount_minor=push.amount,
        phone=push.phone,
        reference=f"stk-{push.id}",
        source="stk",
        mpesa_receipt=push.mpesa_receipt,
        stk_push_id=push.id,
        flags=deposit_flags.join(flags),
        status="pending",
    )
    db.session.add(row)
    audit_log.record(
        push.sacco_id, None, "payment.stk_pending", f"stk:{push.id} flags:{row.flags}"
    )
    try:
        db.session.commit()
    except IntegrityError:  # a manual entry for this receipt landed meanwhile
        db.session.rollback()
        if _retry:
            return record_stk_payment(push, _retry=False)
        raise

    limit = deposit_flags.controls(push.sacco_id)["auto_confirm_limit"]
    if flags or not 0 < push.amount <= limit:
        return
    account = db.session.execute(
        select(SavingsAccount).where(SavingsAccount.membership_id == push.membership_id)
    ).scalar_one_or_none()
    if account is None:
        return
    try:
        # One transaction: the deposit and the request's confirmation together.
        row = db.session.get(PaymentRequest, row.id)
        row.status = "confirmed"
        row.decided_at = func.now()
        db.session.flush()
        audit_log.record(
            push.sacco_id, None, "savings.request.auto_confirm", f"stk:{push.id}"
        )
        posted = service.deposit(
            None,
            account.id,
            push.amount,
            f"mpesa-{push.mpesa_receipt}",
            source="stk",
            system=True,
        )
        row.ledger_transaction_id = posted.id
        db.session.commit()
    except SavingsError:
        db.session.rollback()  # stays pending for an officer
    except Exception:
        db.session.rollback()
        log.exception("Auto-confirm failed for stk %s", push.id)
