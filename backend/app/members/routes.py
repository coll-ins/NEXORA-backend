"""Member routes.

Two groups live here because they both serve members: the signed-in user's own
profile under /members/me (Sam's Move 2 work), and one SACCO's member list
under /saccos/<sacco_id>/members (Neo's Move 2 work).
"""

from datetime import timedelta

from flask import Response, current_app, g, jsonify, request
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app import members_bp, saccos_bp
from app.auth.join import burn_codes, issue_code, normalize_phone
from app.auth.limits import read_limit, write_limit
from app.auth.routes import _body
from app.errors import error_response
from app.extensions import db
from app.mail import send_join_code_safely
from app.members import self_service
from app.members.schemas import (
    InvalidMember,
    parse_create_member,
    parse_email_invite,
    parse_invite_role,
    parse_update_member,
)
from app.models.access import MembershipRoleLink, Role
from app.models.audit import AuditLog
from app.models.loan import SaccoSettings
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.sacco import Sacco
from app.models.user import User
from app.saccos import payment_details as payin
from app.security.permissions import (
    LEGACY_ROLE_CODES,
    OWNER_PLATFORM,
    _role_codes,
    can,
    is_admin_membership,
    is_admin_role,
    is_platform_owner,
    permissions_for,
    role_codes_by_membership,
)
from app.security.tenant import (
    AccessDenied,
    current_user,
    permission_scoped,
)


def _me_error(message: str, status: int):
    return error_response(status, message)


def _me_user() -> tuple[User | None, tuple | None]:
    """The signed-in caller for /me, or (None, error response).

    401 for no or a bad token. The profile is the user's own, not a SACCO's, so a
    suspended SACCO does not block it (the SACCO's own endpoints answer 423).
    """
    try:
        user = current_user()
    except AccessDenied as exc:
        return None, error_response(exc.status, exc.message)
    return user, None


@members_bp.after_request
def _me_no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@members_bp.get("/me")
@read_limit
def get_me():
    user, error = _me_user()
    if error:
        return error
    try:
        return jsonify(self_service.get_profile(user.id))
    except self_service.ProfileError as exc:
        return error_response(exc.status, exc.message)


@members_bp.get("/me/saccos")
@read_limit
def my_saccos():
    """Every SACCO the caller belongs to, with its status and the caller's roles.

    One login covers all of them: a SACCO-scoped request names its SACCO, and
    the membership is checked again on every request. A suspended SACCO is
    listed with status "suspended" (its own endpoints answer 423).
    """
    try:
        user = current_user()
    except AccessDenied as exc:
        return error_response(exc.status, exc.message)
    rows = db.session.execute(
        select(SaccoMembership, Sacco)
        .join(Sacco, Sacco.id == SaccoMembership.sacco_id)
        .where(SaccoMembership.user_id == user.id)
        .order_by(Sacco.name, Sacco.id)
    ).all()
    roles = role_codes_by_membership([m for m, _ in rows])
    return jsonify(
        saccos=[
            {
                "id": sacco.id,
                "name": sacco.name,
                "status": sacco.status.value,
                "currency": sacco.currency,
                "membership_id": membership.id,
                "membership_status": membership.status.value,
                "roles": sorted(roles[membership.id]),
            }
            for membership, sacco in rows
        ]
    )


@members_bp.patch("/me")
@write_limit
def patch_me():
    user, error = _me_user()
    if error:
        return error
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error_response(400, "A JSON object is required.")
    try:
        return jsonify(self_service.update_profile(user.id, body))
    except self_service.ProfileError as exc:
        return error_response(exc.status, exc.message)


_PROFILE_PHOTO_MAX_BYTES = 1_500_000
_PROFILE_PHOTO_TYPES = {
    "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/webp": lambda data: (
        len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    ),
}


@members_bp.get("/me/photo")
@read_limit
def get_me_photo():
    user, error = _me_user()
    if error:
        return error
    if not user.profile_photo or not user.profile_photo_content_type:
        return "", 204
    return Response(
        user.profile_photo,
        mimetype=user.profile_photo_content_type,
        headers={"Cache-Control": "private, no-store"},
    )


@members_bp.put("/me/photo")
@write_limit
def put_me_photo():
    user, error = _me_user()
    if error:
        return error
    # This endpoint accepts a larger multipart body than the app's small JSON
    # default while still rejecting oversized uploads before parsing them.
    request.max_content_length = _PROFILE_PHOTO_MAX_BYTES + 64 * 1024
    uploaded = request.files.get("photo")
    if uploaded is None:
        return error_response(400, "Choose a JPEG, PNG or WebP photo.")
    data = uploaded.stream.read(_PROFILE_PHOTO_MAX_BYTES + 1)
    if not data:
        return error_response(400, "The selected photo is empty.")
    if len(data) > _PROFILE_PHOTO_MAX_BYTES:
        return error_response(413, "Choose a photo smaller than 1.5 MB.")
    content_type = next(
        (mime for mime, matches in _PROFILE_PHOTO_TYPES.items() if matches(data)),
        None,
    )
    if content_type is None:
        return error_response(400, "Choose a valid JPEG, PNG or WebP photo.")

    user.profile_photo = data
    user.profile_photo_content_type = content_type
    db.session.commit()
    return jsonify(saved=True)


@members_bp.delete("/me/photo")
@write_limit
def delete_me_photo():
    user, error = _me_user()
    if error:
        return error
    user.profile_photo = None
    user.profile_photo_content_type = None
    db.session.commit()
    return jsonify(deleted=True)


@members_bp.get("/me/permissions")
@read_limit
def my_permissions():
    """The caller's job titles and permissions in one SACCO.

    The screens decide by permission name, never by role name, so this is where
    the names come from. With no `sacco_id`, a platform owner gets the platform
    permissions; everyone else must name a SACCO (400). Not being in the SACCO
    is 404, like every other tenant check.
    """
    user, error = _me_user()
    if error:
        return error
    raw = request.args.get("sacco_id")
    if raw is None:
        if not is_platform_owner(user):
            return error_response(400, "sacco_id is required.")
        return jsonify(roles=[], permissions=sorted(OWNER_PLATFORM))
    try:
        sacco_id = int(raw)
    except (TypeError, ValueError):
        return error_response(400, "sacco_id must be a whole number.")
    sacco = db.session.get(Sacco, sacco_id)
    if sacco is None:
        return error_response(404, "Not found.")
    membership = db.session.execute(
        select(SaccoMembership).where(
            SaccoMembership.user_id == user.id, SaccoMembership.sacco_id == sacco_id
        )
    ).scalar_one_or_none()
    if membership is None and not is_platform_owner(user):
        return error_response(404, "Not found.")
    return jsonify(
        roles=sorted(_role_codes(membership)) if membership is not None else [],
        permissions=sorted(permissions_for(user, sacco)),
    )


# ---------------------------------------------------------------------------
# One SACCO's members (Move 2, Neo). Platform owner or that SACCO's admin only.
# The tenant guard answers 401/404/403/423, so another SACCO is never revealed.
# Only the platform owner creates, edits or removes administrator memberships.
# ---------------------------------------------------------------------------


def _member_json(
    membership: SaccoMembership, email: str | None, roles: set[str] | None = None
) -> dict:
    if roles is None:
        roles = _role_codes(membership)
    return {
        "id": membership.id,
        "user_id": membership.user_id,
        "email": email,
        "phone": membership.phone,
        "role": membership.role.value,
        "roles": sorted(roles),
        "status": membership.status.value,
        "created_at": membership.created_at.isoformat(),
        # Why a refused invitation was refused, in the person's own words.
        "declined_at": membership.declined_at.isoformat()
        if membership.declined_at
        else None,
        "decline_reason": membership.decline_reason,
    }


def _state(membership: SaccoMembership) -> str:
    return f"{membership.role.value}/{membership.status.value}"


def _audit(membership: SaccoMembership, action: str, before: str = "") -> None:
    # Same transaction as the change: both are saved or neither is.
    change = f"{before} -> {_state(membership)}" if before else _state(membership)
    db.session.add(
        AuditLog(
            sacco_id=membership.sacco_id,
            actor_id=g.access.user.id,
            action=action,
            target=f"membership:{membership.id} {change}",
        )
    )


def _members(sacco_id: int, membership_id: int | None = None, lock: bool = False):
    """(membership, email) rows in this SACCO only, so other SACCOs never leak.

    Invited members have no user yet, so the join to users is an outer join.
    """
    query = (
        select(SaccoMembership, User.email)
        .outerjoin(User, User.id == SaccoMembership.user_id)
        .where(SaccoMembership.sacco_id == sacco_id)
        .order_by(SaccoMembership.id)
    )
    if membership_id is not None:
        query = query.where(SaccoMembership.id == membership_id)
    if lock:
        query = query.with_for_update(of=SaccoMembership)
    return db.session.execute(query).all()


@saccos_bp.get("/<int:sacco_id>/members")
@read_limit
@permission_scoped("member.view")
def list_members(sacco_id):
    rows = _members(sacco_id)
    roles = role_codes_by_membership([m for m, _ in rows])
    return jsonify(members=[_member_json(m, e, roles[m.id]) for m, e in rows])


@saccos_bp.get("/<int:sacco_id>/members/<int:membership_id>")
@read_limit
@permission_scoped("member.view")
def get_member(sacco_id, membership_id):
    rows = _members(sacco_id, membership_id)
    if not rows:
        return error_response(404, "Member not found.")
    return jsonify(_member_json(*rows[0]))


def _code_ttl(sacco_id: int) -> timedelta:
    """How long a join code lives: the SACCO's setting, 15 minutes by default."""
    minutes = db.session.execute(
        select(SaccoSettings.join_code_ttl_minutes).where(
            SaccoSettings.sacco_id == sacco_id
        )
    ).scalar_one_or_none()
    return timedelta(minutes=minutes or 15)


def _invite_member(sacco_id):
    """An officer adds a phone: invited membership plus a one-time join code."""
    body = request.get_json(silent=True) or {}
    try:
        phone = normalize_phone(body.get("phone"))
        role, title = parse_invite_role(body.get("role", "member"))
    except (TypeError, ValueError, InvalidMember):
        return error_response(400, "A valid phone and role are required.")
    if is_admin_role(role) and not can(g.access.user, sacco_id, "admin.manage"):
        return error_response(
            403, "Only the platform owner can add a SACCO administrator."
        )
    if not is_admin_role(role) and not can(g.access.user, sacco_id, "member.manage"):
        return error_response(
            403, "Adding or changing members needs member.manage (the SACCO admin)."
        )
    membership = db.session.execute(
        select(SaccoMembership)
        .where(SaccoMembership.sacco_id == sacco_id, SaccoMembership.phone == phone)
        .with_for_update()
    ).scalar_one_or_none()
    reissued = membership is not None
    if reissued:
        if membership.status not in (
            MembershipStatus.INVITED,
            MembershipStatus.DECLINED,
        ):
            db.session.rollback()
            return error_response(409, "That phone already belongs to this SACCO.")
        # Inviting a person who declined starts over: the reason is history now.
        membership.declined_at = membership.decline_reason = None
        membership.status = MembershipStatus.INVITED
        if is_admin_role(membership.role) and not can(
            g.access.user, sacco_id, "admin.manage"
        ):
            db.session.rollback()
            return error_response(
                403, "Only the platform owner can change a SACCO administrator invite."
            )
        old_title_codes = _role_codes(membership)
        membership.role = role
        if old_title_codes != {title}:
            db.session.execute(
                delete(MembershipRoleLink).where(
                    MembershipRoleLink.membership_id == membership.id
                )
            )
            _grant_default_title(membership, title)
    else:
        membership = SaccoMembership(
            user_id=None,
            sacco_id=sacco_id,
            phone=phone,
            role=role,
            status=MembershipStatus.INVITED,
        )
        db.session.add(membership)
        try:
            db.session.flush()
        except IntegrityError:  # another request invited this phone at the same time
            db.session.rollback()
            return error_response(
                409, "That phone has already been invited to this SACCO."
            )
    row, code = issue_code(
        membership.id,
        g.access.user.id,
        current_app.config["SECRET_KEY"],
        ttl=_code_ttl(sacco_id),
    )
    if not reissued:
        _grant_default_title(membership, title)
    _audit(membership, "member.invite_code_reissued" if reissued else "member.invite")
    db.session.commit()
    return (
        jsonify(
            id=membership.id,
            phone=phone,
            role=role.value,
            title=title,
            status="invited",
            join_code=code,
            expires_at=row.expires_at.isoformat(),
        ),
        200 if reissued else 201,
    )


def _invite_member_by_email(sacco_id):
    """An officer adds an existing, verified account by email: an invited
    membership plus a one-time join code that is emailed to that address.

    The code is never in the response, so the officer cannot use it; only the
    owner of the mailbox can finish the join (code, rules, PIN).
    """
    try:
        email, role, title = parse_email_invite(_body())
    except InvalidMember as exc:
        return error_response(400, str(exc))
    if is_admin_role(role) and not can(g.access.user, sacco_id, "admin.manage"):
        return error_response(
            403, "Only the platform owner can add a SACCO administrator."
        )
    if not is_admin_role(role) and not can(g.access.user, sacco_id, "member.manage"):
        return error_response(
            403, "Adding or changing members needs member.manage (the SACCO admin)."
        )
    target = db.session.execute(
        select(User).where(User.email == email)
    ).scalar_one_or_none()
    # Same answers as adding an email member: the platform owner looks unknown,
    # and only an address whose owner opened the emailed link can be invited.
    if target is None or is_platform_owner(target):
        return error_response(404, "No registered user with that email.")
    if target.email_verified_at is None:
        return error_response(409, "That user has not verified their email yet.")
    membership = db.session.execute(
        select(SaccoMembership)
        .where(
            SaccoMembership.sacco_id == sacco_id, SaccoMembership.user_id == target.id
        )
        .with_for_update()
    ).scalar_one_or_none()
    reissued = membership is not None
    if reissued:
        if membership.status not in (
            MembershipStatus.INVITED,
            MembershipStatus.DECLINED,
        ):
            db.session.rollback()
            return error_response(409, "That user is already a member of this SACCO.")
        # Inviting a person who declined starts over: the reason is history now.
        membership.declined_at = membership.decline_reason = None
        membership.status = MembershipStatus.INVITED
        if is_admin_role(membership.role) and not can(
            g.access.user, sacco_id, "admin.manage"
        ):
            db.session.rollback()
            return error_response(
                403, "Only the platform owner can change a SACCO administrator invite."
            )
        old_title_codes = _role_codes(membership)
        membership.role = role
        if old_title_codes != {title}:
            db.session.execute(
                delete(MembershipRoleLink).where(
                    MembershipRoleLink.membership_id == membership.id
                )
            )
            _grant_default_title(membership, title)
    else:
        membership = SaccoMembership(
            user_id=target.id,
            sacco_id=sacco_id,
            role=role,
            status=MembershipStatus.INVITED,
        )
        db.session.add(membership)
        try:
            db.session.flush()
        except IntegrityError:  # another request invited this user at the same time
            db.session.rollback()
            return error_response(
                409, "That user has already been invited to this SACCO."
            )
    ttl = _code_ttl(sacco_id)
    row, code = issue_code(
        membership.id,
        g.access.user.id,
        current_app.config["SECRET_KEY"],
        ttl=ttl,
    )
    if not reissued:
        _grant_default_title(membership, title)
    _audit(
        membership,
        "member.invite_email_code_reissued" if reissued else "member.invite_email",
    )
    sacco_name = db.session.get(Sacco, sacco_id).name
    body = dict(
        id=membership.id,
        email=target.email,
        role=role.value,
        title=title,
        status="invited",
        expires_at=row.expires_at.isoformat(),
    )
    db.session.commit()
    send_join_code_safely(
        target.email, code, sacco_name, max(1, int(ttl.total_seconds() // 60))
    )
    return jsonify(body), 200 if reissued else 201


@saccos_bp.post("/<int:sacco_id>/members")
@write_limit
@permission_scoped("member.manage", "admin.manage")
def add_member(sacco_id):
    raw = request.get_json(silent=True)
    if isinstance(raw, dict) and raw.get("phone") and not raw.get("email"):
        return _invite_member(sacco_id)
    if isinstance(raw, dict) and raw.get("email") and raw.get("invite") is True:
        return _invite_member_by_email(sacco_id)
    owner = can(g.access.user, sacco_id, "admin.manage")
    try:
        email, role, status = parse_create_member(_body())
    except InvalidMember as exc:
        return error_response(400, str(exc))
    if status is MembershipStatus.INVITED:
        # An invitation is a phone plus a join code (send "phone"); an email
        # member is added active or inactive.
        return error_response(400, "Invite by phone; add an email member as active.")
    if is_admin_role(role) and not owner:
        return error_response(
            403, "Only the platform owner can add a SACCO administrator."
        )
    if not is_admin_role(role) and not can(g.access.user, sacco_id, "member.manage"):
        return error_response(
            403, "Adding or changing members needs member.manage (the SACCO admin)."
        )
    target = db.session.execute(
        select(User).where(User.email == email)
    ).scalar_one_or_none()
    # The platform owner gets the same answer as an unknown email, so an admin
    # cannot discover the owner's address. The owner never needs a membership.
    if target is None or is_platform_owner(target):
        return error_response(404, "No registered user with that email.")
    # Whoever registered an address may not be its owner. Only a user who has
    # opened the emailed link can be given access to a SACCO.
    if target.email_verified_at is None:
        return error_response(409, "That user has not verified their email yet.")
    officers_before = payin.officer_ids(sacco_id)
    membership = SaccoMembership(
        user_id=target.id, sacco_id=sacco_id, role=role, status=status
    )
    db.session.add(membership)
    try:
        db.session.flush()  # assigns the id the audit row points at
    except IntegrityError:  # unique (user_id, sacco_id)
        db.session.rollback()
        return error_response(409, "That user is already a member of this SACCO.")
    _grant_default_title(membership)
    _audit(membership, "member.add")
    db.session.commit()
    payin.announce_new_officers(sacco_id, officers_before, g.access.user.email)
    return jsonify(_member_json(membership, target.email)), 201


def _reset_titles(membership: SaccoMembership) -> None:
    """After a role change, drop every job title the old role carried.

    Titles left behind would keep powers the new role does not have (a loans
    officer demoted to member could still approve). With no title rows, `can()`
    answers from the role column; a new admin gets sacco_admin as a row, so
    `can()` and the admin routes (which read the role) agree.
    """
    db.session.execute(
        delete(MembershipRoleLink).where(
            MembershipRoleLink.membership_id == membership.id
        )
    )
    _grant_default_title(membership)


def _grant_default_title(membership: SaccoMembership, title: str | None = None) -> None:
    """Store the job title the invite asked for (or the one the role implies:
    admin -> sacco_admin, staff -> secretary, member -> member), so `can()`
    reads a title row for every membership the API creates or changes, never the
    role column.
    """
    code = title or LEGACY_ROLE_CODES[membership.role]
    role_id = db.session.execute(
        select(Role.id).where(Role.code == code)
    ).scalar_one_or_none()
    if role_id is not None:  # no catalogue (create_all): the role column answers
        db.session.add(
            MembershipRoleLink(
                membership_id=membership.id,
                role_id=role_id,
                granted_by_user_id=g.access.user.id,
            )
        )


@saccos_bp.patch("/<int:sacco_id>/members/<int:membership_id>")
@write_limit
@permission_scoped("member.manage", "admin.manage")
def update_member(sacco_id, membership_id):
    try:
        changes = parse_update_member(_body())
    except InvalidMember as exc:
        return error_response(400, str(exc))
    rows = _members(sacco_id, membership_id, lock=True)
    if not rows:
        db.session.rollback()
        return error_response(404, "Member not found.")
    membership, email = rows[0]
    # An admin cannot touch an admin row (their own included) or create one.
    touches_admin = is_admin_membership(membership) or is_admin_role(
        changes.get("role")
    )
    if not touches_admin and not can(g.access.user, sacco_id, "member.manage"):
        db.session.rollback()
        return error_response(
            403, "Adding or changing members needs member.manage (the SACCO admin)."
        )
    if touches_admin and not can(g.access.user, sacco_id, "admin.manage"):
        db.session.rollback()
        return error_response(
            403, "Only the platform owner can change SACCO administrators."
        )
    new_status = changes.get("status")
    reinvite = False
    if new_status is not None and new_status is not membership.status:
        if new_status is MembershipStatus.INVITED:
            # Agreed rule: only inactive -> invited, a re-add by phone with a
            # fresh join code. A person who declined can be invited again the
            # same way. An active member never goes back to invited.
            idle = membership.status in (
                MembershipStatus.INACTIVE,
                MembershipStatus.DECLINED,
            )
            if not idle or not membership.phone:
                db.session.rollback()
                return error_response(
                    409,
                    "Only an inactive member with a phone can be invited again.",
                )
            reinvite = True
        # Nobody has joined a phone invitation yet (no user): only the join flow,
        # with its code, makes it active. Also closes invited -> inactive -> active.
        if new_status is MembershipStatus.ACTIVE and (
            membership.status is MembershipStatus.INVITED
            or membership.user_id is None
            or (membership.phone is not None and membership.pin_hash is None)
        ):
            db.session.rollback()
            return error_response(
                409, "An invited member becomes active by joining with their code."
            )
    if all(getattr(membership, f) is v for f, v in changes.items()):
        body = _member_json(membership, email)
        db.session.rollback()  # nothing changed; release the lock
        return jsonify(body)
    before = _state(membership)
    officers_before = payin.officer_ids(sacco_id)
    old_role = membership.role
    for field, value in changes.items():
        setattr(membership, field, value)
    if membership.role is not old_role:
        _reset_titles(membership)
    if new_status is MembershipStatus.INACTIVE:
        # Cancelling (or ending) a membership kills its unused join codes.
        burn_codes(membership.id)
    code = None
    if reinvite:
        # A re-add starts over: the person joins and sets a new PIN, and holds
        # only the default title of their role until an admin gives more.
        _reset_titles(membership)
        membership.declined_at = membership.decline_reason = None
        membership.pin_hash = None
        membership.pin_attempts = 0
        membership.pin_locked_until = None
        row, code = issue_code(
            membership.id,
            g.access.user.id,
            current_app.config["SECRET_KEY"],
            ttl=_code_ttl(sacco_id),
        )
    _audit(membership, "member.update", before)
    body = _member_json(membership, email)
    if code is not None:
        body.update(join_code=code, expires_at=row.expires_at.isoformat())
    db.session.commit()
    payin.announce_new_officers(sacco_id, officers_before, g.access.user.email)
    return jsonify(body)
