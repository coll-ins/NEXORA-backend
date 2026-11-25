import enum
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class MembershipRole(enum.Enum):
    SACCO_ADMIN = "sacco_admin"
    STAFF = "staff"
    MEMBER = "member"


class MembershipStatus(enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    INVITED = "invited"
    # The person read the rules and refused the invitation, with a reason.
    DECLINED = "declined"


class SaccoMembership(db.Model):
    __tablename__ = "sacco_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "sacco_id", name="uq_membership_user_sacco"),
        UniqueConstraint("id", "sacco_id", name="uq_membership_id_sacco"),
        UniqueConstraint("sacco_id", "phone", name="uq_membership_sacco_phone"),
        CheckConstraint(
            "pin_attempts >= 0", name="ck_memberships_pin_attempts_non_negative"
        ),
        CheckConstraint(
            "user_id IS NOT NULL OR phone IS NOT NULL",
            name="ck_membership_user_or_phone",
        ),
        # The member's own record. Every field is optional for now.
        CheckConstraint(
            "gender IN ('female', 'male', 'other')", name="ck_memberships_gender"
        ),
        CheckConstraint(
            "national_id ~ '^[0-9]{7,8}$'", name="ck_memberships_national_id"
        ),
        CheckConstraint(
            "date_of_birth >= DATE '1900-01-01'", name="ck_memberships_date_of_birth"
        ),
        CheckConstraint(
            "share_capital >= 0", name="ck_memberships_share_capital_non_negative"
        ),
        Index(
            "uq_memberships_sacco_national_id",
            "sacco_id",
            "national_id",
            unique=True,
            postgresql_where=text("national_id IS NOT NULL"),
        ),
        # A decline always has the moment and the reason, or neither.
        CheckConstraint(
            "(declined_at IS NULL) = (decline_reason IS NULL) "
            "AND (status <> 'declined' OR declined_at IS NOT NULL)",
            name="ck_memberships_decline_pair",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id"), nullable=False, index=True
    )
    role: Mapped[MembershipRole] = mapped_column(
        Enum(
            MembershipRole,
            name="membership_role",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    status: Mapped[MembershipStatus] = mapped_column(
        Enum(
            MembershipStatus,
            name="membership_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=MembershipStatus.ACTIVE,
        server_default=MembershipStatus.ACTIVE.value,
    )
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Personal data (Data Protection Act 2019): stored only while the member's
    # `member_profile` consent is active, and erased when it is withdrawn.
    full_name: Mapped[str | None] = mapped_column(String(120))
    national_id: Mapped[str | None] = mapped_column(String(8))
    gender: Mapped[str | None] = mapped_column(String(10))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    # What the member has paid in as share capital, in minor units. Recorded by
    # an officer; NEXORA stores and shows it but does not apply it to any rule.
    share_capital: Mapped[int | None] = mapped_column(BigInteger)
    # SACCO PIN (Section 2 sets and checks it; Section 1 owns the columns).
    # Stored hashed, never in clear. Five wrong entries lock it for 15 minutes.
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pin_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    pin_locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The member refused the invitation after reading the rules: when, and why in
    # their own words, so the officers can see it. Both cleared by a re-invite.
    declined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decline_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
