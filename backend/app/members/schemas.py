"""Validation for the SACCO member endpoints (Move 2).

Bodies arrive as plain dictionaries, so every field is checked here before a
model is touched. Unknown fields are refused rather than ignored: a typo in a
field name must not look like a successful update that changed nothing.
"""

from app.auth.routes import EMAIL_RE, MAX_EMAIL_LENGTH
from app.models.membership import MembershipRole, MembershipStatus
from app.security.permissions import LEGACY_ROLE_CODES, ROLE_NAMES

CREATE_FIELDS = {"email", "role", "status"}
EMAIL_INVITE_FIELDS = {"email", "role", "invite"}
# The legacy `role` values an invite may send, and the job titles the Add a
# member page offers. Parsed here (this module is where the API's role field is
# read) so that no route or service decides access by a role name.
INVITE_ROLES = {
    "sacco_admin": MembershipRole.SACCO_ADMIN,
    "staff": MembershipRole.STAFF,
    "member": MembershipRole.MEMBER,
}
UPDATE_FIELDS = {"role", "status"}


class InvalidMember(Exception):
    """A refused body. The message is safe to show the client."""


def parse_invite_role(raw) -> tuple[MembershipRole, str]:
    """The (legacy role, job title) an invite asked for.

    `can()` reads titles, so the title that was asked for is the one to grant.
    Raises InvalidMember for anything the catalogue does not name.
    """
    value = str(raw)
    if value in INVITE_ROLES:
        role = INVITE_ROLES[value]
        return role, LEGACY_ROLE_CODES[role]
    if value in ROLE_NAMES:
        return MembershipRole.STAFF, value
    raise InvalidMember("A valid phone and role are required.")


def _options(enum_class) -> str:
    return ", ".join(member.value for member in enum_class)


def _email(value) -> str:
    email = value.strip().lower() if isinstance(value, str) else ""
    try:
        email.encode()  # a lone surrogate cannot be stored
    except UnicodeEncodeError:
        email = ""
    valid = (
        email
        and len(email) <= MAX_EMAIL_LENGTH
        and "\x00" not in email
        and EMAIL_RE.fullmatch(email)
    )
    if not valid:
        raise InvalidMember("A valid email is required.")
    return email


def _choice(value, enum_class, field: str, default=None):
    """The matching enum member, the default when the field is absent, or an error."""
    if value is None:
        if default is None:
            raise InvalidMember(f"{field} is required.")
        return default
    problem = InvalidMember(f"{field} must be one of: {_options(enum_class)}.")
    if not isinstance(value, str):
        raise problem
    try:
        return enum_class(value.strip().lower())
    except ValueError:
        raise problem from None


def _reject_unknown(data: dict, allowed: set) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise InvalidMember(f"Unknown field(s): {', '.join(sorted(unknown))}.")


def parse_create_member(data: dict) -> tuple[str, MembershipRole, MembershipStatus]:
    """(email, role, status); a new member defaults to role member and active."""
    _reject_unknown(data, CREATE_FIELDS)
    return (
        _email(data.get("email")),
        _choice(data.get("role"), MembershipRole, "role", MembershipRole.MEMBER),
        _choice(
            data.get("status"), MembershipStatus, "status", MembershipStatus.ACTIVE
        ),
    )


def parse_email_invite(data: dict) -> tuple[str, MembershipRole, str]:
    """(email, legacy role, job title) for an invite whose join code is emailed.

    The caller sends `invite: true` with an email, so the older request that adds
    an email member as active (and refuses `status: invited`) keeps its meaning.
    """
    _reject_unknown(data, EMAIL_INVITE_FIELDS)
    email = _email(data.get("email"))
    try:
        role, title = parse_invite_role(data.get("role", "member"))
    except InvalidMember:
        raise InvalidMember("A valid role is required.") from None
    return email, role, title


def parse_update_member(data: dict) -> dict:
    """Only the fields that were sent, already converted to their enums."""
    _reject_unknown(data, UPDATE_FIELDS)
    changes = {}
    if "role" in data:
        changes["role"] = _choice(data["role"], MembershipRole, "role")
    if "status" in data:
        changes["status"] = _choice(data["status"], MembershipStatus, "status")
    if not changes:
        raise InvalidMember("Send role and/or status to update.")
    return changes
