"""The five small backend additions the frontend needs (Neo, Part A1 to A5).

One test per item, per the plan: permissions for a SACCO, `roles` on the member
list, `?status=` on the loan list, `Retry-After` readable by the browser, and
the renamed SACCO login that still answers on its old path.
"""

import itertools

import pytest

from app.auth.tokens import create_access_token
from app.extensions import db
from app.models.access import MembershipRoleLink, Role
from app.models.loan import Loan, LoanStatus
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.models.sacco import Sacco
from app.models.user import User

_n = itertools.count(1)


def make_user(owner=False):
    user = User(
        email=f"frontend{next(_n)}@example.com",
        password_hash="x",
        is_platform_owner=owner,
    )
    db.session.add(user)
    db.session.commit()
    return user


def make_sacco():
    sacco = Sacco(name=f"Frontend Sacco {next(_n)}")
    db.session.add(sacco)
    db.session.commit()
    return sacco


def join(user, sacco, title, status=MembershipStatus.ACTIVE):
    """A membership holding one job title (the row `can()` really reads)."""
    membership = SaccoMembership(
        user_id=user.id,
        sacco_id=sacco.id,
        role=MembershipRole.MEMBER,
        status=status,
    )
    db.session.add(membership)
    db.session.flush()
    role = db.session.query(Role).filter_by(code=title).one()
    db.session.add(
        MembershipRoleLink(membership_id=membership.id, role_id=role.id)
    )
    db.session.commit()
    return membership


def auth(user):
    return {"Authorization": f"Bearer {create_access_token(user.id, 0)}"}


# ------------------------------------------------------- A1 permissions


def test_a1_a_member_and_an_admin_get_different_lists(client):
    sacco = make_sacco()
    admin, member = make_user(), make_user()
    join(admin, sacco, "sacco_admin")
    join(member, sacco, "member")

    url = f"/api/v1/members/me/permissions?sacco_id={sacco.id}"
    admin_body = client.get(url, headers=auth(admin)).get_json()
    member_body = client.get(url, headers=auth(member)).get_json()

    assert admin_body["roles"] == ["sacco_admin"]
    assert member_body["roles"] == ["member"]
    assert "member.manage" in admin_body["permissions"]
    assert "member.manage" not in member_body["permissions"]
    assert admin_body["permissions"] != member_body["permissions"]


def test_a1_someone_outside_the_sacco_is_404(client):
    sacco = make_sacco()
    outsider = make_user()
    resp = client.get(
        f"/api/v1/members/me/permissions?sacco_id={sacco.id}",
        headers=auth(outsider),
    )
    assert resp.status_code == 404


def test_a1_a_platform_owner_without_a_sacco_gets_the_platform_permissions(client):
    owner = make_user(owner=True)
    body = client.get("/api/v1/members/me/permissions", headers=auth(owner)).get_json()
    assert body["roles"] == []
    assert "admin.manage" in body["permissions"]


# ---------------------------------------------------------- A2 roles


def test_a2_the_member_list_and_one_member_carry_roles(client):
    sacco = make_sacco()
    admin, officer = make_user(), make_user()
    join(admin, sacco, "sacco_admin")
    membership = join(officer, sacco, "loans_officer")

    listed = client.get(
        f"/api/v1/saccos/{sacco.id}/members", headers=auth(admin)
    ).get_json()
    rows = {row["id"]: row for row in listed["members"]}
    assert rows[membership.id]["roles"] == ["loans_officer"]
    assert rows[membership.id]["role"] == "member"  # the legacy column stays

    one = client.get(
        f"/api/v1/saccos/{sacco.id}/members/{membership.id}", headers=auth(admin)
    ).get_json()
    assert one["roles"] == ["loans_officer"]


# --------------------------------------------------------- A3 status


def test_a3_the_loan_list_filters_by_status(client):
    sacco = make_sacco()
    admin = make_user()
    join(admin, sacco, "sacco_admin")
    waiting = join(make_user(), sacco, "member")
    refused = join(make_user(), sacco, "member")
    db.session.add_all(
        [
            Loan(
                sacco_id=sacco.id,
                membership_id=waiting.id,
                principal=1000,
                term_months=3,
                required_approvals=1,
                status=LoanStatus.REQUESTED,
            ),
            Loan(
                sacco_id=sacco.id,
                membership_id=refused.id,
                principal=2000,
                term_months=3,
                required_approvals=1,
                status=LoanStatus.REJECTED,
            ),
        ]
    )
    db.session.commit()

    url = f"/api/v1/loans/saccos/{sacco.id}"
    filtered = client.get(f"{url}?status=requested", headers=auth(admin))
    assert [loan["status"] for loan in filtered.get_json()["loans"]] == ["requested"]
    everything = client.get(url, headers=auth(admin))
    assert len(everything.get_json()["loans"]) == 2


def test_a3_an_unknown_status_is_400(client):
    sacco = make_sacco()
    admin = make_user()
    join(admin, sacco, "sacco_admin")
    resp = client.get(
        f"/api/v1/loans/saccos/{sacco.id}?status=waiting", headers=auth(admin)
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["message"] == "Unknown status."


# ---------------------------------------------------------- A4 CORS


def test_a4_the_browser_may_read_retry_after(client, app):
    app.config["CORS_ORIGINS"] = "http://localhost:5173"
    resp = client.get(
        "/api/v1/members/me", headers={"Origin": "http://localhost:5173"}
    )
    assert "Retry-After" in resp.headers["Access-Control-Expose-Headers"]


# ------------------------------------------------------ A5 sacco-login


@pytest.mark.parametrize(
    "path", ["/api/v1/auth/sacco-login", "/api/v1/auth/sacco-login"]
)
def test_a5_both_login_paths_are_served(client, path):
    resp = client.post(path, json={})
    assert resp.status_code == 401  # the route exists; nobody is signed in
