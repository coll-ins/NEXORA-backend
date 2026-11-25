from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("btrim(action) <> ''", name="ck_audit_logs_action_not_blank"),
        # Newest-first paging, per SACCO and platform-wide (migration 0041).
        Index(
            "ix_audit_logs_sacco_created",
            "sacco_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index("ix_audit_logs_created", text("created_at DESC"), text("id DESC")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Nullable: platform-owner actions are not tied to one SACCO.
    sacco_id: Mapped[int | None] = mapped_column(ForeignKey("saccos.id"), nullable=True)
    # Nullable: failed logins before authentication have no actor.
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
