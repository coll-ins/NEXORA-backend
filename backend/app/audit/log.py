"""The append-only audit trail (Move 3, Neo).

Every financial action records one row here, in the same database transaction as
the action itself, naming the actor and what they touched. The row is queued on
the session, never committed by this module: a refused action rolls back and
leaves no audit row behind, and a successful one cannot commit its audit entry
without its money movement.

There are no edit or delete helpers, by design. `audit_logs` is append-only in
the database (migration 0005 refuses UPDATE, DELETE and TRUNCATE).

Action names in use, kept as they are so reports and tests do not break:
`savings.open`, `savings.deposit`, `savings.withdrawal`, `savings.reverse`.
"""

from app.extensions import db
from app.models.audit import AuditLog

MAX_ACTION_LENGTH = 100  # AuditLog.action is String(100)
MAX_TARGET_LENGTH = 255  # AuditLog.target is String(255)


def record(
    sacco_id: int | None,
    actor_user_id: int | None,
    action: str,
    target: str | None = None,
) -> AuditLog:
    """Queue one audit row for the caller's transaction and return it.

    `sacco_id` is None for a platform-wide action and `actor_user_id` is None when
    there is no authenticated actor, which is why both columns are nullable. The
    lengths are checked here so an oversized value is a clear error instead of a
    database failure halfway through a money movement.
    """
    if not isinstance(action, str) or not action.strip():
        raise ValueError("action must be a non-blank string.")
    if len(action) > MAX_ACTION_LENGTH:
        raise ValueError(f"action cannot exceed {MAX_ACTION_LENGTH} characters.")
    if target is not None:
        if not isinstance(target, str) or not target.strip():
            raise ValueError("target must be a non-blank string when given.")
        if len(target) > MAX_TARGET_LENGTH:
            raise ValueError(f"target cannot exceed {MAX_TARGET_LENGTH} characters.")

    row = AuditLog(
        sacco_id=sacco_id, actor_id=actor_user_id, action=action, target=target
    )
    db.session.add(row)
    return row
