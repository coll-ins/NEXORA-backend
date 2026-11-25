"""Validation for a member's deposit or withdrawal request.

The names and shapes are the contract the Deposit and Withdraw screens build
against (Master file, section 3b), so they do not change.
"""

import re

from app.savings.validation import InvalidRequest, parse_amount, parse_reference

KINDS = ("deposit", "withdrawal")
METHODS = ("mpesa", "bank")
PHONE_MESSAGE = "Enter a Safaricom number like 0712 345 678."
PHONE_RE = re.compile(r"254[71]\d{8}")


def normalize_phone(value) -> str:
    """`0712 345 678`, `+254 712 345 678`, `254712345678` -> `254712345678`."""
    digits = re.sub(r"[\s-]", "", str(value or ""))
    if digits.startswith("+"):
        digits = digits[1:]
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    if not PHONE_RE.fullmatch(digits):
        raise InvalidRequest(PHONE_MESSAGE)
    return digits


def _text(value, name: str, low: int, high: int) -> str:
    if not isinstance(value, str):
        raise InvalidRequest(f"{name} is required.")
    text = value.strip()
    if not low <= len(text) <= high:
        raise InvalidRequest(f"{name} must be {low} to {high} characters.")
    return text


def _member_reference(value) -> str:
    """`manual-` and `stk-` belong to rows the system writes; a member cannot
    pre-occupy them."""
    reference = parse_reference(value)
    if reference.lower().startswith(("manual-", "stk-", "mpesa-")):
        raise InvalidRequest("reference cannot start with manual-, stk- or mpesa-.")
    return reference


def parse_request(body: dict) -> dict:
    """A clean request, or `InvalidRequest` (the routes answer it as a 400)."""
    kind = body.get("kind")
    if kind not in KINDS:
        raise InvalidRequest("kind must be 'deposit' or 'withdrawal'.")
    method = body.get("method")
    if method not in METHODS:
        raise InvalidRequest("method must be 'mpesa' or 'bank'.")
    clean = {
        "kind": kind,
        "method": method,
        "amount": parse_amount(body.get("amount")),
        "reference": _member_reference(body.get("reference")),
        "phone": None,
        "bank_name": None,
        "account_ref": None,
    }
    if method == "mpesa":
        clean["phone"] = normalize_phone(body.get("phone"))
    else:
        clean["bank_name"] = _text(body.get("bank_name"), "bank_name", 2, 60)
        clean["account_ref"] = _text(body.get("account_ref"), "account_ref", 3, 40)
    return clean
