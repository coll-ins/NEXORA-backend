"""Job titles, SACCO settings, and the loan flow end to end over HTTP."""

import uuid

import pytest

from app.auth.tokens import create_access_token
from app.extensions import db
from app.models.membership import MembershipRole, SaccoMembership
from app.models.user import User
from app.savings.balance import verify_balance
from savings_support import (  # noqa: F401
    Setup,
    add_officer,
    ask_for_reversal,
    release_session,
    schema_app,
)

pytestmark = pytest.mark.schema


def auth(user):
    token = create_access_token(user.id, user.token_version)
    return {"Authorization": f"Bearer {token}"}


def new_member(s, role=MembershipRole.MEMBER):
    user = User(email=f"u-{uuid.uuid4().hex[:10]}@example.com", password_hash="x")
    db.session.add(user)
    db.session.flush()
    m = SaccoMembership(user_id=user.id, sacco_id=s.sacco.id, role=role)
    db.session.add(m)
    db.session.commit()
    ids = (user.id, user.token_version, m.id)
    db.session.remove()
    user = type("U", (), {"id": ids[0], "token_version": ids[1]})
    return user, ids[2]


def owner():
    user = User(
        email=f"owner-{uuid.uuid4().hex[:10]}@example.com",
        password_hash="x",
        is_platform_owner=True,
    )
    db.session.add(user)
    db.session.commit()
    ref = type("U", (), {"id": user.id, "token_version": user.token_version})
    db.session.remove()
    return ref


def roles_url(s, membership_id):
    return f"/api/v1/saccos/{s.sacco.id}/members/{membership_id}/roles"


# ---- job titles -------------------------------------------------------------


def test_admin_gives_a_member_a_job_title(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    _, mid = new_member(s)
    resp = client.put(
        roles_url(s, mid),
        json={"roles": ["loans_officer", "member"]},
        headers=auth(s.admin),
    )
    assert resp.status_code == 200, resp.json
    assert resp.json["roles"] == ["loans_officer", "member"]
    got = client.get(roles_url(s, mid), headers=auth(s.admin))
    assert got.json["roles"] == ["loans_officer", "member"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"roles": []}, 400),
        ({"roles": "loans_officer"}, 400),
        ({"roles": ["wizard"]}, 400),
        ({"roles": ["sacco_admin"]}, 403),
    ],
)
def test_bad_or_forbidden_titles_are_refused(schema_app, body, status):  # noqa: F811
    s = Setup()
    _, mid = new_member(s)
    resp = schema_app.test_client().put(
        roles_url(s, mid), json=body, headers=auth(s.admin)
    )
    assert resp.status_code == status


def test_nobody_changes_their_own_titles(schema_app):  # noqa: F811
    s = Setup()
    resp = schema_app.test_client().put(
        roles_url(s, s.admin_membership.id),
        json={"roles": ["finance_officer"]},
        headers=auth(s.admin),
    )
    assert resp.status_code == 403


def test_a_member_cannot_set_titles(schema_app):  # noqa: F811
    s = Setup()
    _, mid = new_member(s)
    resp = schema_app.test_client().put(
        roles_url(s, mid), json={"roles": ["loans_officer"]}, headers=auth(s.member)
    )
    assert resp.status_code == 403


def test_another_saccos_member_is_404(schema_app):  # noqa: F811
    s, other = Setup(), Setup()
    _, mid = new_member(other)
    resp = schema_app.test_client().put(
        roles_url(s, mid), json={"roles": ["loans_officer"]}, headers=auth(s.admin)
    )
    assert resp.status_code == 404


def test_the_owner_edits_an_admins_titles_within_the_admin_rule(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    boss = auth(owner())
    url = roles_url(s, s.admin_membership.id)
    ok = client.put(url, json={"roles": ["sacco_admin", "member"]}, headers=boss)
    assert ok.status_code == 200
    assert ok.json["roles"] == ["member", "sacco_admin"]
    # An admin holds no officer title (schema: never approves, never reads audit).
    extra = {"roles": ["sacco_admin", "finance_officer"]}
    assert client.put(url, json=extra, headers=boss).status_code == 409


# ---- settings ---------------------------------------------------------------


def test_members_read_settings_and_only_the_owner_changes_them(schema_app):  # noqa: F811
    s = Setup()
    client = schema_app.test_client()
    url = f"/api/v1/saccos/{s.sacco.id}/settings"

    got = client.get(url, headers=auth(s.member))
    assert got.status_code == 200
    assert (got.json["loan_cap"], got.json["savings_multiple"]) == (0, 3)

    boss = auth(owner())
    by_admin = client.patch(url, json={"loan_cap": 5}, headers=auth(s.admin))
    assert by_admin.status_code == 403
    for bad in ({"loan_cap": -1}, {"loan_cap": True}, {"required_approvals": 3}, {}):
        assert client.patch(url, json=bad, headers=boss).status_code == 400, bad

    changed = client.patch(
        url, json={"loan_cap": 500_000, "savings_multiple": 2}, headers=boss
    )
    assert changed.status_code == 200
    assert (changed.json["loan_cap"], changed.json["savings_multiple"]) == (500_000, 2)
    assert client.get(url, headers=auth(s.member)).json["loan_cap"] == 500_000


# ---- the loan flow over HTTP, officers appointed through the API -------------


def test_full_loan_flow_and_loan_rows_cannot_be_reversed(schema_app):  # noqa: F811
    s = Setup(balance=10_000)
    client = schema_app.test_client()
    loans_officer, lo_mid = new_member(s, MembershipRole.STAFF)
    loans_officer_2, lo2_mid = new_member(s, MembershipRole.STAFF)
    finance, fo_mid = new_member(s, MembershipRole.STAFF)
    appointments = (
        (lo_mid, "loans_officer"),
        (lo2_mid, "loans_officer"),
        (fo_mid, "finance_officer"),
    )
    for mid, title in appointments:
        resp = client.put(
            roles_url(s, mid), json={"roles": [title]}, headers=auth(s.admin)
        )
        assert resp.status_code == 200

    loan = client.post(
        f"/api/v1/loans/saccos/{s.sacco.id}",
        json={"principal": 20_000, "term_months": 6},
        headers=auth(s.member),
    )
    assert loan.status_code == 201, loan.json
    loan_id = loan.json["id"]
    assert loan.json["required_approvals"] == 2

    # The loans officer sees every loan in the SACCO, not only their own.
    listed = client.get(
        f"/api/v1/loans/saccos/{s.sacco.id}", headers=auth(loans_officer)
    )
    assert [row["id"] for row in listed.json["loans"]] == [loan_id]

    decide = f"/api/v1/loans/{loan_id}/decisions"
    # A SACCO admin never approves loans (agreed schema).
    assert client.post(
        decide, json={"step": 1, "decision": "approve"}, headers=auth(s.admin)
    ).status_code == 403
    assert client.post(
        decide, json={"step": 1, "decision": "approve"}, headers=auth(loans_officer_2)
    ).status_code == 201
    second = client.post(
        decide, json={"step": 2, "decision": "approve"}, headers=auth(loans_officer)
    )
    assert second.json["status"] == "approved"

    paid = client.post(
        f"/api/v1/loans/{loan_id}/disbursements",
        json={"reference": "disb-1"},
        headers=auth(finance),
    )
    assert paid.status_code == 201, paid.json
    assert verify_balance(s.account.id).stored == 30_000

    history = client.get(
        f"/api/v1/savings/accounts/{s.account.id}/transactions",
        headers=auth(s.admin),
    ).json["transactions"]
    disbursement_id = next(t["id"] for t in history if t["reference"] == "disb-1")
    reverse = ask_for_reversal(client, s.admin, disbursement_id)
    assert reverse.status_code == 409

    repaid = client.post(
        f"/api/v1/loans/{loan_id}/repayments",
        json={"amount": 20_000, "reference": "rep-1"},
        headers=auth(finance),
    )
    assert repaid.status_code == 201, repaid.json
    assert repaid.json["loan_status"] == "repaid"
    reverse = ask_for_reversal(client, s.admin, repaid.json["transaction_id"])
    assert reverse.status_code == 409
    check = verify_balance(s.account.id)
    assert check.consistent and check.stored == 10_000


# ---- found by the red-team review ---------------------------------------------


def _titled_member(s, role=MembershipRole.MEMBER, titles=()):
    from app.models.access import MembershipRoleLink, Role

    user, mid = new_member(s, role)
    for code in titles:
        role_id = db.session.query(Role.id).filter_by(code=code).scalar()
        db.session.add(MembershipRoleLink(membership_id=mid, role_id=role_id))
    db.session.commit()
    db.session.remove()
    return user, mid


def _fund(s, mid, amount, reference):
    from app.models.savings import SavingsAccount
    from app.savings import service as savings

    savings.open_account(s.admin.id, mid)
    account_id = (
        db.session.query(SavingsAccount.id).filter_by(membership_id=mid).scalar()
    )
    db.session.remove()
    savings.deposit(s.admin.id, account_id, amount, reference)
    return account_id


def test_sacco_admin_cannot_drift_from_the_admin_role(schema_app):  # noqa: F811
    """Taking sacco_admin through /roles would leave the admin routes open."""
    s = Setup()
    client = schema_app.test_client()
    boss = auth(owner())
    stripped = client.put(
        roles_url(s, s.admin_membership.id), json={"roles": ["member"]}, headers=boss
    )
    assert stripped.status_code == 409
    _, mid = new_member(s)
    granted = client.put(
        roles_url(s, mid), json={"roles": ["sacco_admin"]}, headers=boss
    )
    assert granted.status_code == 409
    # The admin is still a full admin: the refused change left nothing behind.
    _, other = new_member(s)
    resp = client.put(
        roles_url(s, other), json={"roles": ["finance_officer"]}, headers=auth(s.admin)
    )
    assert resp.status_code == 200


def test_demoting_an_admin_takes_the_sacco_admin_title_too(schema_app):  # noqa: F811
    from app.security.permissions import can

    s = Setup()
    ex_admin, mid = _titled_member(s, MembershipRole.SACCO_ADMIN, ["sacco_admin"])
    client = schema_app.test_client()
    demoted = client.patch(
        f"/api/v1/saccos/{s.sacco.id}/members/{mid}",
        json={"role": "member"},
        headers=auth(owner()),
    )
    assert demoted.status_code == 200, demoted.json
    got = client.get(roles_url(s, mid), headers=auth(s.admin))
    assert "sacco_admin" not in got.json["roles"]
    user = db.session.get(User, ex_admin.id)
    assert not can(user, s.sacco.id, "loan.approve")
    assert not can(user, s.sacco.id, "savings.post")


def test_an_admin_cannot_edit_another_admin_or_an_inactive_member(schema_app):  # noqa: F811
    from app.models.membership import MembershipStatus

    s = Setup()
    client = schema_app.test_client()
    _, admin_mid = _titled_member(s, MembershipRole.SACCO_ADMIN, ["sacco_admin"])
    resp = client.put(
        roles_url(s, admin_mid),
        json={"roles": ["sacco_admin", "finance_officer"]},
        headers=auth(s.admin),
    )
    assert resp.status_code == 403
    _, mid = new_member(s)
    db.session.get(SaccoMembership, mid).status = MembershipStatus.INACTIVE
    db.session.commit()
    db.session.remove()
    resp = client.put(
        roles_url(s, mid), json={"roles": ["loans_officer"]}, headers=auth(s.admin)
    )
    assert resp.status_code == 409


def test_legacy_memberships_show_the_titles_they_effectively_hold(schema_app):  # noqa: F811
    s = Setup()
    got = schema_app.test_client().get(
        roles_url(s, s.admin_membership.id), headers=auth(s.admin)
    )
    assert got.json["roles"] == ["sacco_admin"]


@pytest.mark.parametrize(
    "body",
    [
        {"savings_multiple": 2**31},
        {"savings_multiple": 1001},
        {"join_code_ttl_minutes": 0},
        {"join_code_ttl_minutes": 1441},
        {"required_approvals": 0},
        {"loan_cap": 10**16},
    ],
)
def test_out_of_range_settings_are_400_not_500(schema_app, body):  # noqa: F811
    s = Setup()
    resp = schema_app.test_client().patch(
        f"/api/v1/saccos/{s.sacco.id}/settings", json=body, headers=auth(owner())
    )
    assert resp.status_code == 400, resp.json


def test_members_see_only_their_own_loans_and_other_saccos_none(schema_app):  # noqa: F811
    s, other = Setup(balance=10_000), Setup(balance=10_000)
    client = schema_app.test_client()
    neighbour, mid = new_member(s)
    _fund(s, mid, 10_000, "dep-neighbour")
    from app.loans import service as loans

    mine = loans.request_loan(s.member.id, s.sacco.id, 1_000, 6)
    theirs = loans.request_loan(neighbour.id, s.sacco.id, 1_000, 6)

    listed = client.get(f"/api/v1/loans/saccos/{s.sacco.id}", headers=auth(s.member))
    assert [row["id"] for row in listed.json["loans"]] == [mine.id]
    assert client.get(
        f"/api/v1/loans/{theirs.id}", headers=auth(s.member)
    ).status_code == 404
    repay = client.post(
        f"/api/v1/loans/{theirs.id}/repayments",
        json={"amount": 1, "reference": "rep-not-mine"},
        headers=auth(s.member),
    )
    assert repay.status_code == 404
    assert client.get(
        f"/api/v1/loans/{mine.id}", headers=auth(other.admin)
    ).status_code == 404
    assert client.get(
        f"/api/v1/loans/saccos/{s.sacco.id}", headers=auth(other.member)
    ).status_code == 404


def test_retry_rules_around_reversals_and_withdrawals(schema_app):  # noqa: F811
    from app.savings import service as savings

    s = Setup(balance=1_000)
    savings.withdraw(s.admin.id, s.account.id, 1_000, "wd-all")
    # The balance is now 0, yet the retry is the same request: replayed.
    assert savings.withdraw(s.admin.id, s.account.id, 1_000, "wd-all").replayed

    from app.ledger import reversals

    tx = savings.deposit(s.admin.id, s.account.id, 500, "dep-then-undo")
    reversals.reverse_transaction(s.admin.id, tx.id, "undo-1")
    again = reversals.reverse_transaction(s.admin.id, tx.id, "undo-1")
    assert again.replayed
    # Retrying a deposit that was reversed since must not report success.
    with pytest.raises(savings.Conflict) as exc:
        savings.deposit(s.admin.id, s.account.id, 500, "dep-then-undo")
    assert exc.value.code == "DUPLICATE_REFERENCE"
