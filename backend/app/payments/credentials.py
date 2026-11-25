"""A SACCO's own Daraja credentials, encrypted at rest with DARAJA_ENCRYPTION_KEY.

The key lives only in the environment, never in the database. Lose it and the
stored credentials cannot be read: the SACCO's admin sets them again.
"""

from typing import NamedTuple

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app
from sqlalchemy import select

from app.extensions import db
from app.models.daraja_settings import SaccoDarajaSettings


class Credentials(NamedTuple):
    shortcode: str
    key: str
    secret: str
    passkey: str
    own: bool = False  # the SACCO's saved credentials, not the platform's sandbox ones


class CredentialsError(Exception):
    """The message is safe to show."""


def _fernet() -> Fernet:
    key = current_app.config.get("DARAJA_ENCRYPTION_KEY")
    if not key:
        raise CredentialsError(
            "Saving M-Pesa credentials is off: DARAJA_ENCRYPTION_KEY is not set."
        )
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise CredentialsError("DARAJA_ENCRYPTION_KEY is not a valid key.") from exc


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def for_sacco(sacco_id: int) -> Credentials | None:
    """The SACCO's own credentials, else (sandbox only) the environment's, else None.

    In production there is no fallback: the shared keys in the environment are
    sandbox keys and must never reach the live API.
    """
    row = db.session.execute(
        select(SaccoDarajaSettings).where(SaccoDarajaSettings.sacco_id == sacco_id)
    ).scalar_one_or_none()
    cfg = current_app.config
    if row is None:
        if cfg.get("DARAJA_ENV") == "production":
            return None
        env = [cfg.get(k) for k in _ENV]
        return Credentials(env[3], env[0], env[1], env[2]) if all(env) else None
    try:
        f = _fernet()
        return Credentials(
            row.shortcode,
            f.decrypt(row.consumer_key_enc.encode()).decode(),
            f.decrypt(row.consumer_secret_enc.encode()).decode(),
            f.decrypt(row.passkey_enc.encode()).decode(),
            own=True,
        )
    except InvalidToken as exc:
        raise CredentialsError(
            "This SACCO's M-Pesa credentials cannot be read. A SACCO admin must "
            "save them again."
        ) from exc


_ENV = (
    "DARAJA_CONSUMER_KEY",
    "DARAJA_CONSUMER_SECRET",
    "DARAJA_PASSKEY",
    "DARAJA_SHORTCODE",
)
