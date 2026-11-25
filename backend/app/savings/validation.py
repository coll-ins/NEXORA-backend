import re
import unicodedata

# Per-transaction ceiling, in minor units: 1,000,000.00 in a 2-decimal currency.
MAX_TRANSACTION_AMOUNT = 100_000_000
MAX_ID = 2**31 - 1  # ids are 32-bit integers in Postgres
REFERENCE_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")


class InvalidRequest(ValueError):
    """The request body is not acceptable. The message is safe to show the client."""


def parse_id(value, name: str) -> int:
    # bool is a subclass of int, so reject it explicitly.
    if type(value) is not int or not 1 <= value <= MAX_ID:
        raise InvalidRequest(f"{name} must be a positive whole number.")
    return value


def parse_amount(value) -> int:
    """A positive whole number of minor units, within the per-transaction ceiling."""
    # type() rather than isinstance(): True is an int, and a float must not round.
    if type(value) is not int:
        raise InvalidRequest("amount must be a whole number of minor units.")
    if value <= 0:
        raise InvalidRequest("amount must be greater than zero.")
    if value > MAX_TRANSACTION_AMOUNT:
        raise InvalidRequest(f"amount cannot exceed {MAX_TRANSACTION_AMOUNT}.")
    return value


def parse_reference(value) -> str:
    """The client's idempotency key. A retry reuses it and cannot post twice.

    Required: a request without one could not be retried safely.
    """
    if value is None:
        raise InvalidRequest("reference is required.")
    if not isinstance(value, str) or not REFERENCE_RE.fullmatch(value):
        raise InvalidRequest(
            "reference must be 1 to 64 characters: letters, digits, . _ : -"
        )
    return value


def parse_override_reason(value, low: int = 10, high: int = 140) -> str | None:
    """Optional. A reason is a plain sentence the audit trail will keep."""
    if value is None:
        return None
    # Control characters, line separators and odd spaces would break or forge
    # the audit text: one line of ordinary characters only.
    plain = isinstance(value, str) and not any(
        unicodedata.category(ch).startswith(("C", "Zl", "Zp"))
        or (unicodedata.category(ch) == "Zs" and ch != " ")
        for ch in value.strip()
    )
    if not plain or not low <= len(value.strip()) <= high:
        raise InvalidRequest(
            f"override_reason must be {low} to {high} characters saying why, "
            "on one line."
        )
    return value.strip()


LOAN_SECTORS = (
    "agriculture",
    "trade",
    "manufacturing",
    "transport",
    "housing",
    "education",
    "health",
    "other",
)


def parse_sector(value) -> str | None:
    """Optional: what the loan is for. null or "" means not given."""
    if value is None or value == "":
        return None
    if value not in LOAN_SECTORS:
        raise InvalidRequest(f"sector must be one of: {', '.join(LOAN_SECTORS)}.")
    return value


def parse_flag(value, name: str) -> bool:
    """Optional true/false, false when absent."""
    if value is None:
        return False
    if type(value) is not bool:
        raise InvalidRequest(f"{name} must be true or false.")
    return value


def parse_open_account(body: dict) -> int:
    """Returns the membership id the account is opened for."""
    return parse_id(body.get("membership_id"), "membership_id")


def parse_money(body: dict) -> tuple[int, str]:
    """Returns (amount, reference) for a deposit, a withdrawal or a reversal."""
    return parse_amount(body.get("amount")), parse_reference(body.get("reference"))
