"""SACCO rules routes: reading, publishing, accepting, proposing and voting,
including every refusal. The vote arithmetic is in test_sacco_rules.py."""

from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models.rules import RulesProposal
from app.models.user import User
from app.saccos import rules as sacco_rules
from savings_support import Setup, release_session, schema_app  # noqa: F401
from test_sacco_rules import RULES, _auth, _propose, _sacco_with_rules, _vote
from test_schema_rules import make_user

pytestmark = pytest.mark.schema


def _as(user):
    return _auth(db.session.get(User, user.id))


def test_reading_publishing_and_accepting_the_first_rules(schema_app):  # noqa: F811
    client = schema_app.test_client()
    s = Setup()
    base = f"/api/v1/saccos/{s.sacco.id}/rules"
    assert client.get(base, headers=_as(s.member)).get_json()["rules"] is None
    assert (
        client.post(
            f"{base}/accept", json={"version": 1}, headers=_as(s.member)
        ).status_code
        == 404
    )  # nothing to accept yet

    bad = client.post(base, json={"contribution_amount": "lots"}, headers=_as(s.admin))
    assert bad.status_code == 400
    assert "contribution_amount" in bad.get_json()["error"]["fields"]
    assert client.post(base, json=RULES, headers=_as(s.member)).status_code == 403
    assert client.post(base, json=RULES, headers=_as(s.admin)).status_code == 201
    again = client.post(base, json=RULES, headers=_as(s.admin))
    assert again.status_code == 409
    assert again.get_json()["error"]["code"] == "RULES_LOCKED"

    seen = client.get(base, headers=_as(s.member)).get_json()["rules"]
    assert seen["version"] == 1 and seen["accepted"] is False and seen["locked"]
    done = client.post(f"{base}/accept", json={"version": 1}, headers=_as(s.member))
    assert done.get_json()["rules"]["accepted"] is True
    assert client.get(base, headers=_as(s.member)).get_json()["rules"]["accepted"]


def test_a_platform_owner_who_is_not_a_member_cannot_accept_or_vote(schema_app):  # noqa: F811
    client = schema_app.test_client()
    s, _ = _sacco_with_rules()
    owner = make_user(owner=True)
    base = f"/api/v1/saccos/{s.sacco.id}/rules"
    assert (
        client.post(
            f"{base}/accept", json={"version": 1}, headers=_as(owner)
        ).status_code
        == 403
    )
    _, made = _propose(client, s)
    pid = made.get_json()["proposal"]["id"]
    assert (
        client.post(
            f"{base}/proposals/{pid}/votes", json={"approve": True}, headers=_as(owner)
        ).status_code
        == 403
    )


def test_proposals_are_refused_with_no_rules_during_the_cycle_or_when_invalid(
    schema_app,  # noqa: F811
):
    client = schema_app.test_client()
    s = Setup()
    base = f"/api/v1/saccos/{s.sacco.id}/rules"
    none = client.post(f"{base}/proposals", json=RULES, headers=_as(s.admin))
    assert (
        none.status_code == 409 and "first rules" in none.get_json()["error"]["message"]
    )

    assert client.post(base, json=RULES, headers=_as(s.admin)).status_code == 201
    locked = client.post(f"{base}/proposals", json=RULES, headers=_as(s.admin))
    assert locked.get_json()["error"]["code"] == "RULES_LOCKED"

    s, _ = _sacco_with_rules()
    base = f"/api/v1/saccos/{s.sacco.id}/rules"
    invalid = client.post(
        f"{base}/proposals", json={"interest_percent": 5}, headers=_as(s.admin)
    )
    assert invalid.status_code == 400
    assert (
        client.post(f"{base}/proposals", json=RULES, headers=_as(s.member)).status_code
        == 403
    )


def test_a_stale_open_proposal_lapses_and_the_sacco_can_propose_again(schema_app):  # noqa: F811
    client = schema_app.test_client()
    s, _ = _sacco_with_rules()
    base, first = _propose(client, s)
    old = db.session.get(RulesProposal, first.get_json()["proposal"]["id"])
    old.created_at = datetime.now(UTC) - timedelta(days=sacco_rules.PROPOSAL_DAYS + 1)
    db.session.commit()
    second = client.post(f"{base}/proposals", json=RULES, headers=_as(s.admin))
    assert second.status_code == 201
    assert db.session.get(RulesProposal, old.id).status == "rejected"

    listed = client.get(f"{base}/proposals", headers=_as(s.member)).get_json()
    assert [p["status"] for p in listed["proposals"]] == ["open", "rejected"]
    assert listed["proposals"][0]["my_vote"] is None


def test_voting_refusals_and_changing_a_vote(schema_app):  # noqa: F811
    client = schema_app.test_client()
    s, (outsider,) = _sacco_with_rules(extras=1, extras_accepting=0)
    base, made = _propose(client, s)
    assert made.status_code == 201, made.get_json()
    pid = made.get_json()["proposal"]["id"]
    url = f"{base}/proposals/{pid}/votes"

    assert (
        client.post(url, json={"approve": "yes"}, headers=_as(s.admin)).status_code
        == 400
    )
    assert (
        client.post(
            f"{base}/proposals/99999/votes",
            json={"approve": True},
            headers=_as(s.admin),
        ).status_code
        == 404
    )
    not_accepted = client.post(url, json={"approve": True}, headers=_as(outsider))
    assert not_accepted.status_code == 403  # did not accept the current rules

    # 3 active members, 2 acceptors: one "no" leaves 1 yes of 3, which cannot win.
    changed = _vote(client, base, pid, s.admin, approve=True)
    assert changed["status"] == "open" and changed["my_vote"] is True
    flipped = _vote(client, base, pid, s.admin, approve=False)
    assert flipped["status"] == "rejected" and flipped["my_vote"] is False
    closed = client.post(url, json={"approve": True}, headers=_as(s.member))
    assert (
        closed.status_code == 409 and "closed" in closed.get_json()["error"]["message"]
    )


def test_voting_on_a_proposal_whose_rules_moved_on_closes_it(schema_app):  # noqa: F811
    client = schema_app.test_client()
    s, _ = _sacco_with_rules()
    base, made = _propose(client, s)
    pid = made.get_json()["proposal"]["id"]
    sacco_rules.publish_rules(
        s.sacco.id, sacco_rules.validate_rules(RULES)[0], s.admin.id
    )
    db.session.commit()  # a newer version appeared: the proposal is out of date
    out = client.post(
        f"{base}/proposals/{pid}/votes", json={"approve": True}, headers=_as(s.admin)
    )
    assert (
        out.status_code == 409 and "out of date" in out.get_json()["error"]["message"]
    )
    assert db.session.get(RulesProposal, pid).status == "rejected"
