"""Transaction history for one savings account. Read-only.

Newest first (created_at, id), paged. Amounts are integers in minor units.
Reversals show up as ordinary rows with `reverses_id` set.
"""

from flask import jsonify, request
from sqlalchemy import func, select

from app import savings_bp
from app.auth.limits import read_limit
from app.errors import error_response
from app.extensions import db
from app.models.ledger import Transaction
from app.models.savings import SavingsAccount
from app.security.permissions import can
from app.security.tenant import AccessDenied, current_user, sacco_access

DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100
MAX_PAGE = 1_000_000


def _whole(name: str, default: int, top: int | None = None) -> int:
    raw = request.args.get(name, str(default))
    if not (raw.isascii() and raw.isdecimal() and int(raw) >= 1):
        raise ValueError(f"{name} must be a whole number of 1 or more.")
    return min(int(raw), top) if top else int(raw)


def page_params() -> tuple[int, int]:
    """(page, per_page) from the query string. ValueError if either is unusable.

    per_page is capped at MAX_PER_PAGE. page is capped too, so the offset stays
    far inside what the database accepts.
    """
    page = _whole("page", 1, MAX_PAGE)
    return page, _whole("per_page", DEFAULT_PER_PAGE, MAX_PER_PAGE)


def _open_account(user, account_id: int) -> SavingsAccount:
    """The account if `user` may read its history, else AccessDenied."""
    account = db.session.get(SavingsAccount, account_id)
    if account is None:
        raise AccessDenied("Not found.", 404)
    access = sacco_access(user, account.sacco_id)
    mine = access.membership
    # Your own account, or every account with savings.view_all (admin, secretary,
    # finance officer). Decided by `can()`, never by the legacy role column.
    own = mine is not None and account.membership_id == mine.id
    if not own and not can(user, account.sacco_id, "savings.view_all"):
        raise AccessDenied("Not found.", 404)  # someone else's account
    return account


def _tx_json(tx: Transaction) -> dict:
    return {
        "id": tx.id,
        "type": tx.type.value,
        "amount": tx.amount,
        "reference": tx.reference,
        "created_at": tx.created_at.isoformat(),
        "reverses_id": tx.reverses_id,
        "source": tx.source,
    }


@savings_bp.get("/accounts/<int:account_id>/transactions")
@read_limit
def account_history(account_id: int):
    try:
        user = current_user()
        account = _open_account(user, account_id)
    except AccessDenied as exc:
        return error_response(exc.status, exc.message)
    try:
        page, per_page = page_params()
    except ValueError as exc:
        return error_response(400, str(exc))

    where = Transaction.savings_account_id == account.id
    total = db.session.execute(
        select(func.count()).select_from(Transaction).where(where)
    ).scalar_one()
    rows = (
        db.session.execute(
            select(Transaction)
            .where(where)
            .order_by(Transaction.created_at.desc(), Transaction.id.desc())
            .limit(per_page)
            .offset((page - 1) * per_page)
        )
        .scalars()
        .all()
    )
    return jsonify(
        transactions=[_tx_json(t) for t in rows],
        page=page,
        per_page=per_page,
        total=total,
    )
