"""A member's own record: personal details (with consent) and share capital.

Every field is optional for now. Personal data (name, national ID, gender, date
of birth) is kept only while the member's consent is active (Data Protection
Act 2019) and is erased when they withdraw it. Share capital is not personal
data: an officer records it and it needs no consent.

Nothing is checked against the national register or any other source: NEXORA
stores what it is given.
"""

import re
import unicodedata
from datetime import UTC, date, datetime

from sqlalchemy import select

from app.extensions import db
from app.models.consent import MemberDataConsent
from app.models.membership import SaccoMembership

PURPOSE = "member_profile"
PERSONAL = ("full_name", "national_id", "gender", "date_of_birth")
FIELDS = (*PERSONAL, "share_capital")
GENDERS = ("female", "male", "other")
NATIONAL_ID = re.compile(r"[0-9]{7,8}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
EARLIEST = date(1900, 1, 1)
MAX_SHARE_CAPITAL = 10**12  # minor units
NAME_LIMIT = 120

# The words a member agrees to. Bump `version` whenever the meaning changes: a
# consent records which version was shown. Have a lawyer review before real use.
NOTICE = {
    "version": 1,
    "purpose": PURPOSE,
    "text": (
        "This SACCO asks to keep your full name, national ID number, gender and "
        "date of birth in NEXORA. It uses them to identify you as a member and to "
        "keep its member records. Only you and this SACCO's admin can read them. "
        "Giving them is your choice, and you can use NEXORA without them. You can "
        "correct them at any time. If you withdraw your consent, NEXORA deletes "
        "these four details at once. Your savings and loan records stay, because "
        "the SACCO must keep its accounts."
    ),
}


def active_consent(membership_id: int) -> MemberDataConsent | None:
    return db.session.execute(
        select(MemberDataConsent).where(
            MemberDataConsent.membership_id == membership_id,
            MemberDataConsent.purpose == PURPOSE,
            MemberDataConsent.withdrawn_at.is_(None),
        )
    ).scalar_one_or_none()


def consent_json(row: MemberDataConsent | None) -> dict | None:
    if row is None:
        return None
    return {
        "purpose": row.purpose,
        "notice_version": row.notice_version,
        "granted_at": row.granted_at.isoformat(),
    }


def profile_json(membership: SaccoMembership) -> dict:
    return {
        "membership_id": membership.id,
        "full_name": membership.full_name,
        "national_id": membership.national_id,
        "gender": membership.gender,
        "date_of_birth": (
            membership.date_of_birth.isoformat() if membership.date_of_birth else None
        ),
        "share_capital": membership.share_capital,
        "consent": consent_json(active_consent(membership.id)),
    }


def withdraw(membership: SaccoMembership, consent: MemberDataConsent) -> list[str]:
    """End the consent and erase the personal details. Returns the field names
    that held a value (never the values)."""
    erased = [f for f in PERSONAL if getattr(membership, f) is not None]
    for field in PERSONAL:
        setattr(membership, field, None)
    consent.withdrawn_at = datetime.now(UTC)
    return erased


def _clean_name(value, errors: dict):
    if not isinstance(value, str):
        errors["full_name"] = "Full name must be text. Example: Wanjiru Mwangi."
        return None
    name = " ".join(unicodedata.normalize("NFC", value).split())
    hidden = any(unicodedata.category(ch).startswith("C") for ch in name)
    odd = any(not (ch.isalpha() or ch in " '.-") for ch in name)
    if len(name) < 2 or len(name) > NAME_LIMIT or hidden or odd:
        errors["full_name"] = (
            f"Full name must be 2 to {NAME_LIMIT} characters: letters, spaces, "
            "apostrophes, hyphens and full stops. Example: Wanjiru Mwangi."
        )
        return None
    return name


def validate(body: dict) -> tuple[dict, dict]:
    """(values, errors) for the fields present in `body`. null or "" clears."""
    values: dict = {}
    errors: dict = {}
    unknown = sorted(set(body) - set(FIELDS))
    if unknown:
        errors["_"] = f"Unknown field(s): {', '.join(unknown)}."
    for field in FIELDS:
        if field not in body:
            continue
        raw = body[field]
        if raw is None or raw == "":
            values[field] = None
        elif field == "full_name":
            values[field] = _clean_name(raw, errors)
        elif field == "national_id":
            if isinstance(raw, str) and NATIONAL_ID.fullmatch(raw):
                values[field] = raw
            else:
                errors[field] = (
                    "National ID must be 7 or 8 digits, no spaces. Example: 12345678."
                )
        elif field == "gender":
            if raw in GENDERS:
                values[field] = raw
            else:
                errors[field] = "Gender must be female, male or other."
        elif field == "date_of_birth":
            values[field] = _date(raw, errors)
        else:  # share_capital
            if type(raw) is int and 0 <= raw <= MAX_SHARE_CAPITAL:
                values[field] = raw
            else:
                errors[field] = (
                    "Share capital must be a whole number of minor units, zero or "
                    "more. Example: 500000 for KES 5,000.00."
                )
    return values, errors


def _date(raw, errors: dict):
    message = "Date of birth must be YYYY-MM-DD. Example: 1990-06-15."
    if not isinstance(raw, str) or not DATE_RE.fullmatch(raw):
        errors["date_of_birth"] = message
        return None
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        errors["date_of_birth"] = "That is not a real date. Example: 1990-06-15."
        return None
    if not EARLIEST <= day <= date.today():
        errors["date_of_birth"] = (
            "Date of birth cannot be before 1900 or in the future."
        )
        return None
    return day
