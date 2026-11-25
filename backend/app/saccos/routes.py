"""SACCO endpoints.

Create, list, activate and deactivate: platform owner only.
Get, rename and record the registration details of one SACCO: the platform
owner, or that SACCO's own admin.
Every change writes an audit row in the same transaction as the change.
"""

import re
import unicodedata

from flask import g, jsonify
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import saccos_bp
from app.auth.guards import platform_owner_required
from app.auth.limits import read_limit, write_limit
from app.auth.routes import _body, _text
from app.errors import error_response
from app.extensions import db
from app.models.audit import AuditLog
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.sacco import Sacco, SaccoStatus
from app.saccos import registration
from app.security.tenant import permission_scoped

MAX_NAME_LENGTH = 255
CURRENCY_RE = re.compile(r"[A-Z]{3}")
NAME_RULE = (
    f"A name is required (max {MAX_NAME_LENGTH} characters, "
    "no control or invisible characters)."
)
DUPLICATE_NAME = "A SACCO with this name already exists."
DUPLICATE_REGISTRATION = (
    "Another SACCO already has this registration number or SASRA licence number."
)
EDITABLE = {"name", *registration.FIELDS}


def _clean_name(value) -> str | None:
    """The trimmed name, or None if it is blank, too long, or hides characters.

    Control and zero-width characters (Unicode "C*") would let a name look blank
    or look the same as another SACCO's name.
    """
    name = (_text(value) or "").strip()
    hidden = any(unicodedata.category(ch).startswith("C") for ch in name)
    return name if name and len(name) <= MAX_NAME_LENGTH and not hidden else None


def _audit(sacco: Sacco, actor_id: int, action: str, target: str | None = None):
    db.session.add(
        AuditLog(
            sacco_id=sacco.id,
            actor_id=actor_id,
            action=action,
            target=(target or f"sacco:{sacco.id}")[:255],
        )
    )


@saccos_bp.after_request
def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def _member_count(sacco_id: int) -> int:
    """How big the SACCO is. Active members only: an invitation is not a member.

    The platform area shows this number and it is a count, never money.
    """
    return db.session.execute(
        select(func.count())
        .select_from(SaccoMembership)
        .where(
            SaccoMembership.sacco_id == sacco_id,
            SaccoMembership.status == MembershipStatus.ACTIVE,
        )
    ).scalar_one()


def _invalid_registration(errors: dict):
    return (
        jsonify(
            error={
                "code": "validation_error",
                "message": "Some registration details are not valid. See fields.",
                "fields": errors,
            }
        ),
        400,
    )


def _sacco_json(sacco: Sacco) -> dict:
    return {
        "id": sacco.id,
        "name": sacco.name,
        "currency": sacco.currency,
        "status": sacco.status.value,
        "member_count": _member_count(sacco.id),
        "created_at": sacco.created_at.isoformat(),
        "registration_number": sacco.registration_number,
        "sasra_licence_number": sacco.sasra_licence_number,
        "registration_date": (
            sacco.registration_date.isoformat() if sacco.registration_date else None
        ),
    }


@saccos_bp.post("")
@write_limit
@platform_owner_required
def create_sacco():
    data = _body()
    name = _clean_name(data.get("name"))
    currency = (_text(data.get("currency", "KES")) or "").strip().upper()
    if name is None:
        return error_response(400, NAME_RULE)
    if not CURRENCY_RE.fullmatch(currency):
        return error_response(400, "Currency must be a 3-letter code such as KES.")

    reg_values, reg_errors = registration.validate(data)
    if reg_errors:
        return _invalid_registration(reg_errors)

    rule_values = None
    if "rules" in data:
        from app.saccos import rules as sacco_rules
        from app.saccos.rules_routes import invalid

        rule_values, rule_errors = sacco_rules.validate_rules(data["rules"])
        if rule_errors:
            return invalid(rule_errors)

    sacco = Sacco(name=name, currency=currency, **reg_values)
    db.session.add(sacco)
    try:
        db.session.flush()  # assigns the id the audit row points at
    except IntegrityError as exc:
        db.session.rollback()
        if "uq_saccos_name_lower" in str(exc.orig):
            return error_response(409, DUPLICATE_NAME)
        return error_response(409, DUPLICATE_REGISTRATION)
    _audit(sacco, g.user.id, "sacco.create")
    if rule_values is not None:
        row = sacco_rules.publish_rules(sacco.id, rule_values, g.user.id)
        _audit(sacco, g.user.id, "rules.publish", f"rules:{row.id} v{row.version}")
    db.session.commit()
    return jsonify(_sacco_json(sacco)), 201


@saccos_bp.get("")
@read_limit
@platform_owner_required
def list_saccos():
    saccos = db.session.query(Sacco).order_by(Sacco.id).all()
    return jsonify(saccos=[_sacco_json(s) for s in saccos])


def _set_status(sacco_id: int, status: SaccoStatus, action: str):
    """Idempotent: setting the current status again changes and logs nothing."""
    sacco = db.session.execute(
        select(Sacco).where(Sacco.id == sacco_id).with_for_update()
    ).scalar_one_or_none()
    if sacco is None:
        db.session.rollback()
        return error_response(404, "Not found.")
    if sacco.status is not status:
        sacco.status = status
        _audit(sacco, g.user.id, action)
    body = _sacco_json(sacco)
    db.session.commit()
    return jsonify(body)


@saccos_bp.post("/<int:sacco_id>/activate")
@write_limit
@platform_owner_required
def activate_sacco(sacco_id):
    return _set_status(sacco_id, SaccoStatus.ACTIVE, "sacco.activate")


@saccos_bp.post("/<int:sacco_id>/deactivate")
@write_limit
@platform_owner_required
def deactivate_sacco(sacco_id):
    return _set_status(sacco_id, SaccoStatus.SUSPENDED, "sacco.deactivate")


@saccos_bp.get("/<int:sacco_id>")
@read_limit
@permission_scoped("sacco.view")
def get_sacco(sacco_id):
    return jsonify(_sacco_json(g.access.sacco))


@saccos_bp.patch("/<int:sacco_id>")
@write_limit
@permission_scoped("sacco.update")
def update_sacco(sacco_id):
    """Rename the SACCO and record its registration details.

    Editable: `name`, `registration_number`, `sasra_licence_number` and
    `registration_date` (null or "" clears a registration field). Status is the
    platform owner's call (activate/deactivate), and the currency must not
    change once money has been posted in it.
    """
    data = _body()
    if not data or not set(data) <= EDITABLE:
        return error_response(
            400,
            "Send only name, registration_number, sasra_licence_number "
            "or registration_date.",
        )
    name = None
    if "name" in data:
        name = _clean_name(data["name"])
        if name is None:
            return error_response(400, NAME_RULE)
    reg_values, reg_errors = registration.validate(data)
    if reg_errors:
        return _invalid_registration(reg_errors)

    # Lock the row so two edits cannot overlap: the second waits, then sees the
    # first one's values, so each audit row records what it really replaced.
    sacco = db.session.execute(
        select(Sacco)
        .where(Sacco.id == sacco_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    renamed = name is not None and name != sacco.name
    changed = [f for f, v in reg_values.items() if getattr(sacco, f) != v]
    if not renamed and not changed:
        body = _sacco_json(sacco)
        db.session.rollback()  # nothing to write; release the lock
        return jsonify(body)

    if renamed:
        old = f"sacco:{sacco.id} from {sacco.name!r}"
        _audit(sacco, g.access.user.id, "sacco.rename", old)
        sacco.name = name
    if changed:
        # Field names only: the audit trail says what changed, not the numbers.
        _audit(
            sacco,
            g.access.user.id,
            "sacco.registration_update",
            f"sacco:{sacco.id} fields:{','.join(changed)}",
        )
        for field in changed:
            setattr(sacco, field, reg_values[field])
    try:
        db.session.commit()  # the edit and its audit rows, or neither
    except IntegrityError as exc:
        db.session.rollback()
        if "uq_saccos_name_lower" in str(exc.orig):
            return error_response(409, DUPLICATE_NAME)
        return error_response(409, DUPLICATE_REGISTRATION)
    return jsonify(_sacco_json(sacco))
