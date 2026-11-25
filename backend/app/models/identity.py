"""Identity tables for joining a SACCO (Section 1, Neo).

`user_phones` and `join_codes` are the tables behind Section 2's join flow: an
officer adds a phone, a code is shown on that officer's screen, and the member
enters it after logging in. Section 2 owns the behaviour; these tables and their
constraints are the schema it builds against.

A join code is stored as a digest, never in clear, so a database reader cannot
reuse a live code. `attempts` counts wrong entries; the fifth burns the code.
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class UserPhone(db.Model):
    """A phone number linked to exactly one user, once verified."""

    __tablename__ = "user_phones"
    __table_args__ = (
        CheckConstraint(
            "btrim(phone) <> ''", name="ck_user_phones_phone_not_blank"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phone: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JoinCode(db.Model):
    """One six-digit code for one invited membership.

    Single use, time limited, and burned after five wrong entries. The code
    itself is never stored: `code_digest` is a hash of it.
    """

    __tablename__ = "join_codes"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_join_codes_attempts_non_negative"),
        CheckConstraint(
            "expires_at > created_at", name="ck_join_codes_expires_after_creation"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    membership_id: Mapped[int] = mapped_column(
        ForeignKey("sacco_memberships.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
