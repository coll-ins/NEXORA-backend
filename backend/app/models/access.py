"""Roles, permissions and platform grants (Section 1, Neo).

The access model is data, not code: a membership holds one or more rows in
`membership_roles`, each pointing at a `roles` row, and each role holds
permissions through `role_permissions`. `can()` in `app/security/permissions.py`
is the only reader.

`platform_grants` exists so a platform owner needs no membership in any SACCO:
the grant is the authority, and it is deliberately a separate table so a
membership can never be mistaken for platform access.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class Role(db.Model):
    """One job title. Seeded as rows, never hard-coded into a route."""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Permission(db.Model):
    """One thing that may be allowed, named as `area.action`."""

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class RolePermission(db.Model):
    """Which permissions a role holds. Composite primary key, no surrogate id."""

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class MembershipRoleLink(db.Model):
    """The roles a membership holds. A membership may hold more than one.

    The legacy `sacco_memberships.role` column stays for compatibility; the
    migration backfills this table from it, and `can()` falls back to it when a
    membership has no rows here yet.
    """

    __tablename__ = "membership_roles"

    membership_id: Mapped[int] = mapped_column(
        ForeignKey("sacco_memberships.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    # Who gave the title. Null for system grants (migration backfill, seed). The
    # database refuses a granter who owns this membership (migration 0021).
    granted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_membership_roles_granted_by"), nullable=True
    )
    # When the title was given. A pay-in change can only be approved by someone
    # who already held an officer title when it was proposed (migration 0027).
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlatformGrant(db.Model):
    """Platform access that does not depend on any SACCO membership."""

    __tablename__ = "platform_grants"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    granted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
