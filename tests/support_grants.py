"""Give a platform owner an approved support-access grant (tests only).

Goes through the same rows and checks as the API: a request by the owner, then
an approval by an active admin of that SACCO, valid for an hour.
"""

from sqlalchemy import text

from app.extensions import db


def approve_support(owner_id: int, sacco_id: int, scope: str, admin_membership_id: int):
    grant_id = db.session.execute(
        text(
            "INSERT INTO support_access_requests "
            "(sacco_id, requested_by_user_id, reason, scope) "
            "VALUES (:s, :o, 'test', :scope) RETURNING id"
        ),
        {"s": sacco_id, "o": owner_id, "scope": scope},
    ).scalar_one()
    db.session.execute(
        text(
            "UPDATE support_access_requests SET status = 'approved', "
            "decided_by_membership_id = :m, decided_at = now(), "
            "expires_at = now() + interval '1 hour' WHERE id = :g"
        ),
        {"m": admin_membership_id, "g": grant_id},
    )
    db.session.commit()
    return grant_id
