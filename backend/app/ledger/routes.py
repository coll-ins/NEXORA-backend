"""Reversals take two officers.

POST /savings/transactions/<id>/reversals     one officer asks, with a `reason`
GET  /savings/saccos/<id>/reversal-requests   the SACCO's requests (`?status=`)
POST /savings/reversal-requests/<id>/approve  a different officer: `reference`, and
                                              `override_reason` only to override
                                              the loan-cap block
POST /savings/reversal-requests/<id>/reject   a different officer: `reason`
"""

from flask import jsonify, request

from app import savings_bp
from app.auth.limits import money_limit, read_limit, write_limit
from app.ledger import reversals
from app.savings.routes import _body, _json, signed_in
from app.savings.validation import (
    InvalidRequest,
    parse_override_reason,
    parse_reference,
)

STATUSES = ("pending", "approved", "rejected")


def _reason(value, what: str) -> str:
    reason = parse_override_reason(value, reversals.MIN_REASON, reversals.MAX_REASON)
    if reason is None:
        raise InvalidRequest(
            f"Write why: {what} is {reversals.MIN_REASON} to "
            f"{reversals.MAX_REASON} characters."
        )
    return reason


@savings_bp.post("/transactions/<int:transaction_id>/reversals")
@money_limit
@signed_in
def request_reversal(user_id: int, transaction_id: int):
    reason = _reason(_body().get("reason"), "reason")
    item, replayed = reversals.request_reversal(user_id, transaction_id, reason)
    return jsonify(item), 200 if replayed else 201


@savings_bp.get("/saccos/<int:sacco_id>/reversal-requests")
@read_limit
@signed_in
def reversal_requests(user_id: int, sacco_id: int):
    status = request.args.get("status")
    if status is not None and status not in STATUSES:
        raise InvalidRequest(f"status must be one of: {', '.join(STATUSES)}.")
    return jsonify(items=reversals.list_requests(user_id, sacco_id, status))


@savings_bp.post("/reversal-requests/<int:request_id>/approve")
@money_limit
@signed_in
def approve_reversal(user_id: int, request_id: int):
    body = _body()
    reference = parse_reference(body.get("reference"))
    override = parse_override_reason(
        body.get("override_reason"), reversals.MIN_REASON, reversals.MAX_REASON
    )
    view = reversals.approve_reversal(user_id, request_id, reference, override)
    return jsonify(_json(view)), 200 if view.replayed else 201


@savings_bp.post("/reversal-requests/<int:request_id>/reject")
@write_limit
@signed_in
def reject_reversal(user_id: int, request_id: int):
    reason = _reason(_body().get("reason"), "reason")
    return jsonify(reversals.reject_reversal(user_id, request_id, reason))
