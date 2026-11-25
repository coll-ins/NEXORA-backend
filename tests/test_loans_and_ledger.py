import pytest

from app.extensions import db
from app.ledger import reversals
from app.ledger.reconcile import run_nightly_reconciliation
from app.loans import service as loan_service
from app.models.access import MembershipRoleLink, Role
from app.models.loan import LoanDecisionKind, SaccoSettings
from app.models.membership import MembershipRole, SaccoMembership
from app.models.user import User
from app.savings import service as savings_service
from app.savings.balance import verify_balance
from savings_support import Setup, release_session, schema_app  # noqa: F401

pytestmark = pytest.mark.schema


def _add_officer(sacco_id: int, email: str, job_title: str) -> User:
    user = User(email=email, password_hash="x")
    db.session.add(user)
    db.session.flush()
    m = SaccoMembership(user_id=user.id, sacco_id=sacco_id, role=MembershipRole.STAFF)
    db.session.add(m)
    db.session.flush()
    role = db.session.query(Role).filter_by(code=job_title).one()
    db.session.add(MembershipRoleLink(membership_id=m.id, role_id=role.id))
    db.session.commit()
    return user


def test_balance_matches_ledger_across_all_movement_types_and_reconcile(schema_app):  # noqa: F811
    s = Setup(balance=10_000)
    tx_int = savings_service.post_interest(s.admin.id, s.account.id, 500, "int-1")
    savings_service.post_fee(s.admin.id, s.account.id, 200, "fee-1")
    savings_service.withdraw(s.admin.id, s.account.id, 1_300, "wd-1")

    # Reverse the interest posting once; reversing a second time fails
    reversals.reverse_transaction(s.admin.id, tx_int.id, "rev-int-1")
    with pytest.raises(savings_service.Conflict):
        reversals.reverse_transaction(s.admin.id, tx_int.id, "rev-int-2")

    check = verify_balance(s.account.id)
    assert check.consistent
    assert check.stored == 8_500
    assert check.ledger == 8_500

    report = run_nightly_reconciliation(sacco_id=s.sacco.id)
    assert report.ok
    assert report.accounts_checked >= 1


def test_duplicate_reference_returns_duplicate_reference_without_posting(schema_app):  # noqa: F811
    s = Setup(balance=5_000)
    savings_service.deposit(s.admin.id, s.account.id, 1_000, "ref-unique-1")

    # The same request again is a safe retry: the original entry comes back.
    again = savings_service.deposit(s.admin.id, s.account.id, 1_000, "ref-unique-1")
    assert again.replayed

    # The same reference for a different request is refused.
    with pytest.raises(savings_service.Conflict) as exc:
        savings_service.deposit(s.admin.id, s.account.id, 2_000, "ref-unique-1")

    assert exc.value.code == "DUPLICATE_REFERENCE"
    assert verify_balance(s.account.id).stored == 6_000


def test_two_approval_loan_fails_with_one_approval(schema_app):  # noqa: F811
    s = Setup(balance=10_000)
    db.session.add(
        # Above the cap, so the SACCO's higher approval count applies.
        SaccoSettings(sacco_id=s.sacco.id, loan_cap=10_000, savings_multiple=3)
    )
    db.session.commit()

    approver_1 = _add_officer(s.sacco.id, "lo1@example.com", "loans_officer")
    finance = _add_officer(s.sacco.id, "fo1@example.com", "finance_officer")

    loan = loan_service.request_loan(s.member.id, s.sacco.id, 15_000)
    assert loan.required_approvals == 2

    loan_service.decide_loan(approver_1.id, loan.id, 1, LoanDecisionKind.APPROVE)

    with pytest.raises(loan_service.Conflict) as exc:
        loan_service.disburse_loan(finance.id, loan.id, "disb-1")
    assert exc.value.code == "LOAN_NEEDS_APPROVAL"


def test_approving_officer_cannot_disburse_same_loan_and_repayment_posts_both_rows(
    schema_app,  # noqa: F811
):
    s = Setup(balance=10_000)
    db.session.add(
        # Above the cap, so the SACCO's higher approval count applies.
        SaccoSettings(sacco_id=s.sacco.id, loan_cap=10_000, savings_multiple=3)
    )
    db.session.commit()

    approver_1 = _add_officer(s.sacco.id, "lo2@example.com", "loans_officer")
    officer_2 = _add_officer(s.sacco.id, "dual@example.com", "loans_officer")
    fo_role = db.session.query(Role).filter_by(code="finance_officer").one()
    m2 = db.session.query(SaccoMembership).filter_by(user_id=officer_2.id).one()
    db.session.add(MembershipRoleLink(membership_id=m2.id, role_id=fo_role.id))
    finance_only = _add_officer(s.sacco.id, "fo2@example.com", "finance_officer")
    db.session.commit()

    loan = loan_service.request_loan(s.member.id, s.sacco.id, 15_000)
    loan_service.decide_loan(approver_1.id, loan.id, 1, LoanDecisionKind.APPROVE)
    loan_service.decide_loan(officer_2.id, loan.id, 2, LoanDecisionKind.APPROVE)

    # Officer who approved cannot disburse
    with pytest.raises(loan_service.Forbidden) as exc:
        loan_service.disburse_loan(officer_2.id, loan.id, "disb-same-officer")
    assert exc.value.code == "LOAN_SAME_OFFICER"

    # Distinct finance officer disburses
    disbursed = loan_service.disburse_loan(finance_only.id, loan.id, "disb-ok")
    assert disbursed.status == "disbursed"
    assert verify_balance(s.account.id).stored == 25_000

    # Repayment posts ledger row + loan_repayments row together
    rep = loan_service.repay_loan(s.member.id, loan.id, 15_000, "rep-1")
    assert rep.loan_status == "repaid"
    assert rep.account_balance == 10_000
    assert verify_balance(s.account.id).consistent
