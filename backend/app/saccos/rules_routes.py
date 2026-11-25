"""SACCO rules: read, publish, accept, propose a change, vote.

GET  /saccos/<id>/rules                       any active member: the current rules
POST /saccos/<id>/rules                       SACCO admin or platform owner
POST /saccos/<id>/rules/accept                a member accepts the current version
GET  /saccos/<id>/rules/proposals             any active member
POST /saccos/<id>/rules/proposals             SACCO admin or platform owner
POST /saccos/<id>/rules/proposals/<pid>/votes a member votes; a majority adopts it
"""

from decimal import Decimal

from flask import g, jsonify
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import saccos_bp
from app.auth.limits import read_limit, write_limit
from app.auth.routes import _body
from app.errors import RULES_LOCKED, error_response, interface_error
from app.extensions import db
from app.models.audit import AuditLog
from app.models.rules import RulesProposal, RulesVote
from app.saccos import rules as logic
from app.security.tenant import permission_scoped


def _audit(sacco_id, actor_id, action, target):
    db.session.add(
        AuditLog(
            sacco_id=sacco_id, actor_id=actor_id, action=action, target=target[:255]
        )
    )


def invalid(errors: dict):
    body = {
        "error": {
            "code": "validation_error",
            "message": "Some SACCO rules are missing or not valid. See fields.",
            "fields": errors,
        }
    }
    return jsonify(body), 400


def _json_fields(values: dict) -> dict:
    return {k: (str(v) if k == "interest_percent" else v) for k, v in values.items()}


def _from_json(fields: dict) -> dict:
    return {
        k: (Decimal(v) if k == "interest_percent" else v) for k, v in fields.items()
    }


@saccos_bp.get("/<int:sacco_id>/rules")
@read_limit
@permission_scoped("sacco.view")
def get_rules(sacco_id):
    rules = logic.current_rules(sacco_id)
    membership = g.access.membership
    accepted = (
        logic.has_accepted(membership.id, rules.id) if rules and membership else None
    )
    return jsonify(rules=logic.rules_json(rules, accepted))


@saccos_bp.post("/<int:sacco_id>/rules")
@write_limit
@permission_scoped("sacco.update")
def publish_first_rules(sacco_id):
    logic.lock_sacco(sacco_id)
    current = logic.current_rules(sacco_id)
    if current is not None:
        return interface_error(RULES_LOCKED, logic.lock_message(current))
    values, errors = logic.validate_rules(_body())
    if errors:
        return invalid(errors)
    row = logic.publish_rules(sacco_id, values, g.access.user.id)
    _audit(
        sacco_id, g.access.user.id, "rules.publish", f"rules:{row.id} v{row.version}"
    )
    db.session.commit()
    return jsonify(rules=logic.rules_json(row, None)), 201


@saccos_bp.post("/<int:sacco_id>/rules/accept")
@write_limit
@permission_scoped("sacco.view")
def accept_rules(sacco_id):
    membership = g.access.membership
    if membership is None:
        return error_response(403, "Only members of the SACCO accept its rules.")
    rules = logic.current_rules(sacco_id)
    if rules is None:
        return error_response(404, "This SACCO has no rules yet.")
    version = _body().get("version")
    if type(version) is not int or version != rules.version:
        return error_response(
            409, "The rules changed. Read the current version before accepting."
        )
    logic.accept(membership.id, rules.id)
    _audit(
        sacco_id, g.access.user.id, "rules.accept", f"rules:{rules.id} v{rules.version}"
    )
    db.session.commit()
    return jsonify(rules=logic.rules_json(rules, True))


def _tally(proposal):
    votes = db.session.execute(
        select(RulesVote.approve, func.count())
        .where(
            RulesVote.proposal_id == proposal.id,
            RulesVote.membership_id.in_(proposal.electorate),
        )
        .group_by(RulesVote.approve)
    ).all()
    counts = {bool(approve): n for approve, n in votes}
    return counts.get(True, 0), counts.get(False, 0)


def _proposal_json(proposal, membership):
    yes, no = _tally(proposal)
    mine = (
        db.session.get(RulesVote, (proposal.id, membership.id)) if membership else None
    )
    return {
        "id": proposal.id,
        "status": proposal.status,
        "base_version": proposal.base_version,
        "fields": {
            k: (float(v) if k == "interest_percent" else v)
            for k, v in proposal.fields.items()
        },
        "electorate_size": len(proposal.electorate),
        "active_members": proposal.active_members,
        "approvals": yes,
        "rejections": no,
        "my_vote": None if mine is None else mine.approve,
    }


@saccos_bp.get("/<int:sacco_id>/rules/proposals")
@read_limit
@permission_scoped("sacco.view")
def list_proposals(sacco_id):
    rows = (
        db.session.execute(
            select(RulesProposal)
            .where(RulesProposal.sacco_id == sacco_id)
            .order_by(RulesProposal.id.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    return jsonify(proposals=[_proposal_json(p, g.access.membership) for p in rows])


@saccos_bp.post("/<int:sacco_id>/rules/proposals")
@write_limit
@permission_scoped("sacco.update")
def propose_rules(sacco_id):
    logic.lock_sacco(sacco_id)
    current = logic.current_rules(sacco_id)
    if current is None:
        return error_response(409, "Publish the first rules before proposing a change.")
    if logic.today() < current.cycle_ends_on:
        return interface_error(RULES_LOCKED, logic.lock_message(current))
    # A vote nobody finishes must not block the SACCO for ever: it lapses.
    for stale in db.session.scalars(
        select(RulesProposal).where(
            RulesProposal.sacco_id == sacco_id, RulesProposal.status == "open"
        )
    ):
        if logic.proposal_expired(stale):
            stale.status = "rejected"
            _audit(sacco_id, g.access.user.id, "rules.expire", f"proposal:{stale.id}")
    db.session.flush()
    if db.session.scalar(
        select(RulesProposal.id).where(
            RulesProposal.sacco_id == sacco_id, RulesProposal.status == "open"
        )
    ):
        return error_response(409, "A change is already open for a vote.")
    values, errors = logic.validate_rules(_body())
    if errors:
        return invalid(errors)
    electorate = logic.electorate(sacco_id, current.id)
    active = logic.active_member_count(sacco_id)
    if not electorate:
        return error_response(
            409,
            "Nobody has accepted the current rules yet, so nobody can vote on a "
            "change. Ask the members to accept them first.",
        )
    if not logic.can_still_pass(len(electorate), 0, active):
        return error_response(
            409,
            f"Only {len(electorate)} of {active} members have accepted the current "
            "rules. A change needs more than half of all members to approve it, so "
            "it cannot pass yet. Ask the others to accept the rules first.",
        )
    proposal = RulesProposal(
        sacco_id=sacco_id,
        base_version=current.version,
        fields=_json_fields(values),
        electorate=electorate,
        active_members=active,
        proposed_by_user_id=g.access.user.id,
    )
    db.session.add(proposal)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return error_response(409, "A change is already open for a vote.")
    _audit(sacco_id, g.access.user.id, "rules.propose", f"proposal:{proposal.id}")
    db.session.commit()
    return jsonify(proposal=_proposal_json(proposal, None)), 201


@saccos_bp.post("/<int:sacco_id>/rules/proposals/<int:proposal_id>/votes")
@write_limit
@permission_scoped("sacco.view")
def vote_on_proposal(sacco_id, proposal_id):
    membership = g.access.membership
    if membership is None:
        return error_response(403, "Only members of the SACCO vote.")
    approve = _body().get("approve")
    if not isinstance(approve, bool):
        return error_response(400, "Send approve: true or false.")
    logic.lock_sacco(sacco_id)
    proposal = db.session.execute(
        select(RulesProposal)
        .where(RulesProposal.id == proposal_id, RulesProposal.sacco_id == sacco_id)
        .with_for_update()
    ).scalar_one_or_none()
    if proposal is None:
        return error_response(404, "Not found.")
    if proposal.status != "open":
        return error_response(409, "Voting on this change has closed.")
    current = logic.current_rules(sacco_id)
    if (
        logic.proposal_expired(proposal)
        or current.version != proposal.base_version
        or logic.today() < current.cycle_ends_on
    ):
        proposal.status = "rejected"  # out of date: the rules moved on, or it lapsed
        db.session.commit()
        return error_response(409, "This change is out of date and has been closed.")
    if membership.id not in proposal.electorate:
        return error_response(
            403,
            "Only members who had accepted the rules when this change was proposed "
            "can vote on it.",
        )
    vote = db.session.get(RulesVote, (proposal.id, membership.id))
    if vote is None:
        db.session.add(
            RulesVote(
                proposal_id=proposal.id, membership_id=membership.id, approve=approve
            )
        )
    else:
        vote.approve = approve
    db.session.flush()
    yes, no = _tally(proposal)
    if yes * 2 > proposal.active_members:  # more than half of ALL active members
        yes_voters = db.session.execute(
            select(RulesVote.membership_id).where(
                RulesVote.proposal_id == proposal.id, RulesVote.approve.is_(True)
            )
        ).scalars()
        row = logic.publish_rules(
            sacco_id,
            _from_json(proposal.fields),
            proposal.proposed_by_user_id,
            carry_forward=yes_voters,
        )
        proposal.status = "adopted"
        _audit(
            sacco_id, g.access.user.id, "rules.adopt", f"rules:{row.id} v{row.version}"
        )
    elif not logic.can_still_pass(
        len(proposal.electorate), no, proposal.active_members
    ):  # it can no longer win
        proposal.status = "rejected"
        _audit(sacco_id, g.access.user.id, "rules.reject", f"proposal:{proposal.id}")
    db.session.commit()
    return jsonify(proposal=_proposal_json(proposal, membership))
