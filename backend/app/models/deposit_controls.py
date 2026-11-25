"""A SACCO's deposit-control thresholds. No row means the defaults below."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db

# Minor units (100 = KES 1). Safe defaults: nothing auto-confirms.
DEFAULTS = {
    "auto_confirm_limit": 0,  # 0 = every deposit waits for an officer
    "large_amount_limit": 10_000_000,  # KES 100,000
    "manual_flag_limit": 2_000_000,  # KES 20,000
    "repeat_count": 3,
    "repeat_window_hours": 24,
    "quick_withdrawal_hours": 24,  # 0 (set in the database only) switches this flag off
}
MAX_AUTO_CONFIRM = 500_000  # KES 5,000: a hard ceiling no setting can lift


class SaccoDepositControls(db.Model):
    __tablename__ = "sacco_deposit_controls"
    __table_args__ = (
        CheckConstraint(
            f"auto_confirm_limit BETWEEN 0 AND {MAX_AUTO_CONFIRM}",
            name="ck_deposit_controls_auto",
        ),
        CheckConstraint("large_amount_limit > 0", name="ck_deposit_controls_large"),
        CheckConstraint("manual_flag_limit > 0", name="ck_deposit_controls_manual"),
        CheckConstraint("repeat_count >= 2", name="ck_deposit_controls_repeat"),
        CheckConstraint(
            "repeat_window_hours > 0 AND quick_withdrawal_hours >= 0",
            name="ck_deposit_controls_hours",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sacco_id: Mapped[int] = mapped_column(
        ForeignKey("saccos.id"), nullable=False, unique=True
    )
    auto_confirm_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    large_amount_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    manual_flag_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repeat_count: Mapped[int] = mapped_column(Integer, nullable=False)
    repeat_window_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    quick_withdrawal_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
