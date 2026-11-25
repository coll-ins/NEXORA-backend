"""The one error body every route answers with.

    {"error": {"code": "not_found", "message": "Not found."}}

Messages are written where the error is raised; the code comes from the status,
so a client can branch on `code` and show `message`.
"""

from flask import jsonify

CODES = {
    400: "validation_error",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    423: "locked",
    429: "rate_limited",
    500: "internal_error",
}


def error_response(status: int, message: str, code: str | None = None):
    """Return `(response, status)` in the API's error shape."""
    code = code or CODES.get(status, "error")
    return jsonify({"error": {"code": code, "message": message}}), status


# ---------------------------------------------------------------------------
# Named codes published in the work split (Section 1, Neo). Sections 2 and 3
# raise these by name so a client can branch on `code` instead of parsing text.
# A route adopts a named code by passing `code=` to `error_response`.
# ---------------------------------------------------------------------------

NOT_ALLOWED = "NOT_ALLOWED"
NOT_FOUND = "NOT_FOUND"
CODE_EXPIRED = "CODE_EXPIRED"
CODE_USED = "CODE_USED"
CODE_WRONG = "CODE_WRONG"
PIN_LOCKED = "PIN_LOCKED"
PIN_WRONG = "PIN_WRONG"
LOAN_NEEDS_APPROVAL = "LOAN_NEEDS_APPROVAL"
LOAN_SAME_OFFICER = "LOAN_SAME_OFFICER"
DUPLICATE_REFERENCE = "DUPLICATE_REFERENCE"
INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
REQUEST_ALREADY_DECIDED = "REQUEST_ALREADY_DECIDED"
OWN_REQUEST = "OWN_REQUEST"
RULES_LOCKED = "RULES_LOCKED"
RULES_NOT_ACCEPTED = "RULES_NOT_ACCEPTED"
LOAN_OVER_CAP = "LOAN_OVER_CAP"
LOAN_POOL_SHORT = "LOAN_POOL_SHORT"
LOAN_RULE_BROKEN = "LOAN_RULE_BROKEN"
PAYIN_SAME_OFFICER = "PAYIN_SAME_OFFICER"
PAYIN_NEW_OFFICER = "PAYIN_NEW_OFFICER"
CONSENT_REQUIRED = "CONSENT_REQUIRED"
DUPLICATE_RECEIPT = "DUPLICATE_RECEIPT"
SECOND_OFFICER_REQUIRED = "SECOND_OFFICER_REQUIRED"
NEEDS_SECOND_APPROVAL = "NEEDS_SECOND_APPROVAL"

# The status each named code is returned with, so nobody has to guess.
INTERFACE_CODES: dict[str, int] = {
    NOT_ALLOWED: 403,
    NOT_FOUND: 404,
    CODE_EXPIRED: 410,
    CODE_USED: 409,
    CODE_WRONG: 400,
    PIN_LOCKED: 423,
    PIN_WRONG: 401,
    LOAN_NEEDS_APPROVAL: 409,
    LOAN_SAME_OFFICER: 403,
    DUPLICATE_REFERENCE: 409,
    INSUFFICIENT_FUNDS: 409,
    REQUEST_ALREADY_DECIDED: 409,
    OWN_REQUEST: 403,
    RULES_LOCKED: 409,
    RULES_NOT_ACCEPTED: 409,
    LOAN_OVER_CAP: 409,
    LOAN_POOL_SHORT: 409,
    LOAN_RULE_BROKEN: 409,
    PAYIN_SAME_OFFICER: 403,
    PAYIN_NEW_OFFICER: 403,
    CONSENT_REQUIRED: 409,
    DUPLICATE_RECEIPT: 409,
    SECOND_OFFICER_REQUIRED: 403,
    NEEDS_SECOND_APPROVAL: 409,
}

PUBLISHED_CODES = frozenset(INTERFACE_CODES)


def interface_error(name: str, message: str):
    """Return `(response, status)` for one of the published named codes."""
    if name not in INTERFACE_CODES:
        raise KeyError(f"Unknown published code: {name!r}")
    return error_response(INTERFACE_CODES[name], message, code=name)
