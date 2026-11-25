"""Savings endpoints: open an account, deposit, withdraw, interest, fees."""

from dataclasses import asdict
from functools import wraps

from flask import jsonify, request

from app import savings_bp
from app.auth.limits import money_limit, write_limit
from app.errors import error_response
from app.savings import deposits, service
from app.savings.validation import (
    InvalidRequest,
    parse_amount,
    parse_money,
    parse_open_account,
)
from app.security.tenant import AccessDenied, current_user


def _error(message: str, status: int):
    return error_response(status, message)


def _body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _json(view) -> dict:
    """A result dataclass as a JSON-ready dict."""
    data = asdict(view)
    data["created_at"] = view.created_at.isoformat()
    return data


def signed_in(view):
    """Pass the caller's user id to the view and answer its refusals as JSON."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            user_id = current_user().id
        except AccessDenied:
            return error_response(401, "Authentication required.")
        try:
            return view(user_id, *args, **kwargs)
        except InvalidRequest as exc:
            return error_response(400, str(exc))
        except service.SavingsError as exc:
            return error_response(exc.status, exc.message, code=exc.code)

    return wrapper


@savings_bp.after_request
def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@savings_bp.post("/accounts")
@write_limit
@signed_in
def open_account(user_id: int):
    account = service.open_account(user_id, parse_open_account(_body()))
    return jsonify(_json(account)), 201


def _post_money(user_id: int, account_id: int, action):
    amount, reference = parse_money(_body())
    tx = action(user_id, account_id, amount, reference)
    # 201 for a new entry, 200 when a retried reference returns the original.
    return jsonify(_json(tx)), 200 if tx.replayed else 201


@savings_bp.post("/accounts/<int:account_id>/deposits")
@money_limit
@signed_in
def deposit(user_id: int, account_id: int):
    """A manual deposit: the officer types in the M-Pesa receipt."""
    body = _body()
    result, posted = deposits.manual_deposit(
        user_id,
        account_id,
        parse_amount(body.get("amount")),
        deposits.parse_receipt(body.get("mpesa_receipt")),
        body.get("phone"),
    )
    if not posted:  # flagged: waits for a second officer, or a retry of that
        return jsonify(queued=True, request=result), 202
    return jsonify(result), 200 if result["replayed"] else 201


@savings_bp.post("/accounts/<int:account_id>/withdrawals")
@money_limit
@signed_in
def withdraw(user_id: int, account_id: int):
    amount, _ = parse_money(_body())
    deposits.refuse_flagged_withdrawal(user_id, account_id, amount)
    return _post_money(user_id, account_id, service.withdraw)


@savings_bp.post("/accounts/<int:account_id>/interest")
@money_limit
@signed_in
def interest(user_id: int, account_id: int):
    amount, _ = parse_money(_body())
    deposits.refuse_large_interest(user_id, account_id, amount)
    return _post_money(user_id, account_id, service.post_interest)


@savings_bp.post("/accounts/<int:account_id>/fees")
@money_limit
@signed_in
def fee(user_id: int, account_id: int):
    return _post_money(user_id, account_id, service.post_fee)
