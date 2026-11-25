"""nobody grants job titles to their own membership

Revision ID: 0021
Revises: 0020

`membership_roles` now records who granted each title. The database refuses a
title whose granter is the user that owns the membership, unless the granter is
a platform owner. Rows without a granter are system grants (the 0010 backfill,
the seed); the application always records the granter.

Collusion between two people (an admin and a friend's account) is not blocked
here; title changes are in the audit log for that.
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "membership_roles",
        sa.Column(
            "granted_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_membership_roles_granted_by"),
            nullable=True,
        ),
    )
    op.execute(
        """
        CREATE FUNCTION forbid_self_granted_role() RETURNS trigger AS $$
        BEGIN
            IF NEW.granted_by_user_id IS NOT NULL
               AND NEW.granted_by_user_id = (
                   SELECT user_id FROM sacco_memberships WHERE id = NEW.membership_id
               )
               AND NOT EXISTS (
                   SELECT 1 FROM users
                   WHERE id = NEW.granted_by_user_id AND is_platform_owner
               )
               AND NOT EXISTS (
                   SELECT 1 FROM platform_grants
                   WHERE user_id = NEW.granted_by_user_id
               ) THEN
                RAISE EXCEPTION 'nobody grants job titles to their own membership'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_membership_roles_no_self_grant "
        "BEFORE INSERT OR UPDATE ON membership_roles "
        "FOR EACH ROW EXECUTE FUNCTION forbid_self_granted_role()"
    )


def downgrade():
    op.execute("DROP TRIGGER trg_membership_roles_no_self_grant ON membership_roles")
    op.execute("DROP FUNCTION forbid_self_granted_role()")
    op.drop_constraint(
        "fk_membership_roles_granted_by", "membership_roles", type_="foreignkey"
    )
    op.drop_column("membership_roles", "granted_by_user_id")
