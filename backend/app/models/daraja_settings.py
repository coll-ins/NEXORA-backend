"""A SACCO's own Daraja credentials. The three secrets are stored encrypted."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class SaccoDarajaSettings(db.Model):
    __tablename__ = "sacco_daraja_settings"
    __table_args__ = (
        CheckConstraint("shortcode ~ '^[0-9]{5,7}$'", name="ck_daraja_shortcode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id"), nullable=False, unique=True
    )
    # BusinessShortCode: the paybill, or the head-office number of a till.
    shortcode: Mapped[str] = mapped_column(String(7), nullable=False)
    consumer_key_enc: Mapped[str] = mapped_column(String(600), nullable=False)
    consumer_secret_enc: Mapped[str] = mapped_column(String(600), nullable=False)
    passkey_enc: Mapped[str] = mapped_column(String(900), nullable=False)
    updated_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
