"""Member self-service: a signed-in user reads and updates only their own profile.

The user id always comes from the access token, never from the request, so there
is no id in the URL or body to tamper with. Only the fields in EDITABLE_FIELDS can
change; role, status, SACCO and platform-owner flags are refused outright.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import verification
from app.auth.account import revoke_all_sessions
from app.auth.lockout import check_lockout, record_failed_login, reset_failed_logins
from app.auth.passwords import verify_password
from app.auth.routes import EMAIL_RE, MAX_EMAIL_LENGTH, MAX_PASSWORD_LENGTH
from app.extensions import db
from app.mail import send_safely
from app.models.membership import SaccoMembership
from app.models.user import User

# The schema stores no name or phone yet (docs/database-schema.md lists them).
# Adding one is a migration the team must agree first, then it goes in here.
EDITABLE_FIELDS = {"email"}
BODY_FIELDS = EDITABLE_FIELDS | {"current_password"}


class ProfileError(Exception):
    """A refused request. `status` and the message are safe to show the client."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _clean_email(value) -> str | None:
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    try:
        email.encode()
    except UnicodeEncodeError:
        return None
    if "\x00" in email or len(email) > MAX_EMAIL_LENGTH:
        return None
    return email if EMAIL_RE.fullmatch(email) else None


def _profile(user: User) -> dict:
    memberships = (
        db.session.execute(
            select(SaccoMembership)
            .where(SaccoMembership.user_id == user.id)
            .order_by(SaccoMembership.id)
        )
        .scalars()
        .all()
    )
    return {
        "id": user.id,
        "email": user.email,
        "created_at": user.created_at.isoformat(),
        "memberships": [
            {
                "membership_id": m.id,
                "sacco_id": m.sacco_id,
                "role": m.role.value,
                "status": m.status.value,
            }
            for m in memberships
        ],
    }


def get_profile(user_id: int) -> dict:
    user = db.session.get(User, user_id)
    if user is None:
        raise ProfileError("Not found.", 404)
    return _profile(user)


def update_profile(user_id: int, data: dict) -> dict:
    """Change the caller's own email. Needs the current password.

    The password is required because email is the login identity: a stolen access
    token alone must not be enough to take the account over. Wrong guesses count
    toward the same lockout as login, so this cannot be used to brute-force it.
    """
    if not set(data) <= BODY_FIELDS or not data:
        raise ProfileError("Only email can be changed here.", 400)
    email = _clean_email(data.get("email"))
    password = data.get("current_password")
    if email is None:
        raise ProfileError("A valid email is required.", 400)
    if (
        not isinstance(password, str)
        or not password
        or len(password) > MAX_PASSWORD_LENGTH
        or "\x00" in password
    ):
        raise ProfileError("current_password is required.", 400)

    user = db.session.execute(
        select(User).where(User.id == user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        raise ProfileError("Not found.", 404)

    if check_lockout(user):
        db.session.rollback()
        raise ProfileError("Account temporarily locked. Try again later.", 423)
    if not verify_password(password, user.password_hash):
        record_failed_login(user)
        db.session.commit()
        raise ProfileError("Current password is incorrect.", 403)
    reset_failed_logins(user)

    if email != user.email:
        user.email = email
        user.email_verified_at = None  # the new address has to be proved again
        try:
            db.session.flush()  # the unique email index answers here
            token = verification.issue(user)
            revoke_all_sessions(user.id)  # whoever held a session signs in again
            db.session.commit()
        except IntegrityError as exc:  # unique index on lower(email)
            db.session.rollback()
            raise ProfileError("Email is already registered.", 409) from exc
        send_safely(user.email, token)
    else:
        db.session.commit()
    return _profile(user)
