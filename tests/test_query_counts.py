"""List endpoints must run the same number of queries however many rows they return."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, text

from app.auth.tokens import create_access_token
from app.extensions import db
from app.models.access import MembershipRoleLink, Role
from app.models.loan import Loan, LoanStatus
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.models.sacco import Sacco
from app.models.user import User


def _truncate():
    db.session.remove()
    db.session.execute(text("TRUNCATE users, saccos RESTART IDENTITY CASCADE"))
    db.session.commit()


@pytest.fixture(autouse=True)
def clean_tables(app):
    _truncate()
    yield
    _truncate()


def _user(email):
    user = User(email=email, password_hash="x", email_verified_at=datetime.now(UTC))
    db.session.add(user)
    db.session.commit()
    return user


def _member(sacco_id, user, role=MembershipRole.MEMBER):
    row = SaccoMembership(
        user_id=user.id, sacco_id=sacco_id, role=role, status=MembershipStatus.ACTIVE
    )
    db.session.add(row)
    db.session.commit()
    return row


def _headers(user):
    token = create_access_token(user.id, user.token_version)
    return {"Authorization": f"Bearer {token}"}


def _count(client, url, headers):
    db.session.remove()  # rows cached by the setup code would hide queries
    seen = []

    def on_query(conn, cursor, statement, *rest):
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", on_query)
    try:
        response = client.get(url, headers=headers)
    finally:
        event.remove(db.engine, "before_cursor_execute", on_query)
    assert response.status_code == 200, response.get_json()
    return len(seen)


def _sacco_with_admin():
    sacco = Sacco(name="Umoja SACCO")
    db.session.add(sacco)
    db.session.commit()
    admin = _user("admin@example.com")
    _member(sacco.id, admin, MembershipRole.SACCO_ADMIN)
    return sacco.id, admin


def test_member_list_query_count_does_not_grow_with_members(app, client):
    sacco_id, admin = _sacco_with_admin()
    url = f"/api/v1/saccos/{sacco_id}/members"
    small = _count(client, url, _headers(admin))
    for i in range(8):
        _member(sacco_id, _user(f"m{i}@example.com"))
    assert _count(client, url, _headers(admin)) == small


def test_loan_list_query_count_does_not_grow_with_loans(app, client):
    sacco_id, admin = _sacco_with_admin()
    url = f"/api/v1/loans/saccos/{sacco_id}"

    def add_loan(n=[0]):  # noqa: B006 - one open loan per member, so one borrower each
        n[0] += 1
        borrower = _member(sacco_id, _user(f"b{n[0]}@example.com"))
        db.session.add(
            Loan(
                sacco_id=sacco_id,
                membership_id=borrower.id,
                principal=1000,
                required_approvals=1,
                status=LoanStatus.REQUESTED,
            )
        )
        db.session.commit()

    add_loan()
    small = _count(client, url, _headers(admin))
    for _ in range(7):
        add_loan()
    assert _count(client, url, _headers(admin)) == small


def test_member_list_roles_match_links_and_legacy_fallback(app, client):
    sacco_id, admin = _sacco_with_admin()
    both = _member(sacco_id, _user("both@example.com"))
    _member(sacco_id, _user("plain@example.com"))
    codes = dict(db.session.execute(select(Role.code, Role.id)).all())
    for code in ("member", "secretary"):
        db.session.add(MembershipRoleLink(membership_id=both.id, role_id=codes[code]))
    db.session.commit()

    body = client.get(
        f"/api/v1/saccos/{sacco_id}/members", headers=_headers(admin)
    ).get_json()
    roles = {m["email"]: m["roles"] for m in body["members"]}
    assert roles["both@example.com"] == ["member", "secretary"]
    assert roles["plain@example.com"] == ["member"]  # no links: legacy column
    assert roles["admin@example.com"] == ["sacco_admin"]
