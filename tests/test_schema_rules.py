"""The agreed rules in docs/roles-permissions.md, one test per rule.

Each test names the schema rule it holds the application to.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from app.auth.tokens import create_access_token
from app.extensions import db
from app.loans import service as loans
from app.models.loan import LoanDecisionKind, SaccoSettings
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.models.user import User
from app.security.permissions import can
from savings_support import (  # noqa: F401
    Setup,
    manual_body,
    release_session,
    schema_app,
)

pytestmark = pytest.mark.schema


class Ref:
    def __init__(self, user):
        self.id, self.token_version = user.id, user.token_version


def auth(user):
    token = create_access_token(user.id, user.token_version)
    return {"Authorization": f"Bearer {token}"}


def make_user(owner=False):
    user = User(
        email=f"rule-{uuid.uuid4().hex[:10]}@example.com",
        password_hash="x",
        is_platform_owner=owner,
        email_verified_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    ref = Ref(user)
    db.session.remove()
    return ref


def officer(s, *titles):
    """A legacy-staff membership holding exactly these job titles."""
    from app.models.access import MembershipRoleLink, Role

    user = make_user()
    membership = SaccoMembership(
        user_id=user.id, sacco_id=s.sacco.id, role=MembershipRole.STAFF
    )
    db.session.add(membership)
    db.session.flush()
    for code in titles:
        role_id = db.session.query(Role.id).filter_by(code=code).scalar()
        db.session.add(MembershipRoleLink(membership_id=membership.id, role_id=role_id))
    db.session.commit()
    db.session.remove()
    return user


def settings(s, **values):
    db.session.add(SaccoSettings(sacco_id=s.sacco.id, **values))
    db.session.commit()
    db.session.remove()


# ---- who may do what ---------------------------------------------------------


def test_job_titles_hold_exactly_the_schema_permissions(schema_app):  # noqa: F811
    s = Setup()
    secretary = officer(s, "secretary")
    finance = officer(s, "finance_officer")
    loans_officer = officer(s, "loans_officer")

    def may(user, permission):
        return can(db.session.get(User, user.id), s.sacco.id, permission)

    assert may(s.admin, "member.manage")
    assert not may(s.admin, "loan.approve")  # admin: never approves loans
    assert not may(s.admin, "audit.view")  # admin: cannot read the audit log
    assert may(secretary, "audit.view")  # secretary: the SACCO's audit reader
    assert not may(secretary, "savings.post")
    assert not may(secretary, "member.manage")
    assert may(finance, "savings.post") and may(finance, "loan.disburse")
    assert not may(finance, "loan.approve")
    assert may(loans_officer, "loan.approve")
    assert not may(loans_officer, "loan.disburse")
    assert may(s.member, "loan.request")
    assert not may(s.member, "member.view")


def test_the_secretary_reads_members_but_cannot_add_them(schema_app):  # noqa: F811
    s = Setup()
    secretary = officer(s, "secretary")
    client = schema_app.test_client()
    url = f"/api/v1/saccos/{s.sacco.id}/members"
    assert client.get(url, headers=auth(secretary)).status_code == 200
    added = client.post(
        url, json={"phone": "+254700000123"}, headers=auth(secretary)
    )
    assert added.status_code == 403
    assert client.get(url, headers=auth(s.member)).status_code == 403


# ---- loan rules ----------------------------------------------------------------


def test_within_cap_and_savings_multiple_needs_one_approval(schema_app):  # noqa: F811
    s = Setup(balance=10_000)
    settings(s, loan_cap=50_000, savings_multiple=3, required_approvals=2)
    lo = officer(s, "loans_officer")
    loan = loans.request_loan(s.member.id, s.sacco.id, 20_000, 6)
    assert loan.required_approvals == 1
    approved = loans.decide_loan(lo.id, loan.id, 1, LoanDecisionKind.APPROVE)
    assert approved.status == "approved"


@pytest.mark.parametrize(
    ("values", "principal"),
    [
        ({}, 1_000),  # no settings row: cap 0, every loan takes the higher count
        ({"loan_cap": 50_000, "savings_multiple": 3}, 40_000),  # above 3x savings
        ({"loan_cap": 5_000, "savings_multiple": 3}, 6_000),  # above the cap
    ],
)
def test_above_either_limit_needs_the_higher_count_and_is_not_refused(
    schema_app,  # noqa: F811
    values,
    principal,
):
    s = Setup(balance=10_000)
    if values:
        settings(s, **values)
    loan = loans.request_loan(s.member.id, s.sacco.id, principal, 6)
    assert (loan.status, loan.required_approvals) == ("requested", 2)


def test_one_open_loan_per_member_until_it_ends(schema_app):  # noqa: F811
    s = Setup(balance=10_000)
    lo = officer(s, "loans_officer")
    first = loans.request_loan(s.member.id, s.sacco.id, 1_000, 6)
    with pytest.raises(loans.Conflict):
        loans.request_loan(s.member.id, s.sacco.id, 500, 3)
    loans.decide_loan(lo.id, first.id, 1, LoanDecisionKind.REJECT, "Too soon")
    # A rejected loan is no longer open, so the member may ask again.
    assert loans.request_loan(s.member.id, s.sacco.id, 500, 3).status == "requested"


def test_the_database_holds_one_open_loan_per_member(schema_app):  # noqa: F811
    s = Setup(balance=10_000)
    loans.request_loan(s.member.id, s.sacco.id, 1_000, 6)
    with pytest.raises(Exception, match="uq_loans_one_open_per_member"):
        db.session.execute(
            text(
                "INSERT INTO loans (sacco_id, membership_id, principal, "
                "required_approvals, term_months) VALUES (:s, :m, 5, 1, 1)"
            ),
            {"s": s.sacco.id, "m": s.member_membership.id},
        )
    db.session.rollback()


def test_a_rejection_needs_a_reason(schema_app):  # noqa: F811
    s = Setup(balance=10_000)
    lo = officer(s, "loans_officer")
    loan = loans.request_loan(s.member.id, s.sacco.id, 1_000, 6)
    client = schema_app.test_client()
    url = f"/api/v1/loans/{loan.id}/decisions"
    for reason in (None, "", "   "):
        body = {"step": 1, "decision": "reject"}
        if reason is not None:
            body["reason"] = reason
        assert client.post(url, json=body, headers=auth(lo)).status_code == 400
    with pytest.raises(loans.Conflict):
        loans.decide_loan(lo.id, loan.id, 1, LoanDecisionKind.REJECT, "  ")
    done = client.post(
        url,
        json={"step": 1, "decision": "reject", "reason": "Savings too low"},
        headers=auth(lo),
    )
    assert done.status_code == 201 and done.json["status"] == "rejected"
    got = client.get(f"/api/v1/loans/{loan.id}", headers=auth(s.member))
    assert got.json["decisions"][0]["reason"] == "Savings too low"


@pytest.mark.parametrize("term", [None, 0, -1, 361, True, "6"])
def test_a_loan_request_needs_a_term_in_months(schema_app, term):  # noqa: F811
    s = Setup(balance=10_000)
    body = {"principal": 1_000}
    if term is not None:
        body["term_months"] = term
    resp = schema_app.test_client().post(
        f"/api/v1/loans/saccos/{s.sacco.id}", json=body, headers=auth(s.member)
    )
    assert resp.status_code == 400


@pytest.mark.parametrize("table", ["loan_decisions", "loan_repayments"])
def test_loan_history_is_append_only(schema_app, table):  # noqa: F811
    s = Setup(balance=10_000)
    lo = officer(s, "loans_officer")
    finance = officer(s, "finance_officer")
    loan = loans.request_loan(s.member.id, s.sacco.id, 1_000, 6)
    settings_one = loans.decide_loan(lo.id, loan.id, 1, LoanDecisionKind.APPROVE)
    if settings_one.status != "approved":
        lo2 = officer(s, "loans_officer")
        loans.decide_loan(lo2.id, loan.id, 2, LoanDecisionKind.APPROVE)
    loans.disburse_loan(finance.id, loan.id, "disb-ao")
    loans.repay_loan(s.member.id, loan.id, 100, "rep-ao")
    for statement in (f"UPDATE {table} SET id = id", f"DELETE FROM {table}"):
        with pytest.raises(Exception, match="append only"):
            db.session.execute(text(statement))
        db.session.rollback()


# ---- memberships ---------------------------------------------------------------


def test_membership_transitions_follow_the_schema(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    member_url = f"/api/v1/saccos/{s.sacco.id}/members/{s.member_membership.id}"
    back = client.patch(member_url, json={"status": "invited"}, headers=auth(s.admin))
    assert back.status_code == 409  # active cannot go back to invited

    invited = client.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"phone": "+254700000777"},
        headers=auth(s.admin),
    )
    assert invited.status_code == 201
    url = f"/api/v1/saccos/{s.sacco.id}/members/{invited.json['id']}"
    skip = client.patch(url, json={"status": "active"}, headers=auth(s.admin))
    assert skip.status_code == 409  # only joining with the code activates it
    gone = client.patch(url, json={"status": "inactive"}, headers=auth(s.admin))
    assert gone.status_code == 200
    m = db.session.get(SaccoMembership, invited.json["id"])
    assert m.status is MembershipStatus.INACTIVE


# ---- platform owners -------------------------------------------------------------


def test_platform_owner_rules(schema_app):  # noqa: F811
    db.session.execute(text("UPDATE users SET is_platform_owner = false"))
    db.session.execute(text("DELETE FROM platform_grants"))
    db.session.commit()
    first, second, third = (make_user(owner=True) for _ in range(3))
    candidate = make_user()
    client = schema_app.test_client()
    s = Setup()

    listed = client.get("/api/v1/admin/owners", headers=auth(first))
    assert [o["user_id"] for o in listed.json["owners"]] == [
        first.id, second.id, third.id
    ]
    assert client.get("/api/v1/admin/owners", headers=auth(s.admin)).status_code == 403

    # Only an owner grants; the user must exist and not be an owner already.
    email = db.session.get(User, candidate.id).email
    db.session.remove()
    owners_url = "/api/v1/admin/owners"
    no = client.post(owners_url, json={"email": email}, headers=auth(s.admin))
    assert no.status_code == 403
    yes = client.post(owners_url, json={"email": email}, headers=auth(first))
    assert yes.status_code == 201
    again = client.post(
        "/api/v1/admin/owners", json={"email": email}, headers=auth(first)
    )
    assert again.status_code == 409

    # Nobody revokes themselves.
    own = client.delete(f"/api/v1/admin/owners/{first.id}", headers=auth(first))
    assert own.status_code == 403
    # Four owners: two revokes are fine, the third would leave one.
    for target in (candidate, third):
        resp = client.delete(f"/api/v1/admin/owners/{target.id}", headers=auth(first))
        assert resp.status_code == 200, resp.json
    last = client.delete(f"/api/v1/admin/owners/{second.id}", headers=auth(first))
    assert last.status_code == 409

    # A revoked owner loses platform access at once.
    assert client.get("/api/v1/admin/owners", headers=auth(third)).status_code == 403
    assert client.get("/api/v1/saccos", headers=auth(third)).status_code == 403
    unknown = client.delete("/api/v1/admin/owners/999999", headers=auth(first))
    assert unknown.status_code == 404


# ---- found by the second red-team review ------------------------------------------


def test_a_phone_invitation_never_becomes_active_without_joining(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    invited = client.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"phone": "+254700000888"},
        headers=auth(s.admin),
    )
    url = f"/api/v1/saccos/{s.sacco.id}/members/{invited.json['id']}"
    to_inactive = client.patch(url, json={"status": "inactive"}, headers=auth(s.admin))
    assert to_inactive.status_code == 200
    # invited -> inactive -> active would skip the join code.
    back = client.patch(url, json={"status": "active"}, headers=auth(s.admin))
    assert back.status_code == 409


def test_an_admin_never_approves_or_reads_the_audit_log_whatever_titles(
    schema_app,  # noqa: F811
):
    from app.models.access import MembershipRoleLink, Role

    s = Setup(balance=10_000)
    # Titles attached straight in the database, past every route.
    for code in ("loans_officer", "secretary"):
        role_id = db.session.query(Role.id).filter_by(code=code).scalar()
        db.session.add(
            MembershipRoleLink(membership_id=s.admin_membership.id, role_id=role_id)
        )
    db.session.commit()
    admin = db.session.get(User, s.admin.id)
    assert not can(admin, s.sacco.id, "loan.approve")
    assert not can(admin, s.sacco.id, "audit.view")
    db.session.remove()
    loan = loans.request_loan(s.member.id, s.sacco.id, 1_000, 6)
    with pytest.raises(loans.Forbidden):
        loans.decide_loan(s.admin.id, loan.id, 1, LoanDecisionKind.APPROVE)
    audit = schema_app.test_client().get(
        f"/api/v1/admin/saccos/{s.sacco.id}/audit", headers=auth(s.admin)
    )
    assert audit.status_code == 403


def test_an_admin_holds_no_other_job_title(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    boss = auth(make_user(owner=True))
    url = f"/api/v1/saccos/{s.sacco.id}/members/{s.admin_membership.id}/roles"
    for extra in ("loans_officer", "secretary", "finance_officer"):
        resp = client.put(url, json={"roles": ["sacco_admin", extra]}, headers=boss)
        assert resp.status_code == 409, extra
    # A secretary promoted to admin keeps only sacco_admin.
    secretary = officer(s, "secretary")
    mid = (
        db.session.query(SaccoMembership.id).filter_by(user_id=secretary.id).scalar()
    )
    db.session.remove()
    promoted = client.patch(
        f"/api/v1/saccos/{s.sacco.id}/members/{mid}",
        json={"role": "sacco_admin"},
        headers=boss,
    )
    assert promoted.status_code == 200
    titles = client.get(
        f"/api/v1/saccos/{s.sacco.id}/members/{mid}/roles", headers=auth(s.admin)
    )
    assert titles.json["roles"] == ["sacco_admin"]


def _disbursed_loan(s, reference):
    lo, lo2 = officer(s, "loans_officer"), officer(s, "loans_officer")
    finance = officer(s, "finance_officer")
    loan = loans.request_loan(s.member.id, s.sacco.id, 1_000, 6)
    if loans.decide_loan(lo.id, loan.id, 1, LoanDecisionKind.APPROVE).status != (
        "approved"
    ):
        loans.decide_loan(lo2.id, loan.id, 2, LoanDecisionKind.APPROVE)
    done = loans.disburse_loan(finance.id, loan.id, reference)
    return loan.id, done.disbursement_transaction_id, finance


def test_a_disbursement_without_its_link_is_still_not_reversible(schema_app):  # noqa: F811
    from app.ledger import reversals

    s = Setup(balance=10_000)
    loan_id, tx_id, finance = _disbursed_loan(s, "disb-nolink")
    # The state a loan disbursed before migration 0017 is in. The database no
    # longer lets anyone clear the link, so the guard is lifted for the setup.
    db.session.execute(text("ALTER TABLE loans DISABLE TRIGGER trg_loans_guard_update"))
    db.session.execute(
        text("UPDATE loans SET disbursement_transaction_id = NULL WHERE id = :i"),
        {"i": loan_id},
    )
    db.session.execute(text("ALTER TABLE loans ENABLE TRIGGER trg_loans_guard_update"))
    db.session.commit()
    db.session.remove()
    with pytest.raises(reversals.Conflict, match="Loan disbursements"):
        reversals.reverse_transaction(s.admin.id, tx_id, "undo-nolink")


def test_0018_upgrades_a_database_with_old_rejections_and_disbursements(
    schema_app,  # noqa: F811
):
    from flask_migrate import downgrade, upgrade

    from test_schema import MIGRATIONS

    s = Setup(balance=10_000)
    loan_id, tx_id, _ = _disbursed_loan(s, "disb-old")
    lo = officer(s, "loans_officer")
    neighbour = officer(s, "member")
    from app.models.savings import SavingsAccount  # noqa: F401
    from app.savings import service as savings

    mid = db.session.query(SaccoMembership.id).filter_by(user_id=neighbour.id).scalar()
    db.session.remove()
    savings.open_account(s.admin.id, mid)
    rejected = loans.request_loan(neighbour.id, s.sacco.id, 1, 1)
    loans.decide_loan(lo.id, rejected.id, 1, LoanDecisionKind.REJECT, "x")
    db.session.remove()

    downgrade(directory=MIGRATIONS, revision="0017")  # reason column gone
    db.session.execute(
        text("UPDATE loans SET disbursement_transaction_id = NULL WHERE id = :i"),
        {"i": loan_id},
    )
    db.session.commit()
    upgrade(directory=MIGRATIONS, revision="head")

    link = db.session.execute(
        text("SELECT disbursement_transaction_id FROM loans WHERE id = :i"),
        {"i": loan_id},
    ).scalar()
    reason = db.session.execute(
        text("SELECT reason FROM loan_decisions WHERE loan_id = :i"),
        {"i": rejected.id},
    ).scalar()
    db.session.remove()
    assert link == tx_id
    assert reason == "No reason recorded (before migration 0018)"


def test_two_revokes_at_once_still_leave_two_owners(schema_app):  # noqa: F811
    import threading

    from app.admin.owners import OWNERS_LOCK

    db.session.execute(text("UPDATE users SET is_platform_owner = false"))
    db.session.execute(text("DELETE FROM platform_grants"))
    db.session.commit()
    a, b = (make_user(owner=True) for _ in range(2))
    c = make_user()
    # C is an owner through a grant row only: row locks on users never see
    # C's revoke, only the owners lock does.
    db.session.execute(
        text(
            "INSERT INTO platform_grants (user_id, granted_by_user_id) "
            "VALUES (:c, :a)"
        ),
        {"c": c.id, "a": a.id},
    )
    db.session.commit()
    db.session.remove()

    # Revoke C by hand inside the owners lock, and hold the transaction open.
    first = db.engine.connect()
    held = first.begin()
    first.execute(text(OWNERS_LOCK))
    first.execute(text("DELETE FROM platform_grants WHERE user_id = :i"), {"i": c.id})

    result = {}

    def b_revokes_a():
        with schema_app.app_context():
            client = schema_app.test_client()
            resp = client.delete(f"/api/v1/admin/owners/{a.id}", headers=auth(b))
            result["status"] = resp.status_code
            db.session.remove()

    thread = threading.Thread(target=b_revokes_a)
    thread.start()
    thread.join(1.5)
    assert thread.is_alive(), "the revoke did not wait for the owners lock"
    held.commit()
    first.close()
    thread.join(15)
    # With C gone, revoking A would leave only B.
    assert result["status"] == 409
    owners = db.session.execute(
        text(
            "SELECT count(*) FROM users WHERE is_platform_owner "
            "OR id IN (SELECT user_id FROM platform_grants)"
        )
    ).scalar()
    db.session.remove()
    assert owners == 2


# ---- found by the third red-team review ---------------------------------------------


def test_any_role_change_drops_the_old_roles_titles(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    staff = officer(s, "secretary", "loans_officer")
    mid = db.session.query(SaccoMembership.id).filter_by(user_id=staff.id).scalar()
    db.session.remove()
    demoted = client.patch(
        f"/api/v1/saccos/{s.sacco.id}/members/{mid}",
        json={"role": "member"},
        headers=auth(s.admin),
    )
    assert demoted.status_code == 200 and demoted.json["role"] == "member"
    user = db.session.get(User, staff.id)
    assert not can(user, s.sacco.id, "loan.approve")
    assert not can(user, s.sacco.id, "audit.view")
    assert can(user, s.sacco.id, "loan.request")  # now an ordinary member
    db.session.remove()
    audit = client.get(f"/api/v1/admin/saccos/{s.sacco.id}/audit", headers=auth(staff))
    assert audit.status_code == 403


def test_a_nul_character_is_a_400_not_a_500(schema_app):  # noqa: F811
    s = Setup(balance=10_000)
    lo = officer(s, "loans_officer")
    loan = loans.request_loan(s.member.id, s.sacco.id, 1_000, 6)
    client = schema_app.test_client()
    reject = client.post(
        f"/api/v1/loans/{loan.id}/decisions",
        json={"step": 1, "decision": "reject", "reason": "bad\x00reason"},
        headers=auth(lo),
    )
    assert reject.status_code == 400
    grant = client.post(
        "/api/v1/admin/owners",
        json={"email": "x\x00@example.com"},
        headers=auth(make_user(owner=True)),
    )
    assert grant.status_code == 400


def test_an_owner_revoked_while_waiting_cannot_still_grant(schema_app):  # noqa: F811
    import threading

    from app.admin.owners import OWNERS_LOCK

    a, b, c = (make_user(owner=True) for _ in range(3))
    newcomer = make_user()
    email = db.session.get(User, newcomer.id).email
    db.session.remove()

    held_conn = db.engine.connect()
    held = held_conn.begin()
    held_conn.execute(text(OWNERS_LOCK))
    result = {}

    def b_grants():
        with schema_app.app_context():
            resp = schema_app.test_client().post(
                "/api/v1/admin/owners", json={"email": email}, headers=auth(b)
            )
            result["status"] = resp.status_code
            db.session.remove()

    thread = threading.Thread(target=b_grants)
    thread.start()
    thread.join(1.5)
    assert thread.is_alive(), "the grant did not wait for the owners lock"
    # A revokes B while B's grant waits (what revoke_owner writes).
    held_conn.execute(
        text("UPDATE users SET is_platform_owner = false WHERE id = :i"), {"i": b.id}
    )
    held.commit()
    held_conn.close()
    thread.join(15)
    granted = db.session.get(User, newcomer.id).is_platform_owner
    db.session.remove()
    assert result["status"] == 403 and not granted, result


# ---- locked down: nothing decided by the legacy role column ------------------------


def _staff_with(s, *titles):
    user = officer(s, *titles)
    mid = db.session.query(SaccoMembership.id).filter_by(user_id=user.id).scalar()
    db.session.remove()
    return user, mid


def test_money_moves_only_with_the_permission_not_the_staff_role(schema_app):  # noqa: F811
    from app.ledger import reversals
    from app.savings import service as savings

    s = Setup(balance=1_000)
    secretary, _ = _staff_with(s, "secretary")
    finance, _ = _staff_with(s, "finance_officer")
    _, other_mid = _staff_with(s, "member")
    for action in (
        lambda u: savings.deposit(u.id, s.account.id, 10, f"d-{u.id}"),
        lambda u: savings.withdraw(u.id, s.account.id, 10, f"w-{u.id}"),
        lambda u: savings.open_account(u.id, other_mid),
    ):
        with pytest.raises(savings.Forbidden):
            action(secretary)
    posted = savings.deposit(finance.id, s.account.id, 10, "by-finance")
    with pytest.raises(savings.Forbidden):
        reversals.reverse_transaction(secretary.id, posted.id, "rev-by-secretary")
    assert reversals.reverse_transaction(finance.id, posted.id, "rev-by-finance")


def test_reports_and_the_account_list_follow_the_schema(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    secretary, _ = _staff_with(s, "secretary")
    finance, _ = _staff_with(s, "finance_officer")
    loans_officer, _ = _staff_with(s, "loans_officer")
    base = f"/api/v1/admin/saccos/{s.sacco.id}"
    reports = (f"{base}/reports/summary", f"{base}/reports/member-activity")
    for who in (s.admin, secretary, finance, loans_officer):
        for url in reports:
            assert client.get(url, headers=auth(who)).status_code == 200, url
    for url in (*reports, f"{base}/accounts"):
        assert client.get(url, headers=auth(s.member)).status_code == 403, url
    for who in (s.admin, secretary, finance):
        assert client.get(f"{base}/accounts", headers=auth(who)).status_code == 200
    # savings.view_all is not a loans officer's (agreed schema).
    accounts = client.get(f"{base}/accounts", headers=auth(loans_officer))
    assert accounts.status_code == 403


def test_an_admin_never_disburses_even_with_an_old_finance_title(schema_app):  # noqa: F811
    from app.models.access import MembershipRoleLink, Role

    s = Setup()
    role_id = db.session.query(Role.id).filter_by(code="finance_officer").scalar()
    db.session.add(
        MembershipRoleLink(membership_id=s.admin_membership.id, role_id=role_id)
    )
    db.session.commit()
    assert not can(db.session.get(User, s.admin.id), s.sacco.id, "loan.disburse")
    db.session.remove()


def test_an_email_member_cannot_be_added_as_invited(schema_app):  # noqa: F811
    s = Setup()
    person = make_user()
    email = db.session.get(User, person.id).email
    db.session.remove()
    resp = schema_app.test_client().post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"email": email, "status": "invited"},
        headers=auth(s.admin),
    )
    assert resp.status_code == 400


def test_a_joined_invite_without_a_pin_is_not_activated_by_hand(schema_app):  # noqa: F811
    s = Setup()
    person = make_user()
    membership = SaccoMembership(
        user_id=person.id,
        sacco_id=s.sacco.id,
        phone="+254700000999",
        role=MembershipRole.MEMBER,
        status=MembershipStatus.INACTIVE,
    )
    db.session.add(membership)
    db.session.commit()
    mid = membership.id
    db.session.remove()
    client = schema_app.test_client()
    url = f"/api/v1/saccos/{s.sacco.id}/members/{mid}"
    refused = client.patch(url, json={"status": "active"}, headers=auth(s.admin))
    assert refused.status_code == 409
    db.session.get(SaccoMembership, mid).pin_hash = "set-by-the-pin-step"
    db.session.commit()
    db.session.remove()
    allowed = client.patch(url, json={"status": "active"}, headers=auth(s.admin))
    assert allowed.status_code == 200


def test_two_first_settings_writes_at_once_both_succeed(schema_app):  # noqa: F811
    import threading

    s = Setup()
    boss = make_user(owner=True)
    url = f"/api/v1/saccos/{s.sacco.id}/settings"
    barrier = threading.Barrier(2)
    statuses = []

    def write(cap):
        with schema_app.app_context():
            barrier.wait()
            resp = schema_app.test_client().patch(
                url, json={"loan_cap": cap}, headers=auth(boss)
            )
            statuses.append(resp.status_code)
            db.session.remove()

    threads = [threading.Thread(target=write, args=(cap,)) for cap in (100, 200)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert statuses == [200, 200]
    rows = db.session.execute(
        text("SELECT count(*) FROM sacco_settings WHERE sacco_id = :s"),
        {"s": s.sacco.id},
    ).scalar()
    db.session.remove()
    assert rows == 1


def test_0018_refuses_clearly_when_a_member_has_two_open_loans(
    schema_app,  # noqa: F811
    capfd,
):
    from flask_migrate import downgrade, upgrade

    from test_schema import MIGRATIONS

    s = Setup(balance=10_000)
    loans.request_loan(s.member.id, s.sacco.id, 1_000, 6)
    db.session.remove()
    downgrade(directory=MIGRATIONS, revision="0017")  # no one-open-loan index
    db.session.execute(
        text(
            "INSERT INTO loans (sacco_id, membership_id, principal, "
            "required_approvals) VALUES (:s, :m, 500, 1)"
        ),
        {"s": s.sacco.id, "m": s.member_membership.id},
    )
    db.session.commit()
    db.session.remove()
    # Flask-Migrate logs the error and exits; the message says what to fix.
    with pytest.raises(SystemExit):
        upgrade(directory=MIGRATIONS, revision="head")
    printed = capfd.readouterr().err
    assert "allows one open loan per member" in printed
    assert f"[{s.member_membership.id}]" in printed
    db.session.remove()
    db.session.execute(
        text("UPDATE loans SET status = 'rejected' WHERE principal = 500")
    )
    db.session.commit()
    db.session.remove()
    upgrade(directory=MIGRATIONS, revision="head")


def test_reading_someone_elses_account_needs_savings_view_all(schema_app):  # noqa: F811
    s = Setup(balance=100)
    client = schema_app.test_client()
    loans_officer, _ = _staff_with(s, "loans_officer")
    secretary, _ = _staff_with(s, "secretary")
    finance, _ = _staff_with(s, "finance_officer")
    account = f"/api/v1/savings/accounts/{s.account.id}"
    for url in (account, f"{account}/transactions"):
        assert client.get(url, headers=auth(s.member)).status_code == 200  # own
        assert client.get(url, headers=auth(loans_officer)).status_code == 404
        for who in (secretary, finance, s.admin):
            assert client.get(url, headers=auth(who)).status_code == 200, url


# ---- invitations: cancel, re-invite, join -------------------------------------------


def test_a_cancelled_phone_invite_can_be_invited_again(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    person = make_user()
    phone = "+254700000555"
    invited = client.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"phone": phone},
        headers=auth(s.admin),
    )
    assert invited.status_code == 201
    old_code = invited.json["join_code"]
    url = f"/api/v1/saccos/{s.sacco.id}/members/{invited.json['id']}"

    cancel = client.patch(url, json={"status": "inactive"}, headers=auth(s.admin))
    assert cancel.status_code == 200
    # A cancelled invite cannot be joined, not even with its old code.
    stale = client.post(
        "/api/v1/auth/join",
        json={"phone": phone, "code": old_code},
        headers=auth(person),
    )
    assert stale.status_code != 200

    again = client.patch(url, json={"status": "invited"}, headers=auth(s.admin))
    assert again.status_code == 200 and again.json["status"] == "invited"
    new_code = again.json["join_code"]
    assert new_code
    from app.auth.join import MAX_ATTEMPTS
    from app.models.identity import JoinCode

    rows = db.session.execute(
        select(JoinCode.attempts, JoinCode.used_at)
        .where(JoinCode.membership_id == invited.json["id"])
        .order_by(JoinCode.id)
    ).all()
    db.session.remove()
    # The first code is burned outright; only the newest one is live.
    assert rows[0].attempts == MAX_ATTEMPTS and rows[-1].attempts == 0
    # The old code stays dead; the fresh one works, then the PIN activates.
    old = client.post(
        "/api/v1/auth/join",
        json={"phone": phone, "code": old_code},
        headers=auth(person),
    )
    assert old.status_code != 200
    joined = client.post(
        "/api/v1/auth/join",
        json={"phone": phone, "code": new_code},
        headers=auth(person),
    )
    assert joined.status_code == 200, joined.json
    pinned = client.post(
        "/api/v1/auth/join/pin",
        json={"setup_token": joined.json["setup_token"], "pin": "4826"},
        headers=auth(person),
    )
    assert pinned.status_code == 200 and pinned.json["status"] == "active"


def test_only_an_inactive_phone_member_is_invited_again(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    url = f"/api/v1/saccos/{s.sacco.id}/members/{s.member_membership.id}"
    active = client.patch(url, json={"status": "invited"}, headers=auth(s.admin))
    assert active.status_code == 409  # active never goes back to invited
    client.patch(url, json={"status": "inactive"}, headers=auth(s.admin))
    no_phone = client.patch(url, json={"status": "invited"}, headers=auth(s.admin))
    assert no_phone.status_code == 409  # an email member has no phone to invite


def test_every_membership_the_api_creates_or_changes_has_a_title_row(schema_app):  # noqa: F811
    from app.models.access import MembershipRoleLink, Role

    s = Setup()
    client = schema_app.test_client()
    person = make_user()
    email = db.session.get(User, person.id).email
    db.session.remove()

    def titles(mid):
        rows = db.session.execute(
            select(Role.code)
            .join(MembershipRoleLink, MembershipRoleLink.role_id == Role.id)
            .where(MembershipRoleLink.membership_id == mid)
        ).scalars()
        result = sorted(rows)
        db.session.remove()
        return result

    added = client.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"email": email, "role": "staff"},
        headers=auth(s.admin),
    )
    assert added.status_code == 201
    assert titles(added.json["id"]) == ["secretary"]
    invited = client.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"phone": "+254700000666"},
        headers=auth(s.admin),
    )
    assert titles(invited.json["id"]) == ["member"]
    changed = client.patch(
        f"/api/v1/saccos/{s.sacco.id}/members/{added.json['id']}",
        json={"role": "member"},
        headers=auth(s.admin),
    )
    assert changed.status_code == 200
    assert titles(added.json["id"]) == ["member"]


def test_a_secretary_moves_no_money_over_http(schema_app):  # noqa: F811
    s = Setup(balance=1_000)
    client = schema_app.test_client()
    secretary, _ = _staff_with(s, "secretary")
    _, other_mid = _staff_with(s, "member")
    base = f"/api/v1/savings/accounts/{s.account.id}"
    for path in ("deposits", "withdrawals", "interest", "fees"):
        resp = client.post(
            f"{base}/{path}",
            json=manual_body(10)
            if path == "deposits"
            else {"amount": 10, "reference": f"sec-{path}"},
            headers=auth(secretary),
        )
        assert resp.status_code == 403, path
    opened = client.post(
        "/api/v1/savings/accounts",
        json={"membership_id": other_mid},
        headers=auth(secretary),
    )
    assert opened.status_code == 403


def test_a_setup_token_from_before_a_reinvite_is_dead(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    wrong_person = make_user()
    phone = "+254700000881"
    invited = client.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"phone": phone},
        headers=auth(s.admin),
    )
    mid = invited.json["id"]
    url = f"/api/v1/saccos/{s.sacco.id}/members/{mid}"
    joined = client.post(
        "/api/v1/auth/join",
        json={"phone": phone, "code": invited.json["join_code"]},
        headers=auth(wrong_person),
    )
    assert joined.status_code == 200
    stale_token = joined.json["setup_token"]
    # The admin cancels the half-done join and re-adds the phone.
    cancel = client.patch(url, json={"status": "inactive"}, headers=auth(s.admin))
    assert cancel.status_code == 200
    again = client.patch(url, json={"status": "invited"}, headers=auth(s.admin))
    assert again.status_code == 200
    pinned = client.post(
        "/api/v1/auth/join/pin",
        json={"setup_token": stale_token, "pin": "4826"},
        headers=auth(wrong_person),
    )
    status = db.session.get(SaccoMembership, mid).status
    db.session.remove()
    assert pinned.status_code == 401
    assert status is MembershipStatus.INVITED


def test_a_join_code_lives_as_long_as_the_sacco_setting(schema_app):  # noqa: F811
    from datetime import datetime as dt

    s = Setup()
    settings(s, join_code_ttl_minutes=60)
    resp = schema_app.test_client().post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"phone": "+254700000444"},
        headers=auth(s.admin),
    )
    expires = dt.fromisoformat(resp.json["expires_at"])
    minutes = (expires - dt.now(UTC)).total_seconds() / 60
    assert 55 < minutes <= 60


def test_a_re_add_starts_from_the_default_title(schema_app):  # noqa: F811
    from app.models.access import MembershipRoleLink, Role

    s = Setup()
    client = schema_app.test_client()
    invited = client.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"phone": "+254700000333"},
        headers=auth(s.admin),
    )
    mid = invited.json["id"]
    url = f"/api/v1/saccos/{s.sacco.id}/members/{mid}"
    # Suppose this member had been made a finance officer, then left.
    role_id = db.session.query(Role.id).filter_by(code="finance_officer").scalar()
    db.session.add(MembershipRoleLink(membership_id=mid, role_id=role_id))
    db.session.commit()
    db.session.remove()
    client.patch(url, json={"status": "inactive"}, headers=auth(s.admin))
    client.patch(url, json={"status": "invited"}, headers=auth(s.admin))
    titles = client.get(f"{url}/roles", headers=auth(s.admin)).json["roles"]
    assert titles == ["member"]


# ---- the database itself refuses ----------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE transactions SET amount = 1",
        "DELETE FROM transactions",
    ],
)
def test_ledger_rows_cannot_be_edited_or_deleted(schema_app, statement):  # noqa: F811
    Setup(balance=1_000)
    with pytest.raises(Exception, match="append-only"):
        db.session.execute(text(statement))
    db.session.rollback()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("principal = 1", "loan terms cannot change"),
        ("term_months = 99", "loan terms cannot change"),
        ("required_approvals = 1", "loan terms cannot change"),
        ("status = 'disbursed'", "is not allowed"),  # requested cannot skip ahead
        ("status = 'repaid'", "is not allowed"),
    ],
)
def test_loan_terms_and_steps_are_held_by_the_database(schema_app, change, message):  # noqa: F811
    s = Setup(balance=10_000)
    loan = loans.request_loan(s.member.id, s.sacco.id, 1_000, 6)
    with pytest.raises(Exception, match=message):
        db.session.execute(text(f"UPDATE loans SET {change} WHERE id = {loan.id}"))
    db.session.rollback()


def test_a_repaid_loan_cannot_go_back(schema_app):  # noqa: F811
    s = Setup(balance=10_000)
    loan_id, _, _ = _disbursed_loan(s, "disb-back")
    loans.repay_loan(s.member.id, loan_id, 1_000, "rep-back")
    for status in ("requested", "approved", "disbursed"):
        with pytest.raises(Exception, match="is not allowed"):
            db.session.execute(
                text("UPDATE loans SET status = :s WHERE id = :i"),
                {"s": status, "i": loan_id},
            )
        db.session.rollback()


# ---- release 1 follow-ups --------------------------------------------------------


def _link(membership_id, code, granted_by):
    from app.models.access import MembershipRoleLink, Role

    role_id = db.session.query(Role.id).filter_by(code=code).scalar()
    db.session.add(
        MembershipRoleLink(
            membership_id=membership_id, role_id=role_id, granted_by_user_id=granted_by
        )
    )
    db.session.commit()


def test_the_database_refuses_a_self_granted_title(schema_app):  # noqa: F811
    s = Setup()
    with pytest.raises(Exception, match="nobody grants job titles to their own"):
        _link(s.member_membership.id, "loans_officer", granted_by=s.member.id)
    db.session.rollback()
    # Someone else may grant it, and the grant records who did.
    _link(s.member_membership.id, "loans_officer", granted_by=s.admin.id)
    db.session.remove()


def test_a_platform_owner_may_grant_titles_on_their_own_membership(schema_app):  # noqa: F811
    s = Setup()
    boss = make_user(owner=True)
    membership = SaccoMembership(
        user_id=boss.id, sacco_id=s.sacco.id, role=MembershipRole.STAFF
    )
    db.session.add(membership)
    db.session.commit()
    mid = membership.id
    _link(mid, "finance_officer", granted_by=boss.id)  # owners are exempt
    db.session.remove()


def test_the_api_records_who_granted_every_title(schema_app):  # noqa: F811
    from app.models.access import MembershipRoleLink

    s = Setup()
    client = schema_app.test_client()
    invited = client.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"phone": "+254700000222"},
        headers=auth(s.admin),
    )
    mid = invited.json["id"]
    client.put(
        f"/api/v1/saccos/{s.sacco.id}/members/{mid}/roles",
        json={"roles": ["member", "loans_officer"]},
        headers=auth(s.admin),
    )
    granters = set(
        db.session.execute(
            select(MembershipRoleLink.granted_by_user_id).where(
                MembershipRoleLink.membership_id == mid
            )
        ).scalars()
    )
    db.session.remove()
    assert granters == {s.admin.id}


@pytest.mark.parametrize("order", ["first_then_second", "second_then_first"])
def test_one_phone_invited_to_two_saccos_joins_both(schema_app, order):  # noqa: F811
    first, second = Setup(), Setup()
    client = schema_app.test_client()
    person = make_user()
    # One phone per parametrised run: a phone belongs to one account for good.
    phone = "+25470000012" + ("3" if order == "first_then_second" else "5")
    codes = {}
    for s in (first, second):
        resp = client.post(
            f"/api/v1/saccos/{s.sacco.id}/members",
            json={"phone": phone},
            headers=auth(s.admin),
        )
        assert resp.status_code == 201
        codes[s] = (resp.json["id"], resp.json["join_code"])
    sequence = (first, second) if order == "first_then_second" else (second, first)
    for s in sequence:
        mid, code = codes[s]
        joined = client.post(
            "/api/v1/auth/join",
            json={"phone": phone, "code": code},
            headers=auth(person),
        )
        assert joined.status_code == 200, joined.json
        assert joined.json["membership_id"] == mid
        pinned = client.post(
            "/api/v1/auth/join/pin",
            json={"setup_token": joined.json["setup_token"], "pin": "4826"},
            headers=auth(person),
        )
        assert pinned.status_code == 200 and pinned.json["status"] == "active"


def test_a_wrong_code_counts_against_every_live_invite_for_the_phone(schema_app):  # noqa: F811
    from app.auth.join import MAX_ATTEMPTS
    from app.models.identity import JoinCode

    first, second = Setup(), Setup()
    client = schema_app.test_client()
    person = make_user()
    phone = "+254700000124"
    ids = []
    for s in (first, second):
        resp = client.post(
            f"/api/v1/saccos/{s.sacco.id}/members",
            json={"phone": phone},
            headers=auth(s.admin),
        )
        ids.append(resp.json["id"])
    for _ in range(MAX_ATTEMPTS):
        wrong = client.post(
            "/api/v1/auth/join",
            json={"phone": phone, "code": "not-a-code"},
            headers=auth(person),
        )
        assert wrong.status_code != 200
    attempts = db.session.execute(
        select(JoinCode.attempts).where(JoinCode.membership_id.in_(ids))
    ).scalars().all()
    db.session.remove()
    # Five guesses in total burn both codes: two invites give no extra guesses.
    assert attempts == [MAX_ATTEMPTS, MAX_ATTEMPTS]
