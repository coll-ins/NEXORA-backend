from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

ph = PasswordHasher()


def hash_password(password: str) -> str:
    return ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return ph.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


def validate_password(password: str, email: str) -> str | None:
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if email and email.lower() in password.lower():
        return "Password cannot contain your email address."
    return None
