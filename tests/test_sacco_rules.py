"""SACCO rules: the loan cap check, the lock during a cycle, accept before join."""

import uuid
from datetime import UTC, datetime

import pytest

from app.auth.tokens import create_access_token
from app.extensions import db
from app.loans import service as loan_service
from app.models.rules import RulesAcceptance
from app.models.user import User
from app.saccos import rules as sacco_rules
from app.savings.service import Conflict
from savings_support import Setup, release_session, schema_app  # noqa: F401

pytestmark = pytest.mark.schema

RULES = {
    "contribution_amount": 50_000,
    "contribution_frequency": "weekly",
    "interest_percent": 10,
    "repayment_period_value": 6,
    "repayment_period_unit": "months",
    "late_fine_amount": 5_000,
}


def _auth(user):
    token = create_access_token(user.id, getattr(user, "token_version", 0))
    return {"Authorization": f"Bearer {token}"}


def test_a_loan_above_the_cap_or_the_pool_is_refused_in_plain_words(schema_app):  # noqa: F811
    s = Setup(balance=10_000)  # savings of KES 100.00
    values, errors = sacco_rules.validate_rules(RULES)
    assert not errors and values["loan_cap_multiple"] == 3  # the default
    rules = sacco_rules.publish_rules(s.sacco.id, values, s.admin.id)
    sacco_rules.accept(s.member_membership.id, rules.id)
    db.session.commit()

    # 3 x savings is KES 300.00, so KES 400.00 is over the cap.
    with pytest.raises(Conflict) as over:
        loan_service.request_loan(s.member.id, s.sacco.id, 40_000, 3)
    assert over.value.code == "LOAN_OVER_CAP", over.value.message
    assert "up to 3 times your savings" in over.value.message
    assert "KES 300.00" in over.value.message

    # Inside the cap, but the SACCO only holds KES 100.00.
    with pytest.raises(Conflict) as short:
        loan_service.request_loan(s.member.id, s.sacco.id, 20_000, 3)
    assert short.value.code == "LOAN_POOL_SHORT"

    assert (
        loan_service.request_loan(s.member.id, s.sacco.id, 10_000, 3).principal
        == 10_000
    )


def test_rules_cannot_be_changed_during_the_cycle(schema_app):  # noqa: F811
    client = schema_app.test_client()
    s = Setup()
    url = f"/api/v1/saccos/{s.sacco.id}/rules"
    admin = db.session.get(User, s.admin.id)

    first = client.post(url, json=RULES, headers=_auth(admin))
    assert first.status_code == 201, first.get_data(as_text=True)

    again = client.post(
        url, json={**RULES, "late_fine_amount": 0}, headers=_auth(admin)
    )
    assert again.status_code == 409
    assert again.get_json()["error"]["code"] == "RULES_LOCKED"
    assert "locked until" in again.get_json()["error"]["message"]

    # A change also cannot be proposed until the cycle has ended.
    early = client.post(f"{url}/proposals", json=RULES, headers=_auth(admin))
    assert early.status_code == 409


def test_a_member_must_accept_the_rules_before_joining(schema_app):  # noqa: F811
    client = schema_app.test_client()
    s = Setup()
    admin = db.session.get(User, s.admin.id)
    base = f"/api/v1/saccos/{s.sacco.id}"
    assert (
        client.post(f"{base}/rules", json=RULES, headers=_auth(admin)).status_code
        == 201
    )

    phone = "+2547" + str(uuid.uuid4().int)[:8]
    invite = client.post(f"{base}/members", json={"phone": phone}, headers=_auth(admin))
    assert invite.status_code == 201, invite.get_data(as_text=True)
    joiner = User(
        email=f"j-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        email_verified_at=datetime.now(UTC),
    )
    db.session.add(joiner)
    db.session.commit()
    me = _auth(joiner)
    joined = client.post(
        "/api/v1/auth/join",
        json={"phone": phone, "code": invite.get_json()["join_code"]},
        headers=me,
    )
    token = joined.get_json()["setup_token"]

    # No acceptance yet: the PIN step is refused.
    blocked = client.post(
        "/api/v1/auth/join/pin", json={"setup_token": token, "pin": "2468"}, headers=me
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["error"]["code"] == "RULES_NOT_ACCEPTED"

    # The member can read every rule, accept, and then finish.
    shown = client.post(
        "/api/v1/auth/join/rules", json={"setup_token": token}, headers=me
    )
    assert shown.get_json()["rules"]["interest_percent"] == 10
    # The screen names the SACCO whose invitation is being read.
    named = shown.get_json()["sacco"]
    assert named["id"] == s.sacco.id
    assert named["name"].startswith("Savings Sacco")
    accepted = client.post(
        "/api/v1/auth/join/rules/accept",
        json={"setup_token": token, "version": 1},
        headers=me,
    )
    assert accepted.status_code == 200
    done = client.post(
        "/api/v1/auth/join/pin", json={"setup_token": token, "pin": "2468"}, headers=me
    )
    assert done.status_code == 200
    # Who accepted which version is stored.
    stored = (
        db.session.query(RulesAcceptance)
        .filter_by(membership_id=invite.get_json()["id"])
        .one()
    )
    assert stored.rules_id == shown.get_json()["rules"]["id"]


def test_interest_has_no_default_and_every_error_says_what_to_send(schema_app):  # noqa: F811
    body = {k: v for k, v in RULES.items() if k != "interest_percent"}
    _, errors = sacco_rules.validate_rules(body)
    assert set(errors) == {"interest_percent"}
    assert "no default" in errors["interest_percent"]
    _, errors = sacco_rules.validate_rules(
        {**RULES, "cycle_months": 8, "late_fine_amount": -1}
    )
    assert (
        "9 to 12" in errors["cycle_months"] and "Example" in errors["late_fine_amount"]
    )


def test_after_the_cycle_a_majority_vote_adopts_the_next_version(schema_app):  # noqa: F811
    client = schema_app.test_client()
    s = Setup()
    base = f"/api/v1/saccos/{s.sacco.id}/rules"
    admin = db.session.get(User, s.admin.id)
    member = db.session.get(User, s.member.id)
    assert client.post(base, json=RULES, headers=_auth(admin)).status_code == 201
    for who in (admin, member):  # only members who accepted can vote
        accepted = client.post(
            f"{base}/accept", json={"version": 1}, headers=_auth(who)
        )
        assert accepted.status_code == 200
    rules = sacco_rules.current_rules(s.sacco.id)
    rules.cycle_ends_on = sacco_rules.today()  # the cycle has just ended
    db.session.commit()

    change = {**RULES, "late_fine_amount": 0}
    proposal = client.post(f"{base}/proposals", json=change, headers=_auth(admin))
    assert proposal.status_code == 201, proposal.get_data(as_text=True)
    url = f"{base}/proposals/{proposal.get_json()['proposal']['id']}/votes"
    one = client.post(url, json={"approve": True}, headers=_auth(admin))
    assert one.get_json()["proposal"]["status"] == "open"  # 1 of 2 is not a majority
    two = client.post(url, json={"approve": True}, headers=_auth(member))
    assert two.get_json()["proposal"]["status"] == "adopted"
    assert sacco_rules.current_rules(s.sacco.id).version == 2


def test_bad_types_are_refused_not_crashed_and_old_votes_cannot_reopen(schema_app):  # noqa: F811
    client = schema_app.test_client()
    _, errors = sacco_rules.validate_rules({**RULES, "repayment_period_unit": []})
    assert "repayment_period_value" in errors  # a list used to raise a TypeError
    _, errors = sacco_rules.validate_rules({**RULES, "exit_rules": "\ud800"})
    assert "exit_rules" in errors  # a lone surrogate would break the insert

    s = Setup()
    admin = db.session.get(User, s.admin.id)
    base = f"/api/v1/saccos/{s.sacco.id}/rules"
    assert client.post(base, json=RULES, headers=_auth(admin)).status_code == 201
    odd = client.post(f"{base}/accept", json={"version": True}, headers=_auth(admin))
    assert odd.status_code == 409  # true is not version 1
    for who in (admin, db.session.get(User, s.member.id)):  # a change needs a quorum
        assert (
            client.post(
                f"{base}/accept", json={"version": 1}, headers=_auth(who)
            ).status_code
            == 200
        )

    sacco_rules.current_rules(s.sacco.id).cycle_ends_on = sacco_rules.today()
    db.session.commit()
    first = client.post(f"{base}/proposals", json=RULES, headers=_auth(admin))
    assert first.status_code == 201
    second = client.post(f"{base}/proposals", json=RULES, headers=_auth(admin))
    assert second.status_code == 409  # one open vote per SACCO


# ---- vote: more than half of ALL active members, quorum, carried acceptance ----


def _sacco_with_rules(
    extras=0, extras_accepting=0, admin_accepts=True, member_accepts=True
):
    """A SACCO with rules v1 whose cycle has just ended, `extras` more active
    members, and the first `extras_accepting` of them (plus whoever is named)
    having accepted v1. Returns (setup, [extra users])."""
    from app.models.membership import MembershipRole, SaccoMembership
    from test_schema_rules import make_user

    s = Setup()
    users = [make_user() for _ in range(extras)]  # make_user ends the session
    values, _ = sacco_rules.validate_rules(RULES)
    rules = sacco_rules.publish_rules(s.sacco.id, values, s.admin.id)
    if admin_accepts:
        sacco_rules.accept(s.admin_membership.id, rules.id)
    if member_accepts:
        sacco_rules.accept(s.member_membership.id, rules.id)
    people = []
    for n, user in enumerate(users):
        membership = SaccoMembership(
            user_id=user.id, sacco_id=s.sacco.id, role=MembershipRole.MEMBER
        )
        db.session.add(membership)
        db.session.flush()
        if n < extras_accepting:
            sacco_rules.accept(membership.id, rules.id)
        people.append(user)
    rules.cycle_ends_on = sacco_rules.today()
    db.session.commit()
    return s, people


def _propose(client, s):
    base = f"/api/v1/saccos/{s.sacco.id}/rules"
    admin = db.session.get(User, s.admin.id)
    return base, client.post(
        f"{base}/proposals", json={**RULES, "late_fine_amount": 0}, headers=_auth(admin)
    )


def _vote(client, base, proposal_id, user, approve=True):
    user = db.session.get(User, user.id)
    return client.post(
        f"{base}/proposals/{proposal_id}/votes",
        json={"approve": approve},
        headers=_auth(user),
    ).get_json()["proposal"]


def test_a_change_needs_more_than_half_of_all_active_members_not_of_acceptors(
    schema_app,  # noqa: F811
):
    # 4 active members; 3 accepted. The old count (more than half of the 3
    # acceptors) would adopt on 2 yes. More than half of all 4 needs 3.
    s, (third, _fourth) = _sacco_with_rules(extras=2, extras_accepting=1)
    client = schema_app.test_client()
    base, made = _propose(client, s)
    assert made.status_code == 201, made.get_json()
    body = made.get_json()["proposal"]
    assert body["active_members"] == 4 and body["electorate_size"] == 3
    pid = body["id"]
    assert _vote(client, base, pid, s.admin)["status"] == "open"
    assert (
        _vote(client, base, pid, s.member)["status"] == "open"
    )  # 2 of 4 is not enough
    assert _vote(client, base, pid, third)["status"] == "adopted"  # 3 of 4
    assert sacco_rules.current_rules(s.sacco.id).version == 2


def test_the_count_is_frozen_when_the_change_is_proposed(schema_app):  # noqa: F811
    from app.models.membership import MembershipRole, SaccoMembership
    from test_schema_rules import make_user

    s, _ = _sacco_with_rules()  # 2 active members, both accepted
    client = schema_app.test_client()
    base, made = _propose(client, s)
    pid = made.get_json()["proposal"]["id"]
    for user in [make_user() for _ in range(3)]:  # joining later does not raise the bar
        db.session.add(
            SaccoMembership(
                user_id=user.id, sacco_id=s.sacco.id, role=MembershipRole.MEMBER
            )
        )
    db.session.commit()
    assert _vote(client, base, pid, s.admin)["status"] == "open"
    assert _vote(client, base, pid, s.member)["status"] == "adopted"


def test_a_proposal_with_no_voters_or_no_quorum_is_refused(schema_app):  # noqa: F811
    client = schema_app.test_client()
    s, _ = _sacco_with_rules(admin_accepts=False, member_accepts=False)
    base, none = _propose(client, s)
    assert none.status_code == 409
    assert "Nobody has accepted" in none.get_json()["error"]["message"]

    # 1 acceptor of 5 active members: it could never get more than half.
    s, _ = _sacco_with_rules(extras=3, member_accepts=False)
    base, short = _propose(client, s)
    assert short.status_code == 409
    assert "1 of 5 members" in short.get_json()["error"]["message"]
    assert (
        client.get(
            f"{base}/proposals", headers=_auth(db.session.get(User, s.admin.id))
        ).get_json()["proposals"]
        == []
    )


def test_a_change_is_rejected_once_the_missing_members_make_it_unwinnable(
    schema_app,  # noqa: F811
):
    # 4 active members need 3 yes, and only 3 accepted: a single "no" leaves at
    # most 2 yes, so it is closed at once instead of waiting 30 days.
    s, (third, _fourth) = _sacco_with_rules(extras=2, extras_accepting=1)
    client = schema_app.test_client()
    base, made = _propose(client, s)
    pid = made.get_json()["proposal"]["id"]
    assert _vote(client, base, pid, s.admin, approve=False)["status"] == "rejected"
    assert sacco_rules.current_rules(s.sacco.id).version == 1


def test_adopting_a_new_version_carries_a_yes_voters_acceptance_for_loans(  # noqa: F811
    schema_app,  # noqa: F811
):
    # The member's savings are KES 100.00 and the SACCO holds that much.
    from app.models.membership import MembershipRole, SaccoMembership
    from test_schema_rules import make_user

    s = Setup(balance=10_000)
    outsider = make_user()
    values, _ = sacco_rules.validate_rules(RULES)
    v1 = sacco_rules.publish_rules(s.sacco.id, values, s.admin.id)
    stranger = SaccoMembership(
        user_id=outsider.id, sacco_id=s.sacco.id, role=MembershipRole.MEMBER
    )
    db.session.add(stranger)
    db.session.flush()
    for membership in (s.admin_membership, s.member_membership):
        sacco_rules.accept(membership.id, v1.id)
    v1.cycle_ends_on = sacco_rules.today()
    db.session.commit()

    client = schema_app.test_client()
    base, made = _propose(client, s)
    pid = made.get_json()["proposal"]["id"]
    _vote(client, base, pid, s.admin)
    assert _vote(client, base, pid, s.member)["status"] == "adopted"  # 2 of 3

    v2 = sacco_rules.current_rules(s.sacco.id)
    assert v2.version == 2
    assert sacco_rules.has_accepted(s.member_membership.id, v2.id)
    assert not sacco_rules.has_accepted(stranger.id, v2.id)  # never accepted v1
    assert (
        loan_service.request_loan(s.member.id, s.sacco.id, 10_000, 3).principal
        == 10_000
    )


def test_only_yes_voters_keep_their_acceptance_when_a_change_is_adopted(
    schema_app,  # noqa: F811
):
    from app.models.membership import SaccoMembership

    # 5 active members, all accepted v1. Three vote yes (more than half of 5),
    # one votes no, one does not vote.
    s, (third, no_voter, silent) = _sacco_with_rules(extras=3, extras_accepting=3)
    client = schema_app.test_client()
    base, made = _propose(client, s)
    pid = made.get_json()["proposal"]["id"]
    assert _vote(client, base, pid, no_voter, approve=False)["status"] == "open"
    _vote(client, base, pid, s.admin)
    _vote(client, base, pid, s.member)
    assert _vote(client, base, pid, third)["status"] == "adopted"

    v2 = sacco_rules.current_rules(s.sacco.id)
    assert v2.version == 2

    def membership_id(user):
        return db.session.execute(
            db.select(SaccoMembership.id).where(
                SaccoMembership.user_id == user.id,
                SaccoMembership.sacco_id == s.sacco.id,
            )
        ).scalar_one()

    assert sacco_rules.has_accepted(s.admin_membership.id, v2.id)
    assert sacco_rules.has_accepted(s.member_membership.id, v2.id)
    assert sacco_rules.has_accepted(membership_id(third), v2.id)
    assert not sacco_rules.has_accepted(membership_id(no_voter), v2.id)
    assert not sacco_rules.has_accepted(membership_id(silent), v2.id)

    # The no-voter cannot ask for a loan until they accept v2; then they can.
    holder = db.session.get(SaccoMembership, membership_id(no_voter))
    with pytest.raises(Conflict) as blocked:
        sacco_rules.check_loan_request(holder, 10_000, 1_000, 3)
    assert blocked.value.code == "RULES_NOT_ACCEPTED"
    sacco_rules.accept(holder.id, v2.id)
    db.session.commit()
    assert sacco_rules.has_accepted(holder.id, v2.id)


# ---- payout re-checks the cap and the pool; withdrawals keep the loan's cap ----


def _approved_loan(s, principal, multiple=None):
    """Rules v1 published and accepted, a loan asked for and approved by a loans
    officer, with a finance officer ready to pay it out. Returns (loan, finance)."""
    from app.models.loan import LoanDecisionKind
    from test_loans_and_ledger import _add_officer

    values, _ = sacco_rules.validate_rules(
        {**RULES, **({"loan_cap_multiple": multiple} if multiple else {})}
    )
    rules = sacco_rules.publish_rules(s.sacco.id, values, s.admin.id)
    sacco_rules.accept(s.member_membership.id, rules.id)
    db.session.commit()
    loan = loan_service.request_loan(s.member.id, s.sacco.id, principal, 3)
    for n in range(1, loan.required_approvals + 1):
        approver = _add_officer(
            s.sacco.id, f"lo{n}-{uuid.uuid4().hex[:6]}@example.com", "loans_officer"
        )
        loan_service.decide_loan(approver.id, loan.id, n, LoanDecisionKind.APPROVE)
    finance = _add_officer(
        s.sacco.id, f"fo-{uuid.uuid4().hex[:6]}@example.com", "finance_officer"
    )
    return loan, finance


def test_payout_checks_the_pool_again(schema_app):  # noqa: F811
    from app.savings import service as savings

    s = Setup(balance=10_000)  # savings of KES 100.00; the pool is KES 100.00
    loan, finance = _approved_loan(s, 10_000)
    # Savings can drop after approval (here a fee, which no loan guard covers).
    savings.post_fee(s.admin.id, s.account.id, 6_000, f"fee-{uuid.uuid4().hex[:6]}")
    with pytest.raises(Conflict) as short:
        loan_service.disburse_loan(finance.id, loan.id, f"d-{uuid.uuid4().hex[:6]}")
    # 3 x KES 40.00 = KES 120.00 still covers the loan; the pool does not.
    assert short.value.code == "LOAN_POOL_SHORT", short.value.message


def test_payout_checks_the_cap_again(schema_app):  # noqa: F811
    from app.savings import service as savings

    s = Setup(balance=30_000)
    loan, finance = _approved_loan(
        s, 60_000 // 2, multiple=1
    )  # KES 300.00 = 1 x savings
    savings.post_fee(s.admin.id, s.account.id, 10_000, f"fee-{uuid.uuid4().hex[:6]}")
    with pytest.raises(Conflict) as over:
        loan_service.disburse_loan(finance.id, loan.id, f"d-{uuid.uuid4().hex[:6]}")
    assert over.value.code == "LOAN_OVER_CAP", over.value.message
    assert "KES 200.00" in over.value.message  # the most, now


def test_payout_still_works_when_nothing_changed(schema_app):  # noqa: F811
    s = Setup(balance=10_000)
    loan, finance = _approved_loan(s, 10_000)
    paid = loan_service.disburse_loan(finance.id, loan.id, f"d-{uuid.uuid4().hex[:6]}")
    assert paid.status == "disbursed"


def test_a_withdrawal_that_breaks_an_open_loans_cap_is_refused(schema_app):  # noqa: F811
    from app.savings import service as savings

    s = Setup(balance=10_000)
    _approved_loan(s, 10_000)  # needs savings of at least KES 33.34 (10,000 / 3)
    ref = lambda: f"w-{uuid.uuid4().hex[:6]}"  # noqa: E731
    with pytest.raises(Conflict) as blocked:
        savings.withdraw(s.admin.id, s.account.id, 7_000, ref())  # leaves KES 30.00
    assert blocked.value.code == "LOAN_OVER_CAP"
    assert "KES 33.34" in blocked.value.message
    assert "up to KES 66.66" in blocked.value.message
    # Up to the line is fine, and the balance is untouched by the refusal.
    assert savings.withdraw(s.admin.id, s.account.id, 6_666, ref()).balance == 3_334


def test_withdrawals_are_free_without_an_open_loan_or_without_rules(schema_app):  # noqa: F811
    from app.savings import service as savings

    s = Setup(balance=10_000)  # no rules at all
    assert savings.withdraw(s.admin.id, s.account.id, 10_000, "w-a").balance == 0
    s = Setup(balance=10_000)
    values, _ = sacco_rules.validate_rules(RULES)
    sacco_rules.publish_rules(s.sacco.id, values, s.admin.id)
    db.session.commit()  # rules, but the member has no loan
    assert savings.withdraw(s.admin.id, s.account.id, 10_000, "w-b").balance == 0
