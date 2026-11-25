"""Member deposit and withdrawal requests.

A request changes no balance. Confirming one calls the existing
`savings.service.deposit` or `.withdraw`, so the ledger rules (idempotent
reference, insufficient funds, tenant check, audit) stay in one place.
"""

from sqlalchemy import func, select

from app.audit import log as audit_log
from app.errors import (
    DUPLICATE_REFERENCE,
    INSUFFICIENT_FUNDS,
    OWN_REQUEST,
    REQUEST_ALREADY_DECIDED,
    SECOND_OFFICER_REQUIRED,
)
from app.extensions import db
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.payment_request import PaymentRequest
from app.models.savings import SavingsAccount
from app.models.user import User
from app.savings import flags as deposit_flags
from app.savings import service
from app.savings.service import (
    Conflict,
    Forbidden,
    NotFound,
    _require_sacco_active,
)
from app.savings.validation import InvalidRequest
from app.security.permissions import can

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def _item(row: PaymentRequest, email: str | None = None) -> dict:
    return {
        "id": row.id,
        "sacco_id": row.sacco_id,
        "membership_id": row.membership_id,
        "email": email,
        "kind": row.kind,
        "method": row.method,
        "amount_minor": row.amount_minor,
        "phone": row.phone,
        "bank_name": row.bank_name,
        "account_ref": row.account_ref,
        "reference": row.reference,
        "source": row.source,
        "mpesa_receipt": row.mpesa_receipt,
        "flags": deposit_flags.split(row.flags),
        "awaiting_second_approval": bool(
            row.flags and row.first_approved_by and row.status == "pending"
        ),
        "first_approved_by": row.first_approved_by,
        "status": row.status,
        "reject_reason": row.reject_reason,
        "ledger_transaction_id": row.ledger_transaction_id,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "created_at": row.created_at.isoformat(),
    }


def _membership(user_id: int, sacco_id: int) -> SaccoMembership | None:
    return db.session.execute(
        select(SaccoMembership).where(
            SaccoMembership.user_id == user_id, SaccoMembership.sacco_id == sacco_id
        )
    ).scalar_one_or_none()


def _acting(user_id: int, sacco_id: int, permission: str) -> SaccoMembership:
    """The caller's active membership here, if it holds `permission`.

    Decided by `can()` alone, never by the role column, like `service._actor`.
    """
    membership = _membership(user_id, sacco_id)
    if membership is None:
        raise NotFound("Not found.")
    user = db.session.get(User, user_id)
    if membership.status is not MembershipStatus.ACTIVE or not can(
        user, sacco_id, permission
    ):
        raise Forbidden("You are not allowed to do this.")
    return membership


def _account(membership_id: int) -> SavingsAccount | None:
    return db.session.execute(
        select(SavingsAccount).where(SavingsAccount.membership_id == membership_id)
    ).scalar_one_or_none()


def _email(membership_id: int) -> str | None:
    return db.session.execute(
        select(User.email)
        .join(SaccoMembership, SaccoMembership.user_id == User.id)
        .where(SaccoMembership.id == membership_id)
    ).scalar_one_or_none()


def _clamp(limit) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def _page(stmt, limit, cursor, with_email: bool):
    """Newest first, `limit + 1` rows read to know whether there is a next page."""
    if cursor is not None:
        stmt = stmt.where(PaymentRequest.id < int(cursor))
    stmt = stmt.order_by(PaymentRequest.id.desc()).limit(limit + 1)
    rows = db.session.execute(stmt).all()
    more = len(rows) > limit
    rows = rows[:limit]
    if with_email:
        items = [_item(row, email) for row, email in rows]
    else:
        items = [_item(row) for (row,) in rows]
    return items, (items[-1]["id"] if more else None)


def create(user_id: int, sacco_id: int, data: dict) -> tuple[dict, bool]:
    """(item, replayed). A reused reference returns the original row."""
    actor = _acting(user_id, sacco_id, "savings.request")
    _require_sacco_active(sacco_id)

    existing = db.session.execute(
        select(PaymentRequest).where(PaymentRequest.reference == data["reference"])
    ).scalar_one_or_none()
    if existing is not None:
        same = (
            existing.membership_id == actor.id
            and existing.kind == data["kind"]
            and existing.method == data["method"]
            and existing.amount_minor == data["amount"]
        )
        if same:
            return _item(existing, _email(actor.id)), True
        raise Conflict(
            "That reference has already been used.", code=DUPLICATE_REFERENCE
        )

    if data["kind"] == "withdrawal":
        account = _account(actor.id)
        balance = account.balance if account is not None else 0
        if data["amount"] > balance:
            raise Conflict("Insufficient funds.", code=INSUFFICIENT_FUNDS)

    row = PaymentRequest(
        sacco_id=sacco_id,
        membership_id=actor.id,
        kind=data["kind"],
        method=data["method"],
        amount_minor=data["amount"],
        phone=data["phone"],
        bank_name=data["bank_name"],
        account_ref=data["account_ref"],
        reference=data["reference"],
        status="pending",
        flags=deposit_flags.join(
            deposit_flags.evaluate(
                sacco_id, actor.id, data["kind"], data["amount"], "member"
            )
        ),
    )
    db.session.add(row)
    db.session.commit()
    db.session.refresh(row)
    return _item(row, _email(actor.id)), False


def list_mine(user_id: int, sacco_id: int, kind, limit, cursor):
    actor = _membership(user_id, sacco_id)
    if actor is None:
        raise NotFound("Not found.")
    stmt = select(PaymentRequest).where(
        PaymentRequest.membership_id == actor.id,
        PaymentRequest.sacco_id == sacco_id,
    )
    if kind is not None:
        stmt = stmt.where(PaymentRequest.kind == kind)
    items, nxt = _page(stmt, _clamp(limit), cursor, with_email=False)
    for item in items:  # a member must not learn the flag thresholds by probing
        for key in ("flags", "first_approved_by", "awaiting_second_approval"):
            item.pop(key, None)
    return items, nxt


def list_for_sacco(user_id: int, sacco_id: int, status, limit, cursor):
    _acting(user_id, sacco_id, "savings.post")
    stmt = (
        select(PaymentRequest, User.email)
        .join(SaccoMembership, SaccoMembership.id == PaymentRequest.membership_id)
        .join(User, User.id == SaccoMembership.user_id)
        .where(PaymentRequest.sacco_id == sacco_id)
    )
    if status is not None:
        stmt = stmt.where(PaymentRequest.status == status)
    return _page(stmt, _clamp(limit), cursor, with_email=True)


def ledger_reference(row: PaymentRequest, client_reference: str | None) -> str:
    """A payment with an M-Pesa receipt posts under that receipt, so the same
    receipt can never reach the ledger twice, whatever the client sends."""
    if row.mpesa_receipt:
        return f"mpesa-{row.mpesa_receipt}"
    if not client_reference:
        raise InvalidRequest("reference is required.")
    return client_reference


def confirm(user_id: int, request_id: int, reference: str | None) -> dict:
    """Confirm one pending request: one ledger entry, then status confirmed.

    A flagged request needs two different officers: the first call records the
    first approval and posts nothing; a second officer's call posts it.
    """
    row = db.session.execute(
        select(PaymentRequest)
        .where(PaymentRequest.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        db.session.rollback()
        raise NotFound("Not found.")

    officer = _acting(user_id, row.sacco_id, "savings.post")
    if row.membership_id == officer.id:
        db.session.rollback()
        raise Forbidden(
            "You cannot decide your own request.", code=OWN_REQUEST
        )
    if row.status != "pending":
        db.session.rollback()
        raise Conflict(
            "Someone already decided this request.",
            code=REQUEST_ALREADY_DECIDED,
        )
    _require_sacco_active(row.sacco_id)
    try:
        ledger_ref = ledger_reference(row, reference)
    except InvalidRequest:
        db.session.rollback()
        raise
    account = _account(row.membership_id)
    if account is None:
        db.session.rollback()
        raise Conflict("The member has no savings account in this SACCO.")

    if row.kind == "withdrawal" and row.amount_minor > account.balance:
        # Refuse now, not after a first approval that could never be completed.
        db.session.rollback()
        raise Conflict("Insufficient funds.", code=INSUFFICIENT_FUNDS)

    flags = sorted(
        set(deposit_flags.split(row.flags))
        | set(
            deposit_flags.evaluate(
                row.sacco_id,
                row.membership_id,
                row.kind,
                row.amount_minor,
                row.source,
                exclude_request_id=row.id,
            )
        )
    )
    if flags:
        row.flags = deposit_flags.join(flags)
        if row.first_approved_by is None:
            row.first_approved_by = officer.id
            row.first_approved_at = func.now()
            audit_log.record(
                row.sacco_id,
                user_id,
                "savings.request.first_approve",
                f"payment_request:{row.id} flags:{row.flags}",
            )
            db.session.commit()
            db.session.refresh(row)
            return _item(row, _email(row.membership_id))
        if row.first_approved_by == officer.id:
            db.session.rollback()
            raise Forbidden(
                "A flagged payment needs a second, different officer.",
                code=SECOND_OFFICER_REQUIRED,
            )

    # Mark the decision before the money moves: the ledger call commits the same
    # transaction, so a second officer finds the row decided even mid-flight. A
    # refused ledger entry rolls the status back with it (the request stays
    # pending, which is what the queue should show).
    row.status = "confirmed"
    row.decided_by = officer.id
    row.decided_at = func.now()
    db.session.flush()
    audit_log.record(
        row.sacco_id, user_id, "savings.request.confirm", f"payment_request:{row.id}"
    )

    if row.kind == "deposit":
        source = {"stk": "stk", "manual": "manual"}.get(row.source, "standard")
        posted = service.deposit(
            user_id, account.id, row.amount_minor, ledger_ref, source=source
        )
    else:
        posted = service.withdraw(user_id, account.id, row.amount_minor, ledger_ref)

    row.ledger_transaction_id = posted.id
    db.session.commit()
    db.session.refresh(row)
    return _item(row, _email(row.membership_id))


def reject(user_id: int, request_id: int, reason: str) -> dict:
    row = db.session.execute(
        select(PaymentRequest)
        .where(PaymentRequest.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        db.session.rollback()
        raise NotFound("Not found.")

    officer = _acting(user_id, row.sacco_id, "savings.post")
    if row.membership_id == officer.id:
        db.session.rollback()
        raise Forbidden(
            "You cannot decide your own request.", code=OWN_REQUEST
        )
    if row.status != "pending":
        db.session.rollback()
        raise Conflict(
            "Someone already decided this request.",
            code=REQUEST_ALREADY_DECIDED,
        )
    if row.first_approved_by == officer.id:
        db.session.rollback()
        raise Forbidden(
            "You gave the first approval; a different officer must reject it.",
            code=SECOND_OFFICER_REQUIRED,
        )
    row.status = "rejected"
    row.reject_reason = reason
    row.decided_by = officer.id
    row.decided_at = func.now()
    audit_log.record(
        row.sacco_id, user_id, "savings.request.reject", f"payment_request:{row.id}"
    )
    db.session.commit()
    db.session.refresh(row)
    return _item(row, _email(row.membership_id))


def item_by_id(request_id: int) -> dict:
    row = db.session.get(PaymentRequest, request_id)
    return _item(row, _email(row.membership_id))
