"""JWT access tokens and revocable, rotating refresh tokens."""

import re
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from flask import current_app
from sqlalchemy import select, update

from app.extensions import db
from app.models.refresh_token import RefreshToken
from app.models.user import User

ALGORITHM = "HS256"
# Who issued a token and who it is for. A token signed with the same key for some
# other service or purpose is refused.
ISSUER = "nexora-api"
AUDIENCE = "nexora"
# A spent token replayed within this window is a double submit, not theft.
REUSE_GRACE = timedelta(seconds=10)


class TokenError(Exception):
    """The token is malformed, expired, revoked or of the wrong type."""


def _now() -> datetime:
    return datetime.now(UTC)


def _encode(
    user_id: int, token_type: str, lifetime: timedelta, **claims
) -> tuple[str, dict]:
    now = _now()
    payload = {
        **claims,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "type": token_type,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + lifetime,
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], ALGORITHM), payload


def _decode(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            current_app.config["SECRET_KEY"],
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["exp", "iat", "sub", "jti", "type", "iss", "aud"]},
        )
        valid = (
            payload["type"] == expected_type
            and re.fullmatch(r"[1-9][0-9]{0,9}", payload["sub"])
            and int(payload["sub"]) < 2**31
            and re.fullmatch(r"[0-9a-f]{32}", payload["jti"])
        )
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise TokenError("Invalid or expired token.") from exc
    if not valid:
        raise TokenError("Invalid or expired token.")
    return payload


def create_access_token(user_id: int, token_version: int) -> str:
    """`tv` is checked against users.token_version on every request."""
    lifetime = current_app.config["ACCESS_TOKEN_EXPIRES"]
    return _encode(user_id, "access", lifetime, tv=token_version)[0]


def decode_access_token(token: str) -> dict:
    return _decode(token, "access")


def create_refresh_token(user_id: int) -> str:
    """Issue a refresh token and record its jti. The caller commits."""
    token, p = _encode(user_id, "refresh", current_app.config["REFRESH_TOKEN_EXPIRES"])
    db.session.add(RefreshToken(user_id=user_id, jti=p["jti"], expires_at=p["exp"]))
    return token


def lock_user(user_id: int) -> None:
    """Hold the user's row until commit. Rotation, logout-all and change-password
    all take it first, so a rotation cannot mint a token after a sweep ran."""
    db.session.execute(select(User.id).where(User.id == user_id).with_for_update())


def rotate_refresh_token(token: str) -> tuple[int, str]:
    """Spend a refresh token and return (user_id, new_refresh_token). Commits.

    One conditional UPDATE spends the token, so concurrent requests cannot both
    win. Replaying a token spent longer ago than REUSE_GRACE means it was
    probably copied, so all of that user's refresh tokens are revoked.
    """
    payload = _decode(token, "refresh")
    user_id = int(payload["sub"])
    lock_user(user_id)
    now = _now()
    live = RefreshToken.revoked_at.is_(None)

    spent = db.session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.jti == payload["jti"],
            RefreshToken.user_id == user_id,
            live,
            RefreshToken.expires_at > now,
        )
        .values(revoked_at=now)
    )
    if spent.rowcount != 1:
        row = db.session.query(RefreshToken.revoked_at).filter_by(jti=payload["jti"])
        revoked_at = row.scalar()
        if revoked_at and revoked_at < now - REUSE_GRACE:
            db.session.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == user_id, live)
                .values(revoked_at=now)
            )
        db.session.commit()
        raise TokenError("Refresh token is no longer valid.")

    new_token = create_refresh_token(user_id)
    db.session.commit()
    return user_id, new_token


def revoke_refresh_token(token: str) -> None:
    """Revoke one refresh token (already revoked is fine). Commits."""
    jti = _decode(token, "refresh")["jti"]
    db.session.execute(
        update(RefreshToken)
        .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    db.session.commit()
