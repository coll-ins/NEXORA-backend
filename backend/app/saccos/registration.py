"""A SACCO's registration details: number, SASRA licence number and date.

All three are optional for now. When one is sent it must look like one; an empty
string or null clears it. Nothing here is checked against a register: NEXORA
records what the SACCO says, it does not verify it with SASRA.
"""

import re
from datetime import date

FIELDS = ("registration_number", "sasra_licence_number", "registration_date")
NUMBER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ./-]{1,38}[A-Za-z0-9]")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
EARLIEST = date(1900, 1, 1)

_LABEL = {
    "registration_number": "Registration number",
    "sasra_licence_number": "SASRA licence number",
}


def _number(field: str, value, errors: dict):
    if value is None or value == "":
        return None
    label = _LABEL[field]
    if not isinstance(value, str):
        errors[field] = f"{label} must be text. Example: CS/12345."
        return None
    value = " ".join(value.split())
    if not NUMBER_RE.fullmatch(value):
        errors[field] = (
            f"{label} must be 3 to 40 characters: letters, digits, spaces and "
            ". / - only. Example: CS/12345."
        )
        return None
    return value


def validate(body: dict, fields=FIELDS) -> tuple[dict, dict]:
    """(values, errors) for the registration fields present in `body`."""
    values: dict = {}
    errors: dict = {}
    for field in fields:
        if field not in body:
            continue
        raw = body[field]
        if field == "registration_date":
            if raw is None or raw == "":
                values[field] = None
                continue
            if not isinstance(raw, str) or not DATE_RE.fullmatch(raw):
                errors[field] = (
                    "Registration date must be YYYY-MM-DD. Example: 2019-03-28."
                )
                continue
            try:
                day = date.fromisoformat(raw)
            except ValueError:
                errors[field] = "That is not a real date. Example: 2019-03-28."
                continue
            if not EARLIEST <= day <= date.today():
                errors[field] = (
                    "Registration date cannot be before 1900 or in the future."
                )
                continue
            values[field] = day
        else:
            values[field] = _number(field, raw, errors)
    return values, errors
