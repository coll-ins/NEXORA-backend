"""SACCO pay-in details: validation, the live row, and the two-officer change.

A change is proposed by one officer (`payin.manage`) and goes live only when a
different officer approves that exact proposal. Nothing here moves money.
"""

import logging
import re
import threading
import unicodedata
from datetime import UTC, datetime, timedelta

from flask import current_app
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.extensions import db
from app.mail import send_text_email
from app.models.payment_details import (
    SaccoPaymentDetails,
    SaccoPaymentDetailsChange,
)
from app.models.sacco import Sacco
from app.models.user import User
from app.payments.daraja import msisdn
from app.security.permissions import holders

log = logging.getLogger(__name__)

PERMISSION = "payin.manage"
MAX_PROPOSALS_PER_HOUR = 10
FIELDS = (
    "mpesa_type",
    "mpesa_number",
    "account_reference_hint",
    "display_name",
    "bank_name",
    "bank_account_name",
    "bank_account_number",
    "bank_branch",
    "bank_uses_shared_paybill",
    "bank_paybill_number",
)
# ASCII only: `\d` would also accept Arabic-Indic and other Unicode digits.
MPESA_NUMBER = re.compile(r"[0-9]{5,7}")
BANK_ACCOUNT = re.compile(r"[0-9]{6,20}")


def lock_sacco(sacco_id: int) -> None:
    """Serialise proposals and approvals for one SACCO."""
    db.session.execute(
        select(Sacco.id).where(Sacco.id == sacco_id).with_for_update()
    ).scalar_one_or_none()


# Hidden, joining, line-breaking, private or unassigned characters, and combining
# marks that survive normalisation: none of them can be seen, so an approver or a
# member could be shown a name that looks identical to a different one.
_REFUSED = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs", "Mn", "Mc", "Me", "Sk"}


def _looks_plain(value: str) -> bool:
    """Latin letters, ASCII digits, ordinary spaces and punctuation, one line.

    A Cyrillic or Greek letter that looks Latin is refused: the name is what
    members are told to match against what M-Pesa shows.
    """
    for ch in value:
        category = unicodedata.category(ch)
        if ch == " ":
            continue
        if category in _REFUSED:
            return False
        if category.startswith("L") and not unicodedata.name(ch, "").startswith(
            "LATIN"
        ):
            return False
        if category == "Nd" and not ch.isascii():
            return False
    return True


def _text(value, limit: int, label: str, example: str):
    """(clean value or None, reason or None). Blank means "not given"."""
    if value is None or value == "":
        return None, None
    plain = (
        f"{label} must be one line of Latin letters, digits and ordinary "
        f"punctuation, because members compare it with what M-Pesa shows. "
        f"Example: {example}."
    )
    if not isinstance(value, str):
        return None, plain
    try:
        value.encode("utf-8")  # a lone surrogate would fail later, in the database
    except UnicodeEncodeError:
        return None, plain
    value = unicodedata.normalize("NFC", value).strip()
    if not _looks_plain(value):
        return None, plain
    if len(value) > limit:
        return None, f"{label} can be at most {limit} characters. Example: {example}."
    return (value or None), None


def validate(body) -> tuple[dict, dict]:
    """(values, errors). `errors` maps a field to a plain-words reason that says
    the format, gives an example and says why it was refused."""
    if not isinstance(body, dict):
        return {}, {"_": "Send the pay-in details as a JSON object."}
    errors: dict[str, str] = {}
    unknown = sorted(set(body) - set(FIELDS))
    if unknown:
        errors["_"] = f"Unknown field(s): {', '.join(unknown)}."
    values: dict = {}

    def text_field(field, limit, label, example):
        value, reason = _text(body.get(field), limit, label, example)
        values[field] = value
        if reason:
            errors[field] = reason

    text_field("display_name", 60, "Name members will see", "Umoja SACCO Savings")
    text_field(
        "account_reference_hint", 60, "Account reference hint", "your member number"
    )
    text_field("bank_name", 60, "Bank name", "Equity Bank")
    text_field("bank_account_name", 80, "Bank account name", "Umoja SACCO")
    text_field("bank_branch", 60, "Bank branch", "Kitale")

    mpesa_type = body.get("mpesa_type")
    mpesa_number = body.get("mpesa_number")
    if mpesa_type in (None, "") and mpesa_number in (None, ""):
        values["mpesa_type"] = values["mpesa_number"] = None
    else:
        if mpesa_type in ("paybill", "till", "pochi"):
            values["mpesa_type"] = mpesa_type
        else:
            values["mpesa_type"] = None
            errors["mpesa_type"] = (
                "M-Pesa type must be paybill, till or pochi, so members know which "
                "M-Pesa menu to use. Example: paybill."
            )
        if mpesa_type == "pochi":
            # Pochi la Biashara is a Safaricom phone number; it is shown to
            # members and never prompted (record only).
            values["mpesa_number"] = msisdn(mpesa_number)
            if values["mpesa_number"] is None:
                errors["mpesa_number"] = (
                    "A Pochi la Biashara number is a Safaricom phone number. "
                    "Example: 0712345678."
                )
        elif isinstance(mpesa_number, str) and MPESA_NUMBER.fullmatch(mpesa_number):
            values["mpesa_number"] = mpesa_number
        else:
            values["mpesa_number"] = None
            errors["mpesa_number"] = (
                "The paybill or till number must be 5 to 7 digits, with no spaces "
                "or letters, because M-Pesa numbers are never shorter or longer. "
                "Example: 400200."
            )
        if values["display_name"] is None and "display_name" not in errors:
            errors["display_name"] = (
                "Add the name members will see when M-Pesa shows the business "
                "name, so they can check it before paying. Example: Umoja SACCO "
                "Savings."
            )
    if values["account_reference_hint"] and values["mpesa_type"] != "paybill":
        errors.setdefault(
            "account_reference_hint",
            "Only a paybill has an account number. Leave this empty for a till "
            "or Pochi.",
        )

    account = body.get("bank_account_number")
    has_bank = (
        values["bank_name"] is not None
        or values["bank_account_name"] is not None
        or account not in (None, "")
    )
    if has_bank:
        for field, label, example in (
            ("bank_name", "Bank name", "Equity Bank"),
            ("bank_account_name", "Bank account name", "Umoja SACCO"),
        ):
            if values[field] is None and field not in errors:
                errors[field] = (
                    f"{label} is needed once any bank detail is given, so members "
                    f"can pay the right account. Example: {example}."
                )
        if isinstance(account, str) and BANK_ACCOUNT.fullmatch(account):
            values["bank_account_number"] = account
        else:
            values["bank_account_number"] = None
            errors["bank_account_number"] = (
                "The bank account number must be 6 to 20 digits, with no spaces "
                "or dashes, so a typo cannot send money elsewhere. "
                "Example: 0123456789."
            )
    else:
        values["bank_account_number"] = None
        if values["bank_branch"]:
            errors["bank_branch"] = (
                "A branch needs a bank. Add the bank name, account name and "
                "account number too. Example: Equity Bank, Kitale branch."
            )

    shared = body.get("bank_uses_shared_paybill", False)
    paybill = body.get("bank_paybill_number")
    if not isinstance(shared, bool):
        values["bank_uses_shared_paybill"] = False
        values["bank_paybill_number"] = None
        errors["bank_uses_shared_paybill"] = (
            "Send true or false. Example: true when members pay the bank by paybill."
        )
    elif shared:
        values["bank_uses_shared_paybill"] = True
        if not has_bank:
            errors["bank_uses_shared_paybill"] = (
                "A shared bank paybill needs the bank details above it, so members "
                "know which account the paybill pays into. Example: Equity Bank, "
                "account 0123456789."
            )
        if isinstance(paybill, str) and MPESA_NUMBER.fullmatch(paybill):
            values["bank_paybill_number"] = paybill
        else:
            values["bank_paybill_number"] = None
            errors["bank_paybill_number"] = (
                "Give the bank's shared paybill number: 5 to 7 digits, no spaces. "
                "Example: 247247."
            )
    else:
        values["bank_uses_shared_paybill"] = False
        values["bank_paybill_number"] = None
        if paybill not in (None, ""):
            errors["bank_paybill_number"] = (
                "Leave this empty unless the bank uses a shared paybill. Turn the "
                "shared paybill on first."
            )

    if not errors and values["mpesa_number"] is None and values["bank_name"] is None:
        errors["_"] = (
            "Give M-Pesa details, bank details or both, so members have somewhere "
            "to pay."
        )
    return values, errors


def values_json(row) -> dict:
    return {
        **{field: getattr(row, field) for field in FIELDS},
        # A shared bank paybill cannot name the SACCO, so only an officer who
        # reads the bank statement can confirm those payments.
        "bank_manual_confirm_only": row.bank_uses_shared_paybill,
    }


def live_json(row: SaccoPaymentDetails) -> dict:
    return {**values_json(row), "updated_at": row.updated_at.isoformat()}


def change_json(row: SaccoPaymentDetailsChange) -> dict:
    proposer = db.session.get(User, row.proposed_by)
    return {
        "id": row.id,
        "status": row.status,
        "proposed_by": row.proposed_by,
        "proposed_by_email": proposer.email if proposer else None,
        "proposed_at": row.proposed_at.isoformat(),
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "go_live_at": row.go_live_at.isoformat() if row.go_live_at else None,
        "values": values_json(row),
    }


def live_details(sacco_id: int) -> SaccoPaymentDetails | None:
    return db.session.execute(
        select(SaccoPaymentDetails).where(SaccoPaymentDetails.sacco_id == sacco_id)
    ).scalar_one_or_none()


def _due_change(sacco_id: int) -> SaccoPaymentDetailsChange | None:
    live = live_details(sacco_id)
    query = (
        select(SaccoPaymentDetailsChange)
        .where(
            SaccoPaymentDetailsChange.sacco_id == sacco_id,
            SaccoPaymentDetailsChange.status == "approved",
            SaccoPaymentDetailsChange.go_live_at <= datetime.now(UTC),
        )
        .order_by(SaccoPaymentDetailsChange.go_live_at.desc())
        .limit(1)
    )
    if live is not None:
        query = query.where(SaccoPaymentDetailsChange.go_live_at > live.updated_at)
    return db.session.execute(query).scalar_one_or_none()


def apply_due(sacco_id: int) -> None:
    """Put an approved change that is not live yet onto the live row.

    Approval applies a change at once, so this normally finds nothing; it also
    finishes a change approved while a go-live delay still existed. There is no
    background job: whoever reads the details next applies it. Commit only when
    something went live.
    """
    if _due_change(sacco_id) is None:
        return
    lock_sacco(sacco_id)
    change = _due_change(sacco_id)  # another request may have applied it
    if change is not None:
        go_live(change)
    db.session.commit()


def pending_change(sacco_id: int) -> SaccoPaymentDetailsChange | None:
    return db.session.execute(
        select(SaccoPaymentDetailsChange).where(
            SaccoPaymentDetailsChange.sacco_id == sacco_id,
            SaccoPaymentDetailsChange.status == "pending",
        )
    ).scalar_one_or_none()


def changed_fields(row, other_values: dict) -> list[str]:
    """Field names (never the numbers) where `other_values` differs from `row`
    (the live row, or None when nothing is live yet)."""
    if row is None:
        return [f for f in FIELDS if other_values[f] not in (None, False)]
    return [f for f in FIELDS if getattr(row, f) != other_values[f]]


def officers(sacco_id: int) -> list[User]:
    """Active members who hold `payin.manage`, decided the way `can()` decides."""
    ids = holders(sacco_id, PERMISSION)
    return list(db.session.execute(select(User).where(User.id.in_(ids))).scalars())


def _send_all(
    sacco_id: int, recipients: list[tuple[int, str]], subject: str, body: str
) -> None:
    for user_id, email in recipients:
        try:
            send_text_email(email, subject, body)
        except Exception:
            # Ids only: the pay-in numbers must never reach the logs.
            log.exception("Could not email officer %s of SACCO %s", user_id, sacco_id)


def notify_officers(
    sacco_id: int, subject: str, body: str, *, skip_user_id: int | None = None
) -> None:
    """Email every officer except `skip_user_id`.

    Call after the commit: a mail outage never fails the
    request and never leaves a half-saved change. SMTP goes out on a background
    thread so a slow mail server cannot hold a worker (same reason as
    `mail.send_safely`); the log backend sends inline."""
    try:
        recipients = [
            (user.id, user.email)
            for user in officers(sacco_id)
            if user.id != skip_user_id
        ]
    except Exception:
        log.exception("Could not list the officers of SACCO %s", sacco_id)
        return
    if current_app.config["MAIL_BACKEND"] != "smtp":
        _send_all(sacco_id, recipients, subject, body)
        return
    app = current_app._get_current_object()

    def work():
        with app.app_context():
            _send_all(sacco_id, recipients, subject, body)

    threading.Thread(target=work, daemon=True).start()


def officer_ids(sacco_id: int) -> set[int]:
    """Who holds `payin.manage` now; take one before a role change and compare."""
    return set(holders(sacco_id, PERMISSION))


def announce_new_officers(sacco_id: int, before: set[int], actor_email: str) -> None:
    """Tell every officer when someone gains the power to propose or approve.

    A second officer is the control on pay-in details, so anyone who can make a
    second officer (add a member, set titles, change a role) must be visible.
    Call after the commit, with `officer_ids()` taken before the change.
    """
    try:
        gained = officer_ids(sacco_id) - before
        if not gained:
            return
        names = ", ".join(
            sorted(
                db.session.execute(
                    select(User.email).where(User.id.in_(gained))
                ).scalars()
            )
        )
        sacco_name = db.session.get(Sacco, sacco_id).name
    except Exception:
        log.exception("Could not announce new officers of SACCO %s", sacco_id)
        return
    notify_officers(
        sacco_id,
        "NEXORA: a new officer can change pay-in details",
        f"SACCO: {sacco_name}\nNew officer: {names}\nGiven by: {actor_email}\n\n"
        "This person can now propose and approve changes to where members pay "
        "in. If you did not expect this, tell your SACCO admin straight away.",
    )


def too_many_proposals(sacco_id: int) -> bool:
    """A SACCO may send a handful of proposals an hour: each one emails every
    officer, and a hostile officer could otherwise bury the honest ones."""
    since = datetime.now(UTC) - timedelta(hours=1)
    recent = db.session.execute(
        select(func.count())
        .select_from(SaccoPaymentDetailsChange)
        .where(
            SaccoPaymentDetailsChange.sacco_id == sacco_id,
            SaccoPaymentDetailsChange.proposed_at > since,
        )
    ).scalar_one()
    return recent >= MAX_PROPOSALS_PER_HOUR


def go_live(change: SaccoPaymentDetailsChange) -> None:
    """Copy an approved change onto the SACCO's live row (insert or replace)."""
    values = {field: getattr(change, field) for field in FIELDS}
    row = {
        "updated_by": change.proposed_by,
        "updated_at": change.go_live_at,
        **values,
    }
    db.session.execute(
        pg_insert(SaccoPaymentDetails)
        .values(sacco_id=change.sacco_id, **row)
        .on_conflict_do_update(index_elements=["sacco_id"], set_=row)
    )
