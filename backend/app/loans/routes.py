"""Loan endpoints: request, list/get, approve/reject step, disburse, repay."""

from dataclasses import asdict

from flask import jsonify, request
from sqlalchemy import select

from app import loans_bp
from app.auth.limits import money_limit, read_limit, write_limit
from app.errors import error_response
from app.extensions import db
from app.loans import service
from app.models.loan import Loan, LoanDecision, LoanDecisionKind, LoanStatus
from app.savings.history import page_params
from app.savings.routes import _body, signed_in
from app.savings.validation import (
    InvalidRequest,
    parse_amount,
    parse_flag,
    parse_id,
    parse_money,
    parse_reference,
    parse_sector,
)
from app.security.permissions import can
from app.security.tenant import AccessDenied, current_user, sacco_access


def _serialize(view) -> dict:
    data = asdict(view)
    for k, v in data.items():
        if hasattr(v, "isoformat"):
            data[k] = v.isoformat()
    return data


@loans_bp.post("/saccos/<int:sacco_id>")
@write_limit
@signed_in
def create_loan(user_id: int, sacco_id: int):
    body = _body()
    principal = parse_amount(body.get("principal"))
    term = body.get("term_months")
    if isinstance(term, bool) or not isinstance(term, int) or not 1 <= term <= 360:
        raise InvalidRequest("term_months must be a whole number from 1 to 360.")
    sector = parse_sector(body.get("sector"))
    disclosure = parse_flag(
        body.get("disclosure_acknowledged"), "disclosure_acknowledged"
    )
    view = service.request_loan(user_id, sacco_id, principal, term, sector, disclosure)
    return jsonify(_serialize(view)), 201


@loans_bp.get("/saccos/<int:sacco_id>")
@read_limit
def list_loans(sacco_id: int):
    raw_status = request.args.get("status")
    wanted = None
    if raw_status is not None:
        try:
            wanted = LoanStatus(raw_status)
        except ValueError:
            return error_response(400, "Unknown status.")
    try:
        user = current_user()
        access = sacco_access(user, sacco_id)
        page, per_page = page_params()
    except AccessDenied as exc:
        return error_response(exc.status, exc.message)
    except ValueError as exc:
        return error_response(400, str(exc))

    stmt = select(Loan).where(Loan.sacco_id == sacco_id)
    if wanted is not None:
        stmt = stmt.where(Loan.status == wanted)
    if not can(user, sacco_id, "loan.view"):
        if access.membership is None:  # a platform owner without a loans grant
            return error_response(403, "You are not allowed to do this.")
        stmt = stmt.where(Loan.membership_id == access.membership.id)
    loans = (
        db.session.execute(
            stmt.order_by(Loan.id.desc()).limit(per_page).offset((page - 1) * per_page)
        )
        .scalars()
        .all()
    )
    return jsonify(
        loans=[_serialize(view) for view in service._loan_views(loans)],
        page=page,
        per_page=per_page,
    )


@loans_bp.get("/<int:loan_id>")
@read_limit
def get_loan(loan_id: int):
    loan = db.session.get(Loan, loan_id)
    if loan is None:
        return error_response(404, "Not found.")
    try:
        user = current_user()
        access = sacco_access(user, loan.sacco_id)
    except AccessDenied as exc:
        return error_response(exc.status, exc.message)
    own = access.membership is not None and loan.membership_id == access.membership.id
    if not own and not can(user, loan.sacco_id, "loan.view"):
        return error_response(404, "Not found.")

    decisions = (
        db.session.execute(
            select(LoanDecision)
            .where(LoanDecision.loan_id == loan.id)
            .order_by(LoanDecision.step)
        )
        .scalars()
        .all()
    )
    payload = _serialize(service._loan_view(loan))
    payload["decisions"] = [
        {
            "step": d.step,
            "decision": d.decision.value,
            "reason": d.reason,
            "decided_by_membership_id": d.decided_by_membership_id,
            "created_at": d.created_at.isoformat(),
        }
        for d in decisions
    ]
    return jsonify(payload)


@loans_bp.post("/<int:loan_id>/decisions")
@write_limit
@signed_in
def decide(user_id: int, loan_id: int):
    body = _body()
    step = parse_id(body.get("step"), "step")
    raw_decision = body.get("decision")
    if raw_decision not in ("approve", "reject"):
        raise InvalidRequest("decision must be 'approve' or 'reject'.")
    reason = body.get("reason")
    if reason is not None and (
        not isinstance(reason, str) or len(reason) > 500 or "\x00" in reason
    ):
        raise InvalidRequest("reason must be text of at most 500 characters.")
    reason = (reason or "").strip() or None
    if raw_decision == "reject" and reason is None:
        raise InvalidRequest("A rejection needs a reason.")
    view = service.decide_loan(
        user_id, loan_id, step, LoanDecisionKind(raw_decision), reason
    )
    return jsonify(_serialize(view)), 201


@loans_bp.post("/<int:loan_id>/disbursements")
@money_limit
@signed_in
def disburse(user_id: int, loan_id: int):
    reference = parse_reference(_body().get("reference"))
    view = service.disburse_loan(user_id, loan_id, reference)
    return jsonify(_serialize(view)), 201


@loans_bp.post("/<int:loan_id>/repayments")
@money_limit
@signed_in
def repay(user_id: int, loan_id: int):
    amount, reference = parse_money(_body())
    view = service.repay_loan(user_id, loan_id, amount, reference)
    return jsonify(_serialize(view)), 201
