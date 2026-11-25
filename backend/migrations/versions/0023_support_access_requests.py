"""consent-based support access: support_access_requests

Revision ID: 0023
Revises: 0022

A platform owner may read a SACCO's data only with that SACCO's consent: the
owner asks (with a reason and one scope), an active admin of the SACCO approves
or denies, and an approval lasts at most 24 hours. The admin can revoke at any
time. There is no bypass or emergency path.

The trigger holds the rules even for a connection that skips the application:

* only an active platform owner can request, and only as status "requested";
* only an active SACCO admin of that SACCO can approve, deny or revoke, and
  never the requester;
* requested -> approved | denied; approved -> revoked | expired; nothing else;
* an approval expires at most 24 hours after it is given;
* the SACCO, requester, reason and scope never change.
"""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

SCOPE = sa.Enum("audit", "members", "savings", "loans", name="support_scope")
STATUS = sa.Enum(
    "requested", "approved", "denied", "revoked", "expired", name="support_status"
)


def upgrade():
    op.create_table(
        "support_access_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sacco_id", sa.Integer(), sa.ForeignKey("saccos.id"), nullable=False),
        sa.Column(
            "requested_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("scope", SCOPE, nullable=False),
        sa.Column("status", STATUS, nullable=False, server_default="requested"),
        sa.Column("decided_by_membership_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_membership_id", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # A decider or revoker is a membership of this same SACCO.
        sa.ForeignKeyConstraint(
            ["decided_by_membership_id", "sacco_id"],
            ["sacco_memberships.id", "sacco_memberships.sacco_id"],
            name="fk_support_decider_same_sacco",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_membership_id", "sacco_id"],
            ["sacco_memberships.id", "sacco_memberships.sacco_id"],
            name="fk_support_revoker_same_sacco",
        ),
        sa.CheckConstraint("btrim(reason) <> ''", name="ck_support_reason_not_blank"),
        sa.CheckConstraint(
            "status <> 'approved' "
            "OR (expires_at IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_support_approved_has_expiry",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at <= decided_at + interval '24 hours'",
            name="ck_support_at_most_24_hours",
        ),
    )
    op.create_index(
        "ix_support_access_sacco_status",
        "support_access_requests",
        ["sacco_id", "status"],
    )
    op.execute(
        """
        CREATE FUNCTION guard_support_access() RETURNS trigger AS $$
        DECLARE
            actor integer;
            actor_user integer;
            is_admin boolean;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NOT (
                    EXISTS (SELECT 1 FROM users
                            WHERE id = NEW.requested_by_user_id AND is_platform_owner)
                    OR EXISTS (SELECT 1 FROM platform_grants
                               WHERE user_id = NEW.requested_by_user_id)
                ) THEN
                    RAISE EXCEPTION
                        'only an active platform owner can request support access'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.status <> 'requested' OR NEW.decided_by_membership_id IS NOT NULL
                   OR NEW.revoked_by_membership_id IS NOT NULL THEN
                    RAISE EXCEPTION 'a support request starts as requested'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.sacco_id <> OLD.sacco_id
               OR NEW.requested_by_user_id <> OLD.requested_by_user_id
               OR NEW.reason <> OLD.reason OR NEW.scope <> OLD.scope
               OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION 'a support request cannot be rewritten'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = OLD.status THEN
                IF NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'a support request changes only with its status'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF NOT (
                   (OLD.status = 'requested' AND NEW.status IN ('approved', 'denied'))
                OR (OLD.status = 'approved' AND NEW.status IN ('revoked', 'expired'))
            ) THEN
                RAISE EXCEPTION 'support access % -> % is not allowed',
                    OLD.status, NEW.status
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'expired' THEN
                IF NEW.expires_at > now() THEN
                    RAISE EXCEPTION 'this grant has not expired yet'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;

            actor := CASE WHEN NEW.status = 'revoked'
                          THEN NEW.revoked_by_membership_id
                          ELSE NEW.decided_by_membership_id END;
            SELECT m.user_id, (m.role = 'sacco_admin' AND m.status = 'active')
              INTO actor_user, is_admin
              FROM sacco_memberships m
             WHERE m.id = actor AND m.sacco_id = NEW.sacco_id;
            IF actor IS NULL OR NOT coalesce(is_admin, false) THEN
                RAISE EXCEPTION
                    'only an active SACCO admin of this SACCO can decide or revoke'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF actor_user = NEW.requested_by_user_id THEN
                RAISE EXCEPTION 'the requester cannot decide their own request'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'revoked' THEN
                NEW.revoked_at := now();
            ELSE
                NEW.decided_at := now();
                IF NEW.status = 'approved' AND (
                       NEW.expires_at IS NULL OR NEW.expires_at <= now()
                    OR NEW.expires_at > now() + interval '24 hours') THEN
                    RAISE EXCEPTION 'an approval lasts more than 0 and at most 24 hours'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_support_access_guard "
        "BEFORE INSERT OR UPDATE ON support_access_requests "
        "FOR EACH ROW EXECUTE FUNCTION guard_support_access()"
    )
    op.execute(
        "CREATE TRIGGER trg_support_access_no_delete "
        "BEFORE DELETE ON support_access_requests "
        "FOR EACH ROW EXECUTE FUNCTION append_only()"
    )


def downgrade():
    op.execute("DROP TRIGGER trg_support_access_no_delete ON support_access_requests")
    op.execute("DROP TRIGGER trg_support_access_guard ON support_access_requests")
    op.execute("DROP FUNCTION guard_support_access()")
    op.drop_index("ix_support_access_sacco_status", "support_access_requests")
    op.drop_table("support_access_requests")
    STATUS.drop(op.get_bind(), checkfirst=False)
    SCOPE.drop(op.get_bind(), checkfirst=False)
