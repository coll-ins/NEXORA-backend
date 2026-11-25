"""Read-only queries over the audit trail. Every query filters by sacco_id
when one is given; the platform route passes None to read across SACCOs.

There are no write helpers here by design: audit_logs is append-only in the
database.
"""

from datetime import date

from sqlalchemy import func, select

from app.admin.reports import _bounds
from app.extensions import db
from app.models.audit import AuditLog

MAX_OFFSET = 2**62  # keep OFFSET inside Postgres bigint range


def _row(entry: AuditLog) -> dict:
    return {
        "id": entry.id,
        "actor_id": entry.actor_id,
        "action": entry.action,
        "target": entry.target,
        "created_at": entry.created_at.isoformat(),
    }


def list_audit(
    sacco_id: int | None,
    action: str | None,
    start: date | None,
    end: date | None,
    page: int,
    per_page: int,
) -> dict:
    """One page of audit rows, newest first, plus the total match count.

    `action` is an exact name, or a prefix when it ends in "." (for example
    "savings."). Prefix matching escapes % and _ in the input.
    """
    lo, hi = _bounds(start, end)

    conditions = []
    if sacco_id is not None:
        conditions.append(AuditLog.sacco_id == sacco_id)
    if action:
        if action.endswith("."):
            conditions.append(AuditLog.action.startswith(action, autoescape=True))
        else:
            conditions.append(AuditLog.action == action)
    if lo is not None:
        conditions.append(AuditLog.created_at >= lo)
    if hi is not None:
        conditions.append(AuditLog.created_at < hi)

    total = db.session.execute(
        select(func.count()).select_from(AuditLog).where(*conditions)
    ).scalar_one()

    offset = (page - 1) * per_page
    if offset > MAX_OFFSET:
        entries = []
    else:
        entries = (
            db.session.execute(
                select(AuditLog)
                .where(*conditions)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(per_page)
                .offset(offset)
            )
            .scalars()
            .all()
        )

    return {
        "total": int(total),
        "page": page,
        "per_page": per_page,
        "entries": [_row(e) for e in entries],
    }
