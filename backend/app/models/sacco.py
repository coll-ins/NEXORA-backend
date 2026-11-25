import enum
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class SaccoStatus(enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Sacco(db.Model):
    __tablename__ = "saccos"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_saccos_name_not_blank"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_saccos_currency_iso"),
        # Registration data is optional for now; when given it must look like one.
        CheckConstraint(
            "registration_number ~ '^[A-Za-z0-9][A-Za-z0-9 ./-]{1,38}[A-Za-z0-9]$'",
            name="ck_saccos_registration_number",
        ),
        CheckConstraint(
            "sasra_licence_number ~ '^[A-Za-z0-9][A-Za-z0-9 ./-]{1,38}[A-Za-z0-9]$'",
            name="ck_saccos_sasra_licence_number",
        ),
        CheckConstraint(
            "registration_date >= DATE '1900-01-01'",
            name="ck_saccos_registration_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="KES", server_default="KES"
    )
    status: Mapped[SaccoStatus] = mapped_column(
        Enum(
            SaccoStatus,
            name="sacco_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SaccoStatus.ACTIVE,
        server_default=SaccoStatus.ACTIVE.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Who regulates and registered this SACCO. All optional until the product
    # decides to require them.
    registration_number: Mapped[str | None] = mapped_column(String(40))
    sasra_licence_number: Mapped[str | None] = mapped_column(String(40))
    registration_date: Mapped[date | None] = mapped_column(Date)


Index("uq_saccos_name_lower", func.lower(func.btrim(Sacco.name)), unique=True)
Index(
    "uq_saccos_registration_number",
    func.lower(Sacco.registration_number),
    unique=True,
    postgresql_where=text("registration_number IS NOT NULL"),
)
Index(
    "uq_saccos_sasra_licence_number",
    func.lower(Sacco.sasra_licence_number),
    unique=True,
    postgresql_where=text("sasra_licence_number IS NOT NULL"),
)
