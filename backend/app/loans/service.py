"""Loan lifecycle service: request, multi-step approval, disbursement, repayment."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.audit import log as audit_log
from app.errors import (
    DUPLICATE_REFERENCE,
    INSUFFICIENT_FUNDS,
    LOAN_NEEDS_APPROVAL,
    LOAN_SAME_OFFICER,
)
from app.extensions import db
from app.models.ledger import Transaction, TransactionType
from app.models.loan import (
    Loan,
    LoanDecision,
    LoanDecisionKind,
    LoanRepayment,
    LoanStatus,
    SaccoSettings,
)
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.savings import SavingsAccount
from app.models.user import User
from app.saccos.rules import check_loan_disburse, check_loan_request, lock_sacco
from app.savings.service import (
    Conflict,
    Forbidden,
    NotFound,
    _atomic,
    _constraint,
    _require_sacco_active,
)
from app.security.permissions import can

OPEN_STATUSES = (LoanStatus.REQUESTED, LoanStatus.APPROVED, LoanStatus.DISBURSED)
DEFAULT_LOAN_CAP = 0
DEFAULT_SAVINGS_MULTIPLE = 3
DEFAULT_REQUIRED_APPROVALS = 2


@dataclass(frozen=True)
class LoanView:
    id: int
    sacco_id: int
    membership_id: int
    principal: int
    term_months: int | None
    required_approvals: int
    approvals_count: int
    repaid_total: int
    status: str
    disbursed_by_membership_id: int | None
    disbursed_at: datetime | None
    disbursement_transaction_id: int | None
    created_at: datetime
    sector: str | None = None
    disclosure_acknowledged_at: datetime | None = None


@dataclass(frozen=True)
class RepaymentView:
    repayment_id: int
    loan_id: int
    transaction_id: int
    amount: int
    repaid_total: int
    loan_status: str
    account_balance: int
    reference: str
    created_at: datetime


def _membership(user_id: int, sacco_id: int) -> SaccoMembership:
    m = db.session.execute(
        select(SaccoMembership).where(
            SaccoMembership.user_id == user_id,
            SaccoMembership.sacco_id == sacco_id,
        )
    ).scalar_one_or_none()
    if m is None:
        raise NotFound("Not found.")
    if m.status is not MembershipStatus.ACTIVE:
        raise Forbidden("Membership is not active.")
    return m


def _require_active_borrower(loan: Loan) -> None:
    """Money moves on a deactivated member's account only through reversals."""
    borrower = db.session.get(SaccoMembership, loan.membership_id)
    if borrower.status is not MembershipStatus.ACTIVE:
        raise Conflict("The borrower's membership is not active.")


def _lock_loan(loan_id: int) -> Loan:
    loan = db.session.execute(
        select(Loan).where(Loan.id == loan_id).with_for_update()
    ).scalar_one_or_none()
    if loan is None:
        raise NotFound("Not found.")
    return loan


def _loan_view(loan: Loan) -> LoanView:
    return _loan_views([loan])[0]


def _loan_views(loans: list[Loan]) -> list[LoanView]:
    """Views for many loans: two grouped queries instead of two per loan."""
    ids = [loan.id for loan in loans]
    approvals: dict[int, int] = {}
    repaid: dict[int, int] = {}
    if ids:
        approvals = dict(
            db.session.execute(
                select(LoanDecision.loan_id, func.count())
                .where(
                    LoanDecision.loan_id.in_(ids),
                    LoanDecision.decision == LoanDecisionKind.APPROVE,
                )
                .group_by(LoanDecision.loan_id)
            ).all()
        )
        repaid = dict(
            db.session.execute(
                select(LoanRepayment.loan_id, func.sum(LoanRepayment.amount))
                .where(LoanRepayment.loan_id.in_(ids))
                .group_by(LoanRepayment.loan_id)
            ).all()
        )
    return [
        LoanView(
            id=loan.id,
            sacco_id=loan.sacco_id,
            membership_id=loan.membership_id,
            principal=loan.principal,
            term_months=loan.term_months,
            required_approvals=loan.required_approvals,
            approvals_count=int(approvals.get(loan.id, 0)),
            repaid_total=int(repaid.get(loan.id, 0)),
            status=loan.status.value,
            disbursed_by_membership_id=loan.disbursed_by_membership_id,
            disbursed_at=loan.disbursed_at,
            disbursement_transaction_id=loan.disbursement_transaction_id,
            created_at=loan.created_at,
            sector=loan.sector,
            disclosure_acknowledged_at=loan.disclosure_acknowledged_at,
        )
        for loan in loans
    ]


@_atomic
def request_loan(
    user_id: int,
    sacco_id: int,
    principal: int,
    term_months: int | None = None,
    sector: str | None = None,
    disclosure_acknowledged: bool = False,
) -> LoanView:
    user = db.session.get(User, user_id)
    actor = _membership(user_id, sacco_id)
    _require_sacco_active(sacco_id)
    if not can(user, sacco_id, "loan.request"):
        raise Forbidden("You are not allowed to request a loan.")

    account = db.session.execute(
        select(SavingsAccount).where(SavingsAccount.membership_id == actor.id)
    ).scalar_one_or_none()
    if account is None:
        raise Conflict(
            "An active savings account is required before requesting a loan."
        )

    open_loan = db.session.execute(
        select(Loan.id).where(
            Loan.membership_id == actor.id, Loan.status.in_(OPEN_STATUSES)
        )
    ).first()
    if open_loan is not None:
        raise Conflict("You already have an open loan.")

    check_loan_request(actor, account.balance, principal, term_months)

    # Read only: a SACCO without a settings row runs on the defaults.
    settings = db.session.get(SaccoSettings, sacco_id)
    cap = settings.loan_cap if settings else DEFAULT_LOAN_CAP
    multiple = settings.savings_multiple if settings else DEFAULT_SAVINGS_MULTIPLE
    above = settings.required_approvals if settings else DEFAULT_REQUIRED_APPROVALS
    # Within the cap and the savings multiple: one approval. Above either: the
    # SACCO's higher count. A cap of 0 sends every loan to the higher count.
    within = principal <= cap and principal <= multiple * account.balance
    required_approvals = 1 if within else above

    loan = Loan(
        sacco_id=sacco_id,
        membership_id=actor.id,
        principal=principal,
        term_months=term_months,
        required_approvals=required_approvals,
        status=LoanStatus.REQUESTED,
        requested_by_membership_id=actor.id,
        sector=sector,
        disclosure_acknowledged_at=datetime.now(UTC)
        if disclosure_acknowledged
        else None,
    )
    db.session.add(loan)
    try:
        db.session.flush()
    except IntegrityError as exc:
        if _constraint(exc) == "uq_loans_one_open_per_member":
            raise Conflict("You already have an open loan.") from exc
        raise
    db.session.refresh(loan)

    audit_log.record(sacco_id, user_id, "loan.request", f"loan:{loan.id}")
    view = _loan_view(loan)
    db.session.commit()
    return view


@_atomic
def decide_loan(
    user_id: int,
    loan_id: int,
    step: int,
    decision: LoanDecisionKind,
    reason: str | None = None,
) -> LoanView:
    loan = _lock_loan(loan_id)
    user = db.session.get(User, user_id)
    actor = _membership(user_id, loan.sacco_id)
    _require_sacco_active(loan.sacco_id)

    if not can(user, loan.sacco_id, "loan.approve"):
        raise Forbidden("You are not allowed to approve or reject loans.")
    if actor.id == loan.membership_id:
        raise Forbidden("You cannot approve or reject your own loan.")
    if loan.status is not LoanStatus.REQUESTED:
        raise Conflict(
            f"Cannot record a decision when loan status is {loan.status.value}."
        )
    if decision is LoanDecisionKind.REJECT and not (reason or "").strip():
        raise Conflict("A rejection needs a reason.")

    existing_decisions = (
        db.session.execute(
            select(LoanDecision)
            .where(LoanDecision.loan_id == loan.id)
            .order_by(LoanDecision.step)
        )
        .scalars()
        .all()
    )
    if any(d.decided_by_membership_id == actor.id for d in existing_decisions):
        raise Forbidden("You have already recorded a decision on this loan.")

    expected_step = len(existing_decisions) + 1
    if step != expected_step or step > loan.required_approvals:
        raise Conflict(
            f"Expected step {expected_step} (max {loan.required_approvals})."
        )

    row = LoanDecision(
        loan_id=loan.id,
        step=step,
        decision=decision,
        decided_by_membership_id=actor.id,
        reason=reason,
    )
    db.session.add(row)
    try:
        db.session.flush()
    except IntegrityError as exc:
        if _constraint(exc) == "uq_loan_decisions_loan_step":
            raise Conflict("This approval step has already been recorded.") from exc
        raise

    if decision is LoanDecisionKind.REJECT:
        loan.status = LoanStatus.REJECTED
    elif step == loan.required_approvals:
        loan.status = LoanStatus.APPROVED

    audit_log.record(
        loan.sacco_id,
        user_id,
        f"loan.{decision.value}",
        f"loan:{loan.id}/step:{step}",
    )
    view = _loan_view(loan)
    db.session.commit()
    return view


@_atomic
def disburse_loan(user_id: int, loan_id: int, reference: str) -> LoanView:
    loan = _lock_loan(loan_id)
    user = db.session.get(User, user_id)
    actor = _membership(user_id, loan.sacco_id)
    _require_sacco_active(loan.sacco_id)

    _require_active_borrower(loan)
    if not can(user, loan.sacco_id, "loan.disburse"):
        raise Forbidden("Only a finance officer can disburse loans.")
    if actor.id == loan.membership_id:
        raise Forbidden("You cannot disburse your own loan.")

    approvals = (
        db.session.execute(
            select(LoanDecision).where(
                LoanDecision.loan_id == loan.id,
                LoanDecision.decision == LoanDecisionKind.APPROVE,
            )
        )
        .scalars()
        .all()
    )
    if (
        loan.status is not LoanStatus.APPROVED
        or len(approvals) < loan.required_approvals
    ):
        raise Conflict(
            "Loan does not have the required approvals.",
            code=LOAN_NEEDS_APPROVAL,
        )

    approver_ids = {d.decided_by_membership_id for d in approvals}
    if actor.id in approver_ids:
        raise Forbidden(
            "Disbursing officer must be different from the approving officers.",
            code=LOAN_SAME_OFFICER,
        )

    existing_tx = db.session.execute(
        select(Transaction).where(
            Transaction.sacco_id == loan.sacco_id,
            Transaction.reference == reference,
        )
    ).scalar_one_or_none()
    if existing_tx is not None:
        raise Conflict(
            "That reference has already been used.", code=DUPLICATE_REFERENCE
        )

    # SACCO lock first, then the account (withdrawals take them in the same
    # order): a withdrawal or another payout cannot slip in between the checks
    # and the credit.
    lock_sacco(loan.sacco_id)
    account = db.session.execute(
        select(SavingsAccount)
        .where(SavingsAccount.membership_id == loan.membership_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    check_loan_disburse(loan, account.balance)

    tx = Transaction(
        sacco_id=loan.sacco_id,
        savings_account_id=account.id,
        type=TransactionType.DEPOSIT,
        amount=loan.principal,
        reference=reference,
        created_by_membership_id=actor.id,
    )
    db.session.add(tx)
    try:
        db.session.flush()
    except IntegrityError as exc:
        if _constraint(exc) == "uq_transactions_sacco_reference":
            raise Conflict(
                "That reference has already been used.", code=DUPLICATE_REFERENCE
            ) from exc
        raise

    loan.status = LoanStatus.DISBURSED
    loan.disbursed_by_membership_id = actor.id
    loan.disbursed_at = func.now()
    loan.disbursement_transaction_id = tx.id
    db.session.flush()
    db.session.refresh(loan)

    audit_log.record(
        loan.sacco_id,
        user_id,
        "loan.disburse",
        f"loan:{loan.id}/transaction:{tx.id}",
    )
    view = _loan_view(loan)
    db.session.commit()
    return view


@_atomic
def repay_loan(
    user_id: int, loan_id: int, amount: int, reference: str
) -> RepaymentView:
    loan = _lock_loan(loan_id)
    user = db.session.get(User, user_id)
    actor = _membership(user_id, loan.sacco_id)
    _require_sacco_active(loan.sacco_id)

    _require_active_borrower(loan)
    if not can(user, loan.sacco_id, "loan.repay"):
        raise Forbidden("You are not allowed to repay loans.")
    if actor.id != loan.membership_id and not can(user, loan.sacco_id, "loan.view"):
        raise NotFound("Not found.")
    if loan.status is not LoanStatus.DISBURSED:
        raise Conflict("Only disbursed loans can be repaid.")

    existing_tx = db.session.execute(
        select(Transaction).where(
            Transaction.sacco_id == loan.sacco_id,
            Transaction.reference == reference,
        )
    ).scalar_one_or_none()
    if existing_tx is not None:
        raise Conflict(
            "That reference has already been used.", code=DUPLICATE_REFERENCE
        )

    repaid_so_far = int(
        db.session.execute(
            select(func.coalesce(func.sum(LoanRepayment.amount), 0)).where(
                LoanRepayment.loan_id == loan.id
            )
        ).scalar_one()
    )
    remaining = loan.principal - repaid_so_far
    if amount > remaining:
        raise Conflict(f"Repayment exceeds remaining loan balance ({remaining}).")

    account = db.session.execute(
        select(SavingsAccount)
        .where(SavingsAccount.membership_id == loan.membership_id)
        .with_for_update()
    ).scalar_one()

    if amount > account.balance:
        raise Conflict(
            "Insufficient funds in savings account.", code=INSUFFICIENT_FUNDS
        )

    tx = Transaction(
        sacco_id=loan.sacco_id,
        savings_account_id=account.id,
        type=TransactionType.WITHDRAWAL,
        amount=amount,
        reference=reference,
        created_by_membership_id=actor.id,
    )
    db.session.add(tx)
    try:
        db.session.flush()
    except IntegrityError as exc:
        name = _constraint(exc)
        if name == "uq_transactions_sacco_reference":
            raise Conflict(
                "That reference has already been used.", code=DUPLICATE_REFERENCE
            ) from exc
        if name == "ck_savings_accounts_balance_non_negative":
            raise Conflict(
                "Insufficient funds in savings account.", code=INSUFFICIENT_FUNDS
            ) from exc
        raise

    repayment = LoanRepayment(
        loan_id=loan.id,
        transaction_id=tx.id,
        amount=amount,
    )
    db.session.add(repayment)

    new_repaid_total = repaid_so_far + amount
    if new_repaid_total == loan.principal:
        loan.status = LoanStatus.REPAID

    db.session.flush()
    db.session.refresh(tx)
    db.session.refresh(repayment)
    db.session.refresh(account)

    audit_log.record(
        loan.sacco_id,
        user_id,
        "loan.repay",
        f"loan:{loan.id}/repayment:{repayment.id}/transaction:{tx.id}",
    )
    view = RepaymentView(
        repayment_id=repayment.id,
        loan_id=loan.id,
        transaction_id=tx.id,
        amount=repayment.amount,
        repaid_total=new_repaid_total,
        loan_status=loan.status.value,
        account_balance=account.balance,
        reference=tx.reference,
        created_at=repayment.created_at,
    )
    db.session.commit()
    return view
