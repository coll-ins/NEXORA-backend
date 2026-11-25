"""Savings account reads. Read-only.

Access rules are the ones in history.py: another SACCO or someone else's account
is 404, an inactive membership is 403, a suspended SACCO is 423, and the platform
owner may read any account. Balances are integers in minor units.
"""

from flask import jsonify
from sqlalchemy import func, select

from app import savings_bp
from app.auth.limits import read_limit
from app.errors import error_response
from app.extensions import db
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.sacco import Sacco, SaccoStatus
from app.models.savings import SavingsAccount
from app.models.user import User
from app.savings.history import _open_account
from app.security.tenant import AccessDenied, current_user


def _account_json(account: SavingsAccount) -> dict:
    return {
        "id": account.id,
        "membership_id": account.membership_id,
        "sacco_id": account.sacco_id,
        "balance": account.balance,
        "created_at": account.created_at.isoformat(),
    }


def list_sacco_accounts(sacco_id: int, page: int, per_page: int):
    """(rows, total) for one SACCO. The caller has already opened the SACCO."""
    where = SavingsAccount.sacco_id == sacco_id
    total = db.session.execute(
        select(func.count()).select_from(SavingsAccount).where(where)
    ).scalar_one()
    rows = db.session.execute(
        select(SavingsAccount.id, SavingsAccount.membership_id, User.email,
               SavingsAccount.balance)
        .join(SaccoMembership, SaccoMembership.id == SavingsAccount.membership_id)
        .join(User, User.id == SaccoMembership.user_id)
        .where(where)
        .order_by(SavingsAccount.id)
        .limit(per_page)
        .offset((page - 1) * per_page)
    ).all()
    accounts = [
        {"account_id": r.id, "membership_id": r.membership_id,
         "email": r.email, "balance": r.balance}
        for r in rows
    ]
    return accounts, total


@savings_bp.get("/accounts/me")
@read_limit
def my_accounts():
    try:
        user = current_user()
    except AccessDenied as exc:
        return error_response(exc.status, exc.message)
    accounts = (
        db.session.execute(
            select(SavingsAccount)
            .join(SaccoMembership, SaccoMembership.id == SavingsAccount.membership_id)
            .join(Sacco, Sacco.id == SavingsAccount.sacco_id)
            .where(
                SaccoMembership.user_id == user.id,
                SaccoMembership.status == MembershipStatus.ACTIVE,
                Sacco.status == SaccoStatus.ACTIVE,
            )
            .order_by(SavingsAccount.id)
        )
        .scalars()
        .all()
    )
    return jsonify(accounts=[_account_json(a) for a in accounts])


@savings_bp.get("/accounts/<int:account_id>")
@read_limit
def get_account(account_id: int):
    try:
        account = _open_account(current_user(), account_id)
    except AccessDenied as exc:
        return error_response(exc.status, exc.message)
    return jsonify(_account_json(account))
