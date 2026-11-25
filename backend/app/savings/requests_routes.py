"""Member deposit and withdrawal request endpoints (Neo, item 6)."""

from flask import jsonify, request

from app import savings_bp
from app.auth.limits import money_limit, read_limit, write_limit
from app.savings import request_service
from app.savings.request_validation import KINDS, parse_request
from app.savings.routes import _body, signed_in
from app.savings.validation import InvalidRequest, parse_id

STATUSES = ("pending", "confirmed", "rejected")


def _paging():
    try:
        limit = request.args.get("limit")
        cursor = request.args.get("cursor")
        return (
            int(limit) if limit is not None else None,
            int(cursor) if cursor is not None else None,
        )
    except (TypeError, ValueError):
        raise InvalidRequest("limit and cursor must be whole numbers.") from None


def _query_id(name: str) -> int:
    try:
        return parse_id(int(request.args.get(name)), name)
    except (TypeError, ValueError):
        raise InvalidRequest(f"{name} is required.") from None


def _one_of(value, allowed, name):
    if value is None:
        return None
    if value not in allowed:
        raise InvalidRequest(f"{name} must be one of: {', '.join(allowed)}.")
    return value


def _page(items, next_cursor):
    return jsonify(items=items, next=next_cursor)


@savings_bp.post("/requests")
@money_limit
@signed_in
def create_request(user_id: int):
    body = _body()
    sacco_id = parse_id(body.get("sacco_id"), "sacco_id")
    item, replayed = request_service.create(user_id, sacco_id, parse_request(body))
    return jsonify(item), 200 if replayed else 201


@savings_bp.get("/requests/me")
@read_limit
@signed_in
def my_requests(user_id: int):
    sacco_id = _query_id("sacco_id")
    kind = _one_of(request.args.get("kind"), KINDS, "kind")
    limit, cursor = _paging()
    items, next_cursor = request_service.list_mine(
        user_id, sacco_id, kind, limit, cursor
    )
    return _page(items, next_cursor)


@savings_bp.get("/saccos/<int:sacco_id>/requests")
@write_limit
@signed_in
def sacco_requests(user_id: int, sacco_id: int):
    status = _one_of(request.args.get("status"), STATUSES, "status")
    limit, cursor = _paging()
    items, next_cursor = request_service.list_for_sacco(
        user_id, sacco_id, status, limit, cursor
    )
    return _page(items, next_cursor)


@savings_bp.post("/requests/<int:request_id>/confirm")
@money_limit
@signed_in
def confirm_request(user_id: int, request_id: int):
    # Optional for a payment with an M-Pesa receipt: that receipt is its reference.
    reference = _body().get("reference")
    if reference is not None and (
        not isinstance(reference, str) or not reference.strip()
    ):
        raise InvalidRequest("reference is required.")
    return jsonify(request_service.confirm(user_id, request_id, reference))


@savings_bp.post("/requests/<int:request_id>/reject")
@write_limit
@signed_in
def reject_request(user_id: int, request_id: int):
    reason = _body().get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidRequest("Write a reason before you reject this request.")
    if len(reason) > 500:
        raise InvalidRequest("reason cannot exceed 500 characters.")
    return jsonify(request_service.reject(user_id, request_id, reason.strip()))
