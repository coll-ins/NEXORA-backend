"""Join, set PIN, SACCO login. Section 2."""

import hmac
from datetime import UTC, datetime

from flask import current_app, g, jsonify, request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import auth_bp
from app.auth.join import (
    MAX_ATTEMPTS,
    JoinError,
    burn_codes,
    check_code,
    digest_code,
    normalize_phone,
)
from app.auth.limits import read_limit, write_limit
from app.auth.pin import PinError, check_pin, set_pin
from app.auth.suspension import SUSPENDED_MESSAGE
from app.errors import CODE_WRONG, RULES_NOT_ACCEPTED, error_response, interface_error
from app.extensions import db
from app.models.audit import AuditLog
from app.models.identity import JoinCode, UserPhone
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.sacco import Sacco, SaccoStatus
from app.saccos import rules as sacco_rules
from app.savings.validation import MAX_ID
from app.security.tenant import AccessDenied, current_user

SETUP_TTL = 600  # seconds the setup token stays valid
DECLINE_REASON_MAX = 500


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="join-setup")


def _audit(membership, action, note):
    db.session.add(
        AuditLog(
            sacco_id=membership.sacco_id,
            actor_id=g.user.id,
            action=action,
            target=f"membership:{membership.id} {note}",
        )
    )


def _sacco_suspended(sacco_id: int):
    """The 423 answer if this SACCO is suspended, else None."""
    sacco = db.session.get(Sacco, sacco_id)
    if sacco is not None and sacco.status is SaccoStatus.SUSPENDED:
        return error_response(423, SUSPENDED_MESSAGE)
    return None


def _signed_in():
    try:
        g.user = current_user()
    except AccessDenied as exc:
        return error_response(exc.status, exc.message)
    return None


def _body() -> dict:
    """The JSON object sent, or {} for anything else (a list, a string, no body)."""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@auth_bp.post("/join")
@write_limit
def join():
    if error := _signed_in():
        return error
    body = _body()
    # No phone means the invitation was made by email: it belongs to the signed-in
    # account itself, so the account is who it is looked up by.
    by_account = str(body.get("phone") or "").strip() == ""
    phone = None
    if not by_account:
        try:
            phone = normalize_phone(body.get("phone"))
        except ValueError:
            return interface_error(CODE_WRONG, "That code is not correct.")
    code = str(body.get("code", ""))
    # Every live invitation for this phone (or account), one per SACCO, locked in
    # id order. The code's digest is tied to its membership, so the code itself
    # says which SACCO is being joined. An unknown phone answers like a wrong code.
    holder = (
        SaccoMembership.user_id == g.user.id
        if by_account
        else SaccoMembership.phone == phone
    )
    invited = (
        db.session.execute(
            select(SaccoMembership)
            .where(
                holder,
                # Only a live invitation can be joined: a cancelled or finished
                # membership answers like an unknown phone.
                SaccoMembership.status == MembershipStatus.INVITED,
            )
            .order_by(SaccoMembership.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    latest = []
    for candidate in invited:
        code_row = db.session.execute(
            select(JoinCode)
            .where(JoinCode.membership_id == candidate.id)
            .order_by(JoinCode.id.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        if code_row is not None:
            latest.append((candidate, code_row))
    secret = current_app.config["SECRET_KEY"]
    match = next(
        (
            (candidate, code_row)
            for candidate, code_row in latest
            if hmac.compare_digest(
                code_row.code_digest, digest_code(candidate.id, code, secret)
            )
        ),
        None,
    )
    if match is None:
        # A wrong guess counts against every live code for this phone, so two
        # invitations never give an extra set of guesses.
        for _, code_row in latest:
            if code_row.used_at is None:
                code_row.attempts = min(code_row.attempts + 1, MAX_ATTEMPTS)
        db.session.commit()
        return interface_error(CODE_WRONG, "That code is not correct.")
    membership, row = match
    if (sacco_error := _sacco_suspended(membership.sacco_id)) is not None:
        db.session.rollback()
        return sacco_error
    try:
        check_code(row, code, secret)  # used, burned or expired
    except JoinError as exc:
        db.session.commit()
        return interface_error(exc.name, exc.message)

    user = g.user
    if phone is not None:
        existing = db.session.execute(
            select(UserPhone).where(UserPhone.phone == phone)
        ).scalar_one_or_none()
        if existing is not None and existing.user_id != user.id:
            db.session.rollback()  # code stays unspent
            return error_response(409, "That phone belongs to another account.")
        if existing is None:
            db.session.add(
                UserPhone(user_id=user.id, phone=phone, verified_at=datetime.now(UTC))
            )
    membership.user_id = user.id
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return error_response(409, "You are already a member of this SACCO.")
    _audit(membership, "member.join", "invited -> linked")
    db.session.commit()
    # The token names the code it came from: a re-invite issues a newer code,
    # so a token from before the cancel can no longer finish the join.
    token = _serializer().dumps({"m": membership.id, "u": user.id, "c": row.id})
    return jsonify(setup_token=token, membership_id=membership.id)


@auth_bp.post("/join/pin")
@write_limit
def join_pin():
    if error := _signed_in():
        return error
    body = _body()
    try:
        data = _serializer().loads(str(body.get("setup_token", "")), max_age=SETUP_TTL)
    except BadSignature:
        return error_response(401, "The setup token is invalid or expired.")
    membership = db.session.get(SaccoMembership, data["m"], with_for_update=True)
    if membership is None or membership.user_id != g.user.id or data["u"] != g.user.id:
        db.session.rollback()
        return error_response(403, "Not allowed.")
    if membership.status is not MembershipStatus.INVITED:
        db.session.rollback()
        return error_response(409, "This membership is already set up.")
    if (sacco_error := _sacco_suspended(membership.sacco_id)) is not None:
        db.session.rollback()
        return sacco_error
    newest = db.session.execute(
        select(JoinCode)
        .where(JoinCode.membership_id == membership.id)
        .order_by(JoinCode.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if newest is None or newest.id != data.get("c") or newest.used_at is None:
        db.session.rollback()
        return error_response(
            401, "The setup token is invalid or expired. Join again with your code."
        )
    rules = sacco_rules.current_rules(membership.sacco_id)
    if rules is not None and not sacco_rules.has_accepted(membership.id, rules.id):
        db.session.rollback()
        return interface_error(
            RULES_NOT_ACCEPTED, "Read and accept the SACCO's rules before joining."
        )
    try:
        set_pin(membership, str(body.get("pin", "")))
    except ValueError as exc:
        db.session.rollback()
        return error_response(400, str(exc))
    membership.status = MembershipStatus.ACTIVE
    _audit(membership, "member.activate", "invited -> active")
    db.session.commit()
    return jsonify(membership_id=membership.id, status="active")


@auth_bp.post("/sacco-login")
@write_limit
def sacco_login():
    if error := _signed_in():
        return error
    body = _body()
    try:
        sacco_id = int(body.get("sacco_id"))
    except (TypeError, ValueError):
        return error_response(400, "sacco_id is required.")
    if not 1 <= sacco_id <= MAX_ID:
        return error_response(400, "sacco_id is required.")
    membership = db.session.execute(
        select(SaccoMembership)
        .where(
            SaccoMembership.user_id == g.user.id, SaccoMembership.sacco_id == sacco_id
        )
        .with_for_update()
    ).scalar_one_or_none()
    if membership is None or membership.status is not MembershipStatus.ACTIVE:
        db.session.rollback()
        return error_response(403, "Not allowed.")
    if (sacco_error := _sacco_suspended(membership.sacco_id)) is not None:
        db.session.rollback()
        return sacco_error
    try:
        check_pin(membership, str(body.get("pin", "")))
    except PinError as exc:
        # Refused attempts are recorded too, with the reason.
        _audit(membership, "member.sacco_login", f"refused: {exc.name}")
        db.session.commit()  # keep the wrong-attempt count
        return interface_error(exc.name, exc.message)
    _audit(membership, "member.sacco_login", "ok")
    db.session.commit()
    return jsonify(ok=True, membership_id=membership.id, role=membership.role.value)


def _joining_membership(body):
    """The invited membership a setup token names, or (None, error response)."""
    try:
        data = _serializer().loads(str(body.get("setup_token", "")), max_age=SETUP_TTL)
    except BadSignature:
        return None, error_response(401, "The setup token is invalid or expired.")
    membership = db.session.get(SaccoMembership, data["m"])
    if (
        membership is None
        or membership.user_id != g.user.id
        or data["u"] != g.user.id
        or membership.status is not MembershipStatus.INVITED
    ):
        return None, error_response(403, "Not allowed.")
    return membership, None


@auth_bp.post("/join/rules")
@read_limit
def join_rules():
    """The rules of the SACCO being joined, for the screen before the PIN."""
    if error := _signed_in():
        return error
    membership, error = _joining_membership(_body())
    if error:
        return error
    rules = sacco_rules.current_rules(membership.sacco_id)
    accepted = sacco_rules.has_accepted(membership.id, rules.id) if rules else None
    # The invitation, not the SACCO the reader is currently in: an officer who is
    # signed into one SACCO may be joining another, and the screen must name the
    # one the rules belong to.
    sacco = db.session.get(Sacco, membership.sacco_id)
    return jsonify(
        rules=sacco_rules.rules_json(rules, accepted),
        sacco={"id": sacco.id, "name": sacco.name} if sacco else None,
    )


@auth_bp.post("/join/rules/accept")
@write_limit
def join_accept_rules():
    if error := _signed_in():
        return error
    body = _body()
    membership, error = _joining_membership(body)
    if error:
        return error
    rules = sacco_rules.current_rules(membership.sacco_id)
    if rules is None:
        return error_response(404, "This SACCO has no rules to accept.")
    version = body.get("version")
    if type(version) is not int or version != rules.version:
        return error_response(409, "The rules changed. Read the current version first.")
    sacco_rules.accept(membership.id, rules.id)
    _audit(membership, "rules.accept", f"rules:{rules.id} v{rules.version}")
    db.session.commit()
    return jsonify(rules=sacco_rules.rules_json(rules, True))


@auth_bp.post("/join/rules/decline")
@write_limit
def join_decline_rules():
    """Refuse the invitation after reading the rules, saying why.

    The reason is required and is kept for the SACCO's officers. The membership
    never becomes active and its join code is spent, so declining also ends the
    invitation; an officer who invites again starts a fresh one.
    """
    if error := _signed_in():
        return error
    body = _body()
    membership, error = _joining_membership(body)
    if error:
        return error
    raw = body.get("reason")
    reason = raw.strip() if isinstance(raw, str) else ""
    if not reason:
        return error_response(
            400, "Say why you are declining. The SACCO's officers read your reason."
        )
    if len(reason) > DECLINE_REASON_MAX:
        return error_response(
            400, f"Keep the reason to {DECLINE_REASON_MAX} characters or fewer."
        )
    rules = sacco_rules.current_rules(membership.sacco_id)
    if rules is not None:
        version = body.get("version")
        if type(version) is not int or version != rules.version:
            return error_response(
                409, "The rules changed. Read the current version first."
            )
    burn_codes(membership.id)
    membership.status = MembershipStatus.DECLINED
    membership.declined_at = datetime.now(UTC)
    membership.decline_reason = reason
    _audit(membership, "member.decline", f"declined: {reason[:200]}")
    db.session.commit()
    return jsonify(membership_id=membership.id, status="declined")
