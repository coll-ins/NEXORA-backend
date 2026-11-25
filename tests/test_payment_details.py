"""SACCO pay-in details: formats, two-officer approval, audit, mail, tenancy.

Runs against the migration-built database (the CHECK constraints matter here).
"""

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.audit import AuditLog
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.sacco import SaccoStatus
from app.models.user import User
from app.saccos import payment_details as logic
from savings_support import Setup, release_session, schema_app  # noqa: F401
from test_schema_rules import auth, make_user, officer

pytestmark = pytest.mark.schema

GOOD = {
    "mpesa_type": "paybill",
    "mpesa_number": "400200",
    "display_name": "Umoja SACCO",
    "account_reference_hint": "your member number",
    "bank_name": "Equity Bank",
    "bank_account_name": "Umoja SACCO",
    "bank_account_number": "0123456789",
    "bank_branch": "Kitale",
}


def url(s, tail=""):
    return f"/api/v1/saccos/{s.sacco.id}/payment-details{tail}"


def propose(client, user, s, **overrides):
    body = {**GOOD, **overrides}
    body = {k: v for k, v in body.items() if v is not ...}
    return client.post(url(s, "/changes"), json=body, headers=auth(user))


def approve(client, user, s, change_id):
    return client.post(url(s, f"/changes/{change_id}/approve"), headers=auth(user))


def read(client, user, s):
    return client.get(url(s), headers=auth(user))





@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(
        logic,
        "send_text_email",
        lambda to, subject, body: sent.append((to, subject, body)),
    )
    return sent


def new_sacco():
    """A SACCO with an admin, a finance officer (the second officer) and a member."""
    s = Setup()
    s.finance = officer(s, "finance_officer")
    return s


# ---- formats: every rejection says the format, an example and why -------------

BAD = [
    ("mpesa_number", {"mpesa_number": "1234"}),
    ("mpesa_number", {"mpesa_number": "12345678"}),
    ("mpesa_number", {"mpesa_number": "40O200"}),
    ("mpesa_number", {"mpesa_number": "٤٠٠٢٠٠"}),  # Arabic-Indic digits
    ("mpesa_number", {"mpesa_number": "400 200"}),
    ("mpesa_number", {"mpesa_number": 400200}),
    ("mpesa_number", {"mpesa_number": ...}),  # a type with no number
    ("mpesa_type", {"mpesa_type": "bank"}),
    ("mpesa_type", {"mpesa_type": ...}),  # a number with no type
    ("display_name", {"display_name": ...}),
    ("display_name", {"display_name": "x" * 61}),
    ("display_name", {"display_name": 12}),
    ("display_name", {"display_name": "Umoja\nSACCO"}),
    ("display_name", {"display_name": "bad\ud800"}),
    ("display_name", {"display_name": "Umoja\u202eSACCO"}),  # right-to-left override
    ("display_name", {"display_name": "Umoja\u200bSACCO"}),  # zero-width space
    ("display_name", {"display_name": "Umoja\u2028Sacco"}),  # line separator
    ("display_name", {"display_name": "Umoja\u00a0Sacco"}),  # no-break space
    ("display_name", {"display_name": "Um\u043eja SACCO"}),  # Cyrillic o
    ("display_name", {"display_name": "Umoja\U000e0041"}),  # tag character
    ("display_name", {"display_name": "Umoja\ufeff"}),  # byte-order mark
    ("account_reference_hint", {"mpesa_type": "till", "account_reference_hint": "x"}),
    ("bank_account_number", {"bank_account_number": "12345"}),
    ("bank_account_number", {"bank_account_number": "1" * 21}),
    ("bank_account_number", {"bank_account_number": "0123 456789"}),
    ("bank_account_number", {"bank_account_number": ...}),
    ("bank_name", {"bank_name": ...}),
    ("bank_account_name", {"bank_account_name": ...}),
    ("bank_uses_shared_paybill", {"bank_uses_shared_paybill": "yes"}),
    ("bank_paybill_number", {"bank_uses_shared_paybill": True}),
    (
        "bank_paybill_number",
        {"bank_uses_shared_paybill": True, "bank_paybill_number": "12"},
    ),
    ("bank_paybill_number", {"bank_paybill_number": "247247"}),
]


@pytest.mark.parametrize(("field", "change"), BAD)
def test_a_bad_value_is_refused_with_format_example_and_reason(
    schema_app,  # noqa: F811
    field,
    change,
):
    s = new_sacco()
    resp = propose(schema_app.test_client(), s.admin, s, **change)
    assert resp.status_code == 400, resp.json
    error = resp.json["error"]
    assert error["code"] == "validation_error"
    reason = error["fields"][field]
    assert len(reason) > 25
    assert "Example" in reason or "Send true" in reason or "empty" in reason
    assert read(schema_app.test_client(), s.member, s).json["details"] is None


def test_a_branch_without_a_bank_and_a_shared_paybill_without_a_bank(schema_app):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    only_mpesa = {
        "bank_name": ...,
        "bank_account_name": ...,
        "bank_account_number": ...,
    }
    branch = propose(c, s.admin, s, **only_mpesa)
    assert list(branch.json["error"]["fields"]) == ["bank_branch"]
    shared = propose(
        c,
        s.admin,
        s,
        bank_branch=...,
        bank_uses_shared_paybill=True,
        bank_paybill_number="247247",
        **only_mpesa,
    )
    assert "bank_uses_shared_paybill" in shared.json["error"]["fields"]


def test_some_method_is_needed_and_unknown_fields_are_refused(schema_app):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    empty = c.post(url(s, "/changes"), json={}, headers=auth(s.admin))
    assert empty.status_code == 400
    assert "M-Pesa details, bank details or both" in empty.json["error"]["fields"]["_"]
    extra = propose(c, s.admin, s, colour="red")
    assert extra.json["error"]["fields"]["_"] == "Unknown field(s): colour."
    assert logic.validate([])[1] == {"_": "Send the pay-in details as a JSON object."}


def test_a_sacco_may_use_mpesa_only_or_bank_only(schema_app):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    bank_only = {k: ... for k in ("mpesa_type", "mpesa_number", "display_name")}
    bank_only["account_reference_hint"] = ...
    till = propose(c, s.admin, s, mpesa_type="till", account_reference_hint=...)
    bank = propose(c, s.admin, s, **bank_only)
    assert till.status_code == 201, till.json
    assert bank.status_code == 201, bank.json
    assert bank.json["change"]["values"]["mpesa_number"] is None


# ---- a change is not live until a second officer approves it -------------------


def test_a_proposal_is_not_live_until_approved(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    empty = {"details": None, "pending": None}
    assert read(c, s.member, s).json == empty

    made = propose(c, s.admin, s)
    assert made.status_code == 201, made.json
    change = made.json["change"]
    assert change["status"] == "pending"

    # Members still see nothing, and never see the waiting change.
    assert read(c, s.member, s).json == empty
    # The proposer and the other officer both see it waiting.
    for officer_ in (s.admin, s.finance):
        waiting = read(c, officer_, s).json
        assert waiting["details"] is None
        assert waiting["pending"]["id"] == change["id"]

    done = approve(c, s.finance, s, change["id"])
    assert done.status_code == 200, done.json
    # The second officer's approval is the moment it takes effect.
    live = read(c, s.member, s).json
    assert live["pending"] is None
    assert live["details"]["mpesa_number"] == "400200"
    assert live["details"]["bank_account_number"] == "0123456789"


def test_updated_at_is_the_moment_the_second_officer_approved(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    change = propose(c, s.admin, s).json["change"]
    done = approve(c, s.finance, s, change["id"])
    assert done.status_code == 200, done.json
    live = read(c, s.member, s).json["details"]
    db.session.expire_all()
    row = db.session.execute(
        text(
            "SELECT proposed_at, approved_at, go_live_at "
            "FROM sacco_payment_detail_changes WHERE id = :i"
        ),
        {"i": change["id"]},
    ).one()
    stamp = datetime.fromisoformat(live["updated_at"])
    assert stamp == row.go_live_at
    # Live at approval: no waiting period between the two.
    assert row.go_live_at == row.approved_at
    assert row.approved_at > row.proposed_at


def test_a_second_officers_approval_changes_what_members_see_at_once(  # noqa: F811
    schema_app,  # noqa: F811
    mails,
):
    s = new_sacco()
    c = schema_app.test_client()
    first = propose(c, s.admin, s).json["change"]
    assert approve(c, s.finance, s, first["id"]).status_code == 200
    assert read(c, s.member, s).json["details"]["mpesa_number"] == "400200"

    second = propose(c, s.finance, s, mpesa_number="400300").json["change"]
    done = approve(c, s.admin, s, second["id"])
    assert done.status_code == 200, done.json
    change = done.json["change"]
    assert change["go_live_at"] == change["approved_at"]

    # No waiting: the new number is what members read now, and nothing is left
    # pending or coming later.
    seen = read(c, s.member, s).json
    assert seen["details"]["mpesa_number"] == "400300"
    assert seen["pending"] is None
    assert "scheduled" not in seen


def test_the_first_ever_details_are_live_at_approval(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    approved = approve(c, s.finance, s, propose(c, s.admin, s).json["change"]["id"])
    assert approved.status_code == 200
    seen = read(c, s.member, s).json
    assert seen["details"] is not None
    assert seen["details"]["mpesa_number"] == "400200"


def test_a_newer_approval_replaces_the_details_in_one_go(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    old = propose(c, s.admin, s, mpesa_number="400111").json["change"]
    approve(c, s.finance, s, old["id"])
    new = propose(c, s.admin, s, mpesa_number="400222").json["change"]
    approve(c, s.finance, s, new["id"])

    # Both approvals are visible in the audit trail, and the newer values are
    # the ones members read: no older change can land on top of them.
    assert read(c, s.member, s).json["details"]["mpesa_number"] == "400222"
    actions = [
        r.action
        for r in db.session.query(AuditLog)
        .filter(AuditLog.sacco_id == s.sacco.id, AuditLog.action.like("payin.%"))
        .order_by(AuditLog.id)
    ]
    assert actions == [
        "payin.propose",
        "payin.approve",
        "payin.propose",
        "payin.approve",
    ]


def test_the_database_refuses_a_go_live_before_the_approval(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    change = propose(c, s.admin, s).json["change"]
    with pytest.raises(IntegrityError):
        db.session.execute(
            text(
                "UPDATE sacco_payment_detail_changes SET status = 'approved', "
                "approved_by = :u, approved_at = now(), "
                "go_live_at = now() - interval '1 hour' WHERE id = :i"
            ),
            {"i": change["id"], "u": s.finance.id},
        )
    db.session.rollback()


def _approved(c, s, **overrides):
    """A change proposed by the admin and approved by the finance officer."""
    change = propose(c, s.admin, s, **overrides).json["change"]
    assert approve(c, s.finance, s, change["id"]).status_code == 200
    return change


def test_the_same_values_cannot_be_proposed_again_once_live(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    _approved(c, s)
    again = propose(c, s.admin, s)  # nothing changed, so there is nothing to approve
    assert again.status_code == 409


def test_a_second_change_replaces_the_live_values(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    first = propose(c, s.admin, s).json["change"]
    approve(c, s.finance, s, first["id"])
    second = propose(c, s.finance, s, mpesa_number="400300").json["change"]
    # Still the old number until the other officer approves.
    assert read(c, s.member, s).json["details"]["mpesa_number"] == "400200"
    approve(c, s.admin, s, second["id"])
    assert read(c, s.member, s).json["details"]["mpesa_number"] == "400300"
    db.session.expire_all()
    assert (
        db.session.execute(
            text("SELECT count(*) FROM sacco_payment_details WHERE sacco_id = :s"),
            {"s": s.sacco.id},
        ).scalar_one()
        == 1
    )


def test_proposing_what_is_already_live_is_refused(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    approve(c, s.finance, s, propose(c, s.admin, s).json["change"]["id"])
    same = propose(c, s.admin, s)
    assert same.status_code == 409
    assert "already see" in same.json["error"]["message"]


def test_a_newer_proposal_replaces_the_waiting_one(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    old = propose(c, s.admin, s).json["change"]
    new = propose(c, s.admin, s, mpesa_number="400999").json["change"]
    stale = approve(c, s.finance, s, old["id"])
    assert stale.status_code == 409
    assert read(c, s.finance, s).json["pending"]["id"] == new["id"]
    assert approve(c, s.finance, s, new["id"]).status_code == 200
    again = approve(c, s.admin, s, new["id"])
    assert again.status_code == 409


def test_an_unknown_change_is_404(schema_app):  # noqa: F811
    s = new_sacco()
    assert approve(schema_app.test_client(), s.finance, s, 999999).status_code == 404


# ---- two different people ------------------------------------------------------


def test_the_proposer_cannot_approve_their_own_change(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    change = propose(c, s.admin, s).json["change"]
    own = approve(c, s.admin, s, change["id"])
    assert own.status_code == 403
    assert own.json["error"]["code"] == "PAYIN_SAME_OFFICER"
    assert read(c, s.member, s).json["details"] is None
    assert read(c, s.admin, s).json["pending"]["status"] == "pending"


def test_one_officer_holding_two_titles_is_still_one_person(schema_app, mails):  # noqa: F811
    # Proposing and approving as the same user is refused whatever the title.
    s = new_sacco()
    c = schema_app.test_client()
    change = propose(c, s.finance, s).json["change"]
    assert approve(c, s.finance, s, change["id"]).status_code == 403


def test_the_database_refuses_self_approval_and_a_second_pending(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    change = propose(c, s.admin, s).json["change"]
    with pytest.raises(IntegrityError):
        db.session.execute(
            text(
                "UPDATE sacco_payment_detail_changes SET status = 'approved', "
                "approved_by = proposed_by, approved_at = now(), go_live_at = now() "
                "WHERE id = :i"
            ),
            {"i": change["id"]},
        )
    db.session.rollback()
    with pytest.raises(IntegrityError):
        db.session.execute(
            text(
                "INSERT INTO sacco_payment_detail_changes "
                "(sacco_id, proposed_by, mpesa_type, mpesa_number, display_name) "
                "VALUES (:s, :u, 'till', '123456', 'x')"
            ),
            {"s": s.sacco.id, "u": s.admin.id},
        )
    db.session.rollback()


def test_the_database_refuses_a_bad_number_even_if_the_app_does_not(schema_app):  # noqa: F811
    s = new_sacco()
    with pytest.raises(IntegrityError):
        db.session.execute(
            text(
                "INSERT INTO sacco_payment_details "
                "(sacco_id, updated_by, mpesa_type, mpesa_number, display_name) "
                "VALUES (:s, :u, 'paybill', '12', 'x')"
            ),
            {"s": s.sacco.id, "u": s.admin.id},
        )
    db.session.rollback()


# ---- who may ask --------------------------------------------------------------


def test_only_officers_with_the_permission_may_propose_or_approve(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    secretary = officer(s, "secretary")
    loans = officer(s, "loans_officer")
    change = propose(c, s.admin, s).json["change"]
    for user in (s.member, secretary, loans):
        assert propose(c, user, s).status_code == 403
        assert approve(c, user, s, change["id"]).status_code == 403
        # They can read what is live, but not what is waiting.
        assert read(c, user, s).json["pending"] is None


def test_a_platform_owner_reads_but_cannot_change_pay_in_details(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    owner = make_user(owner=True)
    change = propose(c, s.admin, s).json["change"]
    assert read(c, owner, s).status_code == 200
    assert propose(c, owner, s).status_code == 403
    assert approve(c, owner, s, change["id"]).status_code == 403


def test_a_removed_officer_can_no_longer_approve(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    change = propose(c, s.admin, s).json["change"]
    membership = (
        db.session.query(SaccoMembership)
        .filter_by(user_id=s.finance.id, sacco_id=s.sacco.id)
        .one()
    )
    membership.status = MembershipStatus.INACTIVE
    db.session.commit()
    db.session.remove()
    assert approve(c, s.finance, s, change["id"]).status_code == 403


def test_a_suspended_sacco_answers_423(schema_app, mails):  # noqa: F811
    from app.models.sacco import Sacco

    s = new_sacco()
    db.session.get(Sacco, s.sacco.id).status = SaccoStatus.SUSPENDED
    db.session.commit()
    db.session.remove()
    assert propose(schema_app.test_client(), s.admin, s).status_code == 423


# ---- another SACCO ------------------------------------------------------------


def test_someone_from_another_sacco_gets_404_everywhere(schema_app, mails):  # noqa: F811
    s = new_sacco()
    other = new_sacco()
    c = schema_app.test_client()
    change = propose(c, s.admin, s).json["change"]
    approve(c, s.finance, s, change["id"])
    pending = propose(c, s.admin, s, mpesa_number="400555").json["change"]

    for outsider in (other.member, other.admin, other.finance):
        assert read(c, outsider, s).status_code == 404
        assert propose(c, outsider, s).status_code == 404
        assert approve(c, outsider, s, pending["id"]).status_code == 404
    # An officer's own SACCO's route cannot reach this SACCO's change either.
    for outsider in (other.admin, other.finance):
        assert approve(c, outsider, other, pending["id"]).status_code == 404
    assert read(c, s.member, s).json["details"]["mpesa_number"] == "400200"


def test_each_sacco_has_its_own_details(schema_app, mails):  # noqa: F811
    a, b = new_sacco(), new_sacco()
    c = schema_app.test_client()
    approve(c, a.finance, a, propose(c, a.admin, a).json["change"]["id"])
    assert read(c, b.member, b).json["details"] is None
    assert read(c, a.member, a).json["details"] is not None


def test_a_signed_out_caller_gets_401(schema_app):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    assert c.get(url(s)).status_code == 401
    assert c.post(url(s, "/changes"), json=GOOD).status_code == 401


# ---- shared bank paybill -------------------------------------------------------


def test_a_shared_bank_paybill_is_marked_manual_confirm_only(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    plain = propose(c, s.admin, s).json["change"]
    assert plain["values"]["bank_manual_confirm_only"] is False
    shared = propose(
        c, s.admin, s, bank_uses_shared_paybill=True, bank_paybill_number="247247"
    ).json["change"]
    assert shared["values"]["bank_manual_confirm_only"] is True
    approve(c, s.finance, s, shared["id"])
    live = read(c, s.member, s).json["details"]
    assert live["bank_manual_confirm_only"] is True
    assert live["bank_paybill_number"] == "247247"
    assert read(c, s.member, s).json["details"]["bank_manual_confirm_only"] is True


# ---- audit ---------------------------------------------------------------------


def test_every_proposal_and_approval_is_audited_without_the_numbers(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    first = propose(c, s.admin, s).json["change"]
    second = propose(c, s.finance, s, mpesa_number="400300").json["change"]
    approve(c, s.admin, s, second["id"])
    rows = (
        db.session.query(AuditLog)
        .filter(AuditLog.sacco_id == s.sacco.id, AuditLog.action.like("payin.%"))
        .order_by(AuditLog.id)
        .all()
    )
    assert [(r.action, r.actor_id) for r in rows] == [
        ("payin.propose", s.admin.id),
        ("payin.supersede", s.finance.id),
        ("payin.propose", s.finance.id),
        ("payin.approve", s.admin.id),
    ]
    assert rows[0].target.startswith(f"change:{first['id']} fields:")
    assert "mpesa_number" in rows[0].target
    assert rows[3].target == f"change:{second['id']}"
    everything = " ".join(r.target for r in rows)
    assert "400200" not in everything and "0123456789" not in everything


def test_a_refused_proposal_leaves_no_audit_row(schema_app, mails):  # noqa: F811
    s = new_sacco()
    propose(schema_app.test_client(), s.admin, s, mpesa_number="1")
    count = (
        db.session.query(AuditLog)
        .filter(AuditLog.sacco_id == s.sacco.id, AuditLog.action.like("payin.%"))
        .count()
    )
    assert count == 0


# ---- officers are told ---------------------------------------------------------


def test_every_officer_and_only_officers_are_emailed(schema_app, mails):  # noqa: F811
    s = new_sacco()
    secretary = officer(s, "secretary")
    c = schema_app.test_client()
    admin_email = db.session.get(User, s.admin.id).email
    finance_email = db.session.get(User, s.finance.id).email

    change = propose(c, s.admin, s).json["change"]
    assert sorted(to for to, _, _ in mails) == sorted([admin_email, finance_email])
    assert "waiting" in mails[0][1]
    assert "400200" not in mails[0][2]  # no numbers in an email
    assert admin_email in mails[0][2]
    assert db.session.get(User, secretary.id).email not in [to for to, _, _ in mails]

    mails.clear()
    approve(c, s.finance, s, change["id"])
    # Officers (the admin included) only: a secretary or a member is not emailed.
    assert sorted(to for to, _, _ in mails) == sorted([admin_email, finance_email])
    assert "have changed" in mails[0][1]
    assert "400200" not in mails[0][2] and "0123456789" not in mails[0][2]


def test_an_inactive_officer_is_not_emailed(schema_app, mails):  # noqa: F811
    s = new_sacco()
    membership = (
        db.session.query(SaccoMembership)
        .filter_by(user_id=s.finance.id, sacco_id=s.sacco.id)
        .one()
    )
    membership.status = MembershipStatus.INACTIVE
    finance_email = db.session.get(User, s.finance.id).email
    db.session.commit()
    db.session.remove()
    propose(schema_app.test_client(), s.admin, s)
    assert finance_email not in [to for to, _, _ in mails]


def test_a_mail_outage_does_not_fail_or_undo_the_change(schema_app, monkeypatch):  # noqa: F811
    def broken(*_):
        raise OSError("smtp down")

    monkeypatch.setattr(logic, "send_text_email", broken)
    s = new_sacco()
    c = schema_app.test_client()
    made = propose(c, s.admin, s)
    assert made.status_code == 201
    approve(c, s.finance, s, made.json["change"]["id"])
    assert read(c, s.member, s).json["details"] is not None


def test_a_failure_listing_the_officers_does_not_fail_the_change(
    schema_app,  # noqa: F811
    monkeypatch,
):
    def broken(_):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(logic, "officers", broken)
    s = new_sacco()
    assert propose(schema_app.test_client(), s.admin, s).status_code == 201


# ---- review findings: second officer minted by the proposer, unicode, mail, cap ----


def test_an_officer_made_after_the_proposal_cannot_approve_it(schema_app, mails):  # noqa: F811
    s = Setup()  # the admin is the only officer
    c = schema_app.test_client()
    change = propose(c, s.admin, s).json["change"]
    late = officer(s, "finance_officer")  # given the title after the proposal
    blocked = approve(c, late, s, change["id"])
    assert blocked.status_code == 403
    assert blocked.json["error"]["code"] == "PAYIN_NEW_OFFICER"
    assert read(c, s.member, s).json["details"] is None
    # The same person can approve a proposal made after they became an officer.
    fresh = propose(c, s.admin, s, mpesa_number="400300").json["change"]
    assert approve(c, late, s, fresh["id"]).status_code == 200


def test_granting_the_officer_title_tells_every_officer(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    admin_email = db.session.get(User, s.admin.id).email
    finance_email = db.session.get(User, s.finance.id).email
    member_email = db.session.get(User, s.member.id).email
    resp = c.put(
        f"/api/v1/saccos/{s.sacco.id}/members/{s.member_membership.id}/roles",
        json={"roles": ["finance_officer"]},
        headers=auth(s.admin),
    )
    assert resp.status_code == 200, resp.json
    assert sorted(to for to, _, _ in mails) == sorted(
        [admin_email, finance_email, member_email]
    )
    subject, body = mails[0][1], mails[0][2]
    assert "new officer" in subject
    assert member_email in body and admin_email in body


def test_giving_a_title_that_is_not_an_officer_title_sends_nothing(schema_app, mails):  # noqa: F811
    s = new_sacco()
    resp = schema_app.test_client().put(
        f"/api/v1/saccos/{s.sacco.id}/members/{s.member_membership.id}/roles",
        json={"roles": ["secretary"]},
        headers=auth(s.admin),
    )
    assert resp.status_code == 200, resp.json
    assert mails == []


def test_adding_an_admin_or_changing_a_role_tells_the_officers(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    owner = make_user(owner=True)
    newcomer = make_user()
    added = c.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={
            "email": db.session.get(User, newcomer.id).email,
            "role": "sacco_admin",
        },
        headers=auth(owner),
    )
    assert added.status_code == 201, added.json
    assert len(mails) == 3  # admin, finance officer and the new admin
    mails.clear()
    other = make_user()
    created = c.post(
        f"/api/v1/saccos/{s.sacco.id}/members",
        json={"email": db.session.get(User, other.id).email, "role": "member"},
        headers=auth(s.admin),
    )
    assert created.status_code == 201, created.json
    assert mails == []  # a plain member gains nothing
    patched = c.patch(
        f"/api/v1/saccos/{s.sacco.id}/members/{created.json['id']}",
        json={"role": "sacco_admin"},
        headers=auth(owner),
    )
    assert patched.status_code == 200, patched.json
    assert len(mails) == 4  # now the second admin is an officer too


def test_a_proposal_from_someone_no_longer_an_officer_cannot_be_approved(
    schema_app,  # noqa: F811
    mails,
):
    s = new_sacco()
    c = schema_app.test_client()
    change = propose(c, s.finance, s).json["change"]
    membership = (
        db.session.query(SaccoMembership)
        .filter_by(user_id=s.finance.id, sacco_id=s.sacco.id)
        .one()
    )
    membership.status = MembershipStatus.INACTIVE
    db.session.commit()
    db.session.remove()
    stale = approve(c, s.admin, s, change["id"])
    assert stale.status_code == 409
    assert "no longer an officer" in stale.json["error"]["message"]
    assert read(c, s.member, s).json["details"] is None


def test_the_waiting_change_names_its_proposer(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    propose(c, s.finance, s)
    pending = read(c, s.admin, s).json["pending"]
    assert pending["proposed_by_email"] == db.session.get(User, s.finance.id).email


def test_text_is_normalised_and_accents_are_allowed(schema_app, mails):  # noqa: F811
    s = new_sacco()
    made = propose(
        schema_app.test_client(),
        s.admin,
        s,
        display_name="Cafe\u0301 Umoja",
        bank_branch=...,
    )
    assert made.status_code == 201, made.json
    assert made.json["change"]["values"]["display_name"] == "Caf\u00e9 Umoja"


def test_a_sacco_cannot_flood_its_officers_with_proposals(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    for n in range(logic.MAX_PROPOSALS_PER_HOUR):
        ok = propose(c, s.admin, s, mpesa_number=f"4002{n:02d}")
        assert ok.status_code == 201, ok.json
    blocked = propose(c, s.admin, s, mpesa_number="499999")
    assert blocked.status_code == 429
    assert "too many" in blocked.json["error"]["message"]


def test_smtp_mail_goes_out_on_a_background_thread(schema_app, monkeypatch):  # noqa: F811
    import threading

    seen = []
    monkeypatch.setattr(
        logic, "send_text_email", lambda *a: seen.append(threading.current_thread())
    )
    monkeypatch.setitem(schema_app.config, "MAIL_BACKEND", "smtp")
    s = new_sacco()
    made = propose(schema_app.test_client(), s.admin, s)
    assert made.status_code == 201
    for thread in threading.enumerate():
        if thread.daemon and thread is not threading.current_thread():
            thread.join(timeout=5)
    assert len(seen) == 2
    assert all(t is not threading.main_thread() for t in seen)


def test_holding_since_is_none_for_a_member_and_for_an_inactive_officer(schema_app):  # noqa: F811
    from app.security.permissions import holding_since

    s = new_sacco()
    assert holding_since(s.sacco.id, s.member.id, logic.PERMISSION) is None
    assert holding_since(s.sacco.id, s.finance.id, logic.PERMISSION) is not None
    # A legacy admin with no title rows counts from when the membership began.
    assert holding_since(s.sacco.id, s.admin.id, logic.PERMISSION) is not None
    secretary = officer(s, "secretary")
    assert holding_since(s.sacco.id, secretary.id, logic.PERMISSION) is None
    assert holding_since(s.sacco.id, make_user().id, logic.PERMISSION) is None


# ---- titles keep granted_at; legacy admins; text; mail logs -------------------


def _titles_url(s, membership_id):
    return f"/api/v1/saccos/{s.sacco.id}/members/{membership_id}/roles"


def _membership_of(s, user):
    return (
        db.session.query(SaccoMembership)
        .filter_by(user_id=user.id, sacco_id=s.sacco.id)
        .one()
    )


def _granted(membership_id):
    from app.models.access import MembershipRoleLink, Role

    return dict(
        db.session.query(Role.code, MembershipRoleLink.granted_at)
        .join(MembershipRoleLink, MembershipRoleLink.role_id == Role.id)
        .filter(MembershipRoleLink.membership_id == membership_id)
        .all()
    )


def test_editing_an_unrelated_title_keeps_granted_at_and_sends_no_alert(
    schema_app,  # noqa: F811
    mails,
):
    s = new_sacco()
    c = schema_app.test_client()
    membership_id = _membership_of(s, s.finance).id
    before = _granted(membership_id)
    db.session.remove()
    for roles in (["finance_officer", "secretary"], ["finance_officer"]):
        put = c.put(
            _titles_url(s, membership_id), json={"roles": roles}, headers=auth(s.admin)
        )
        assert put.status_code == 200, put.json
    after = _granted(membership_id)
    assert after["finance_officer"] == before["finance_officer"]
    assert mails == []


def test_adding_the_officer_title_sends_one_alert_and_stamps_only_that_title(
    schema_app,  # noqa: F811
    mails,
):
    s = new_sacco()
    c = schema_app.test_client()
    membership_id = s.member_membership.id
    url_ = _titles_url(s, membership_id)
    assert (
        c.put(url_, json={"roles": ["secretary"]}, headers=auth(s.admin)).status_code
        == 200
    )
    secretary_at = _granted(membership_id)["secretary"]
    db.session.remove()
    assert mails == []
    put = c.put(
        url_, json={"roles": ["secretary", "finance_officer"]}, headers=auth(s.admin)
    )
    assert put.status_code == 200, put.json
    assert len(mails) == 3  # one alert, to each of the 3 officers
    assert {subject for _, subject, _ in mails} == {
        "NEXORA: a new officer can change pay-in details"
    }
    granted = _granted(membership_id)
    assert granted["secretary"] == secretary_at
    assert granted["finance_officer"] > secretary_at
    db.session.remove()
    mails.clear()
    again = c.put(
        url_,
        json={"roles": ["finance_officer", "secretary", "treasurer"]},
        headers=auth(s.admin),
    )
    assert again.status_code in (200, 400), again.json
    assert mails == []  # already an officer: nothing newly gained


def test_a_waiting_change_can_still_be_approved_after_an_unrelated_title_edit(
    schema_app,  # noqa: F811
    mails,
):
    s = new_sacco()
    c = schema_app.test_client()
    change = propose(c, s.admin, s).json["change"]
    put = c.put(
        _titles_url(s, _membership_of(s, s.finance).id),
        json={"roles": ["finance_officer", "secretary"]},
        headers=auth(s.admin),
    )
    assert put.status_code == 200, put.json
    db.session.remove()
    assert approve(c, s.finance, s, change["id"]).status_code == 200


def test_a_legacy_admin_may_approve_another_officers_change(schema_app, mails):  # noqa: F811
    s = new_sacco()  # the admin has no title rows: the role column alone grants it
    c = schema_app.test_client()
    change = propose(c, s.finance, s).json["change"]
    assert approve(c, s.admin, s, change["id"]).status_code == 200


def test_a_legacy_admin_cannot_approve_their_own_change(schema_app, mails):  # noqa: F811
    s = new_sacco()
    c = schema_app.test_client()
    change = propose(c, s.admin, s).json["change"]
    blocked = approve(c, s.admin, s, change["id"])
    assert blocked.status_code == 403
    assert blocked.json["error"]["code"] == "PAYIN_SAME_OFFICER"


def test_a_legacy_admin_created_after_the_proposal_is_still_a_new_officer(
    schema_app,  # noqa: F811
    mails,
):
    from app.models.membership import MembershipRole

    s = new_sacco()
    c = schema_app.test_client()
    change = propose(c, s.finance, s).json["change"]
    late = make_user()
    db.session.add(
        SaccoMembership(
            user_id=late.id, sacco_id=s.sacco.id, role=MembershipRole.SACCO_ADMIN
        )
    )
    db.session.commit()
    db.session.remove()
    blocked = approve(c, late, s, change["id"])
    assert blocked.status_code == 403
    assert blocked.json["error"]["code"] == "PAYIN_NEW_OFFICER"


TEXT_FIELDS = (
    "display_name",
    "account_reference_hint",
    "bank_name",
    "bank_account_name",
    "bank_branch",
)


def test_apostrophes_ampersands_full_stops_and_brackets_are_accepted(schema_app, mails):  # noqa: F811
    s = new_sacco()
    made = propose(
        schema_app.test_client(),
        s.admin,
        s,
        display_name="St. Mary's Teachers' SACCO & Co.",
        bank_account_name="Nyeri Women's Group (2026)",
    )
    assert made.status_code == 201, made.json
    values = made.json["change"]["values"]
    assert values["display_name"] == "St. Mary's Teachers' SACCO & Co."
    assert values["bank_account_name"] == "Nyeri Women's Group (2026)"


@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize(
    "bad",
    ["Umoja SA\u0421CO", "Umoja\u202eSACCO", "\u202eUmoja"],
    ids=["cyrillic", "bidi", "bidi-lead"],
)
def test_cyrillic_and_bidi_characters_are_refused_in_every_text_field(
    schema_app,  # noqa: F811
    mails,
    field,
    bad,
):
    s = new_sacco()
    resp = propose(schema_app.test_client(), s.admin, s, **{field: bad})
    assert resp.status_code == 400, resp.json
    assert field in resp.json["error"]["fields"]


def test_a_mail_failure_is_logged_with_sacco_and_officer_ids_never_the_numbers(
    schema_app,  # noqa: F811
    monkeypatch,
    caplog,
):
    def broken(*_):
        raise OSError("smtp down")

    monkeypatch.setattr(logic, "send_text_email", broken)
    # The migration run in the schema_app fixture calls logging.fileConfig, which
    # disables loggers that already exist.
    monkeypatch.setattr(logic.log, "disabled", False)
    s = new_sacco()
    with caplog.at_level("ERROR", logger=logic.log.name):
        assert propose(schema_app.test_client(), s.admin, s).status_code == 201
    text = caplog.text
    for officer_id in (s.admin.id, s.finance.id):
        assert f"officer {officer_id} of SACCO {s.sacco.id}" in text
    for number in ("400200", "0123456789"):
        assert number not in text
