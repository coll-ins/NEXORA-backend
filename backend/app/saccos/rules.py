"""SACCO rules: validation, publishing, acceptance and the loan checks.

The rules only record and enforce. Nothing here moves or lends money.
"""

import calendar
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.errors import (
    LOAN_OVER_CAP,
    LOAN_POOL_SHORT,
    LOAN_RULE_BROKEN,
    RULES_NOT_ACCEPTED,
)
from app.extensions import db
from app.models.loan import Loan, LoanRepayment, LoanStatus, SaccoSettings
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.rules import RulesAcceptance, SaccoRules
from app.models.sacco import Sacco
from app.models.savings import SavingsAccount
from app.savings.service import Conflict

FREQUENCIES = ("weekly", "twice_monthly", "monthly")
FREQUENCY_WORDS = {"weekly": "week", "twice_monthly": "half month", "monthly": "month"}
DEFAULT_LOAN_CAP_MULTIPLE = 3  # CARE VSLA manual: "up to but no more than 3x savings"
DEFAULT_CYCLE_MONTHS = 12
MAX_MONEY = 10**12  # minor units; far above any real SACCO, below BigInteger


def kes(minor: int) -> str:
    return f"KES {minor // 100:,}.{minor % 100:02d}"


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_text(value, limit):
    if value is None or value == "":
        return None, None
    if not isinstance(value, str) or "\x00" in value:
        return None, "Send plain text."
    try:
        value.encode("utf-8")  # a lone surrogate would fail later, inside the database
    except UnicodeEncodeError:
        return None, "Send plain text."
    value = value.strip()
    if len(value) > limit:
        return None, f"Keep it to {limit} characters or fewer."
    return (value or None), None


def validate_rules(body) -> tuple[dict, dict]:
    """Return (values, errors). `errors` maps a field to a plain-words reason
    that says the format and gives an example; empty when everything is valid."""
    errors: dict[str, str] = {}
    values: dict = {}
    if not isinstance(body, dict):
        return {}, {"_": "Send the rules as a JSON object."}
    known = {
        "contribution_amount",
        "contribution_frequency",
        "loan_cap_multiple",
        "interest_percent",
        "repayment_period_value",
        "repayment_period_unit",
        "late_fine_amount",
        "cycle_months",
        "guarantors_required",
        "min_membership_days",
        "exit_rules",
        "loan_purposes",
    }
    extra = sorted(set(body) - known)
    if extra:
        errors["_"] = f"Unknown field(s): {', '.join(extra)}."

    def money(field, label, minimum, example):
        value = body.get(field)
        if not _is_int(value) or not minimum <= value <= MAX_MONEY:
            errors[field] = (
                f"{label} is required: a whole number of cents"
                f"{' above 0' if minimum else ', 0 or more'}. {example}"
            )
        else:
            values[field] = value

    money(
        "contribution_amount",
        "Contribution amount",
        1,
        "Example: 50000 means KES 500.00 each time.",
    )
    money(
        "late_fine_amount",
        "Late-payment fine",
        0,
        "Example: 5000 means KES 50.00. Send 0 for no fine.",
    )

    if body.get("contribution_frequency") in FREQUENCIES:
        values["contribution_frequency"] = body["contribution_frequency"]
    else:
        errors["contribution_frequency"] = (
            "Contribution frequency is required: weekly, twice_monthly or monthly."
        )

    multiple = body.get("loan_cap_multiple", DEFAULT_LOAN_CAP_MULTIPLE)
    if _is_int(multiple) and 1 <= multiple <= 10:
        values["loan_cap_multiple"] = multiple
    else:
        errors["loan_cap_multiple"] = (
            "Loan cap must be a whole number from 1 to 10: how many times a member's "
            "savings they may borrow. Example: 3 means savings of KES 1,000 allow up "
            "to KES 3,000. Left blank it is 3."
        )

    rate = body.get("interest_percent")
    try:
        if rate is None or isinstance(rate, bool | str):
            raise InvalidOperation
        rate = Decimal(str(rate))
        if not (Decimal(0) <= rate <= Decimal(100)) or rate != rate.quantize(
            Decimal("0.01")
        ):
            raise InvalidOperation
        values["interest_percent"] = rate
    except InvalidOperation:
        errors["interest_percent"] = (
            "Interest or service charge is required and has no default: type the "
            "percentage your SACCO agreed to charge per loan, from 0 to 100 with up "
            "to 2 decimals. Example: 10 means 10% of each loan. Type 0 if loans "
            "carry no charge."
        )

    unit = body.get("repayment_period_unit")
    period = body.get("repayment_period_value")
    limit = {"days": 730, "months": 24}.get(unit) if isinstance(unit, str) else None
    if limit and _is_int(period) and 1 <= period <= limit:
        values["repayment_period_value"] = period
        values["repayment_period_unit"] = unit
    else:
        errors["repayment_period_value"] = (
            "Repayment period is required: a whole number with a unit of days "
            "(1 to 730) or months (1 to 24). Example: 6 months."
        )

    months = body.get("cycle_months", DEFAULT_CYCLE_MONTHS)
    if _is_int(months) and 9 <= months <= 12:
        values["cycle_months"] = months
    else:
        errors["cycle_months"] = (
            "Cycle length must be 9 to 12 months. Example: 12. Left blank it is 12."
        )

    for field, label, high, example in (
        (
            "guarantors_required",
            "Guarantors required",
            10,
            "Example: 2. Send 0 for none.",
        ),
        (
            "min_membership_days",
            "Minimum membership before borrowing",
            730,
            "A whole number of days. Example: 90. Send 0 for none.",
        ),
    ):
        value = body.get(field, 0)
        if _is_int(value) and 0 <= value <= high:
            values[field] = value
        else:
            errors[field] = (
                f"{label} must be a whole number from 0 to {high}. {example}"
            )

    for field, label, limit_chars in (
        ("exit_rules", "Exit rules", 500),
        ("loan_purposes", "Allowed loan purposes", 200),
    ):
        text, problem = _clean_text(body.get(field), limit_chars)
        if problem:
            errors[field] = (
                f"{label}: {problem} Example: school fees, stock, medical bills."
            )
        else:
            values[field] = text
    return values, errors


def add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def today() -> date:
    return datetime.now(UTC).date()


PROPOSAL_DAYS = 30


def proposal_expired(proposal) -> bool:
    return proposal.created_at < datetime.now(UTC) - timedelta(days=PROPOSAL_DAYS)


def lock_sacco(sacco_id: int) -> None:
    """Serialise rule changes, loan checks, payouts and withdrawals for one SACCO.

    FOR NO KEY UPDATE, not FOR UPDATE: every ledger insert takes a KEY SHARE lock
    on the SACCO's row (the foreign key), and a plain FOR UPDATE here would make a
    reversal or deposit that holds an account lock wait on a withdrawal that holds
    this one and waits for that account: a deadlock."""
    db.session.execute(
        select(Sacco.id).where(Sacco.id == sacco_id).with_for_update(key_share=True)
    )


def electorate(sacco_id: int, rules_id: int) -> list[int]:
    """Active members who accepted the current rules: the people who may vote."""
    return list(
        db.session.execute(
            select(SaccoMembership.id)
            .join(RulesAcceptance, RulesAcceptance.membership_id == SaccoMembership.id)
            .where(
                SaccoMembership.sacco_id == sacco_id,
                SaccoMembership.status == MembershipStatus.ACTIVE,
                RulesAcceptance.rules_id == rules_id,
            )
            .order_by(SaccoMembership.id)
        ).scalars()
    )


def active_member_count(sacco_id: int) -> int:
    return db.session.scalar(
        select(func.count())
        .select_from(SaccoMembership)
        .where(
            SaccoMembership.sacco_id == sacco_id,
            SaccoMembership.status == MembershipStatus.ACTIVE,
        )
    )


def can_still_pass(voters: int, rejections: int, active: int) -> bool:
    """A change needs more than half of ALL active members at proposal time, so
    members who did not accept the current rules count against it."""
    return (voters - rejections) * 2 > active


def current_rules(sacco_id: int) -> SaccoRules | None:
    return db.session.execute(
        select(SaccoRules)
        .where(SaccoRules.sacco_id == sacco_id)
        .order_by(SaccoRules.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def lock_message(rules: SaccoRules) -> str:
    if today() < rules.cycle_ends_on:
        return (
            f"These rules are locked until the cycle ends on "
            f"{rules.cycle_ends_on:%d %b %Y}. "
            "They can change only after that, by a vote of the members."
        )
    return "This SACCO already has rules. Propose a change and the members vote on it."


def publish_rules(
    sacco_id: int,
    values: dict,
    user_id: int | None,
    carry_forward: Iterable[int] = (),
) -> SaccoRules:
    """Add the next version and start its cycle today. The caller checked it may.

    `carry_forward` is the memberships whose acceptance of the last version
    counts for this one: the members who voted yes on the change. Everyone else,
    including members who voted no or did not vote, must accept again before
    they can ask for a loan.
    """
    current = current_rules(sacco_id)
    start = today()
    row = SaccoRules(
        sacco_id=sacco_id,
        version=current.version + 1 if current else 1,
        cycle_starts_on=start,
        cycle_ends_on=add_months(start, values["cycle_months"]),
        created_by_user_id=user_id,
        **values,
    )
    db.session.add(row)
    db.session.flush()
    carried = list(carry_forward)
    if current is not None and carried:
        # A yes vote is agreement with the new rules, so those members need not
        # open the app again to keep borrowing. A no vote is not: that member
        # re-accepts (or leaves) before the next loan.
        db.session.execute(
            pg_insert(RulesAcceptance)
            .from_select(
                ["membership_id", "rules_id"],
                select(RulesAcceptance.membership_id, literal(row.id)).where(
                    RulesAcceptance.rules_id == current.id,
                    RulesAcceptance.membership_id.in_(carried),
                ),
            )
            .on_conflict_do_nothing()
        )
    # One source of truth: the loan service reads the multiple from the settings
    # row when a SACCO has none, so keep that row in step.
    settings = db.session.get(SaccoSettings, sacco_id)
    if settings is None:
        db.session.add(
            SaccoSettings(
                sacco_id=sacco_id, savings_multiple=values["loan_cap_multiple"]
            )
        )
    else:
        settings.savings_multiple = values["loan_cap_multiple"]
    return row


def has_accepted(membership_id: int, rules_id: int) -> bool:
    return db.session.get(RulesAcceptance, (membership_id, rules_id)) is not None


def accept(membership_id: int, rules_id: int) -> None:
    db.session.execute(
        pg_insert(RulesAcceptance)
        .values(membership_id=membership_id, rules_id=rules_id)
        .on_conflict_do_nothing()
    )


def rules_json(rules: SaccoRules | None, accepted: bool | None = None) -> dict | None:
    if rules is None:
        return None
    return {
        "id": rules.id,
        "version": rules.version,
        "contribution_amount": rules.contribution_amount,
        "contribution_frequency": rules.contribution_frequency,
        "loan_cap_multiple": rules.loan_cap_multiple,
        "interest_percent": float(rules.interest_percent),
        "repayment_period_value": rules.repayment_period_value,
        "repayment_period_unit": rules.repayment_period_unit,
        "late_fine_amount": rules.late_fine_amount,
        "cycle_months": rules.cycle_months,
        "guarantors_required": rules.guarantors_required,
        "min_membership_days": rules.min_membership_days,
        "exit_rules": rules.exit_rules,
        "loan_purposes": rules.loan_purposes,
        "cycle_starts_on": rules.cycle_starts_on.isoformat(),
        "cycle_ends_on": rules.cycle_ends_on.isoformat(),
        "locked": today() < rules.cycle_ends_on,
        "accepted": accepted,
    }


def pool_cash(sacco_id: int) -> int:
    """Cash the SACCO can still lend, on the cautious side.

    A disbursed loan is credited to the borrower's account, so it is inside the
    balances total and has also left the pool: it is taken off twice. Loans still
    waiting for approval or payout are reserved so two requests cannot both claim
    the same cash. If a borrower withdrew the proceeds this can understate the
    pool, which only ever makes the SACCO refuse a loan it could have made.
    """
    total = db.session.scalar(
        select(func.coalesce(func.sum(SavingsAccount.balance), 0)).where(
            SavingsAccount.sacco_id == sacco_id
        )
    )
    lent = db.session.scalar(
        select(func.coalesce(func.sum(Loan.principal), 0)).where(
            Loan.sacco_id == sacco_id, Loan.status == LoanStatus.DISBURSED
        )
    )
    paid = db.session.scalar(
        select(func.coalesce(func.sum(LoanRepayment.amount), 0))
        .join(Loan, Loan.id == LoanRepayment.loan_id)
        .where(Loan.sacco_id == sacco_id, Loan.status == LoanStatus.DISBURSED)
    )
    reserved = db.session.scalar(
        select(func.coalesce(func.sum(Loan.principal), 0)).where(
            Loan.sacco_id == sacco_id,
            Loan.status.in_((LoanStatus.REQUESTED, LoanStatus.APPROVED)),
        )
    )
    outstanding = int(lent) - int(paid)
    return int(total) - 2 * outstanding - int(reserved)


OPEN_LOAN_STATUSES = (LoanStatus.REQUESTED, LoanStatus.APPROVED, LoanStatus.DISBURSED)


def check_loan_disburse(loan, savings: int) -> None:
    """Run the cap and pool checks again at payout. A loan is approved days after
    it was asked for: savings may have dropped and the pool may have been spent
    since. The caller holds the SACCO lock. A SACCO without rules is not checked."""
    rules = current_rules(loan.sacco_id)
    if rules is None:
        return
    most = rules.loan_cap_multiple * savings
    if loan.principal > most:
        raise Conflict(
            f"This loan is now above {rules.loan_cap_multiple} times the member's "
            f"savings. Their savings are {kes(savings)}, so the most is {kes(most)} "
            f"and the loan is {kes(loan.principal)}.",
            code=LOAN_OVER_CAP,
        )
    # This loan is APPROVED, so pool_cash already reserved it for itself.
    available = pool_cash(loan.sacco_id) + loan.principal
    if loan.principal > available:
        raise Conflict(
            f"The SACCO does not have enough cash for this loan now. It holds "
            f"{kes(max(available, 0))} and the loan is {kes(loan.principal)}.",
            code=LOAN_POOL_SHORT,
        )


def _cap_floor(account) -> tuple[int, int, int] | None:
    """(open loan principal, cap multiple, least savings allowed) or None when no
    rule applies: the SACCO has no rules or the member has no open loan."""
    rules = current_rules(account.sacco_id)
    if rules is None:
        return None
    principal = db.session.scalar(
        select(func.max(Loan.principal)).where(
            Loan.membership_id == account.membership_id,
            Loan.status.in_(OPEN_LOAN_STATUSES),
        )
    )
    if principal is None:
        return None
    multiple = rules.loan_cap_multiple
    return principal, multiple, -(-principal // multiple)  # principal / cap, rounded up


def check_withdrawal(account, amount: int) -> None:
    """Refuse a withdrawal that would leave the member's savings too small for
    the cap on a loan they still have open. The caller holds the SACCO lock."""
    floor = _cap_floor(account)
    if floor is None:
        return
    principal, multiple, keep = floor
    if account.balance - amount < keep:
        raise Conflict(
            f"You have an open loan of {kes(principal)}. This SACCO lets you borrow "
            f"{multiple} times your savings, so your savings must stay at "
            f"{kes(keep)} or more while it is open. You can withdraw up to "
            f"{kes(max(account.balance - keep, 0))}.",
            code=LOAN_OVER_CAP,
        )


def reversal_cap_problem(account, amount: int) -> str | None:
    """Why taking `amount` off this account by a reversal would break the cap of
    the holder's open loan, or None when it would not. The caller holds the
    SACCO lock."""
    floor = _cap_floor(account)
    if floor is None:
        return None
    principal, multiple, keep = floor
    if account.balance - amount >= keep:
        return None
    return (
        f"This reversal would leave the member's savings at "
        f"{kes(account.balance - amount)}. They have an open loan of "
        f"{kes(principal)} and this SACCO lets members borrow {multiple} times "
        f"their savings, so savings must stay at {kes(keep)} or more while it is "
        f"open. A SACCO admin can override this with a written reason."
    )


def check_loan_request(membership, savings: int, principal: int, term_months) -> None:
    """Refuse a loan that breaks the SACCO's rules, in plain words.
    A SACCO without published rules is not checked."""
    rules = current_rules(membership.sacco_id)
    if rules is None:
        return
    lock_sacco(membership.sacco_id)  # two requests cannot both spend the same pool
    if not has_accepted(membership.id, rules.id):
        raise Conflict(
            "Read and accept your SACCO's rules before asking for a loan.",
            code=RULES_NOT_ACCEPTED,
        )
    # Member since: when they first accepted the rules (at joining), not when they
    # were invited; an invitation can sit unused for months.
    first = db.session.scalar(
        select(func.min(RulesAcceptance.accepted_at)).where(
            RulesAcceptance.membership_id == membership.id
        )
    )
    joined = (first or membership.created_at).astimezone(UTC).date()
    waited = (today() - joined).days
    if waited < rules.min_membership_days:
        ready = joined.toordinal() + rules.min_membership_days
        raise Conflict(
            f"This SACCO lets members borrow after {rules.min_membership_days} days. "
            f"You have been a member for {waited}. You can ask from "
            f"{date.fromordinal(ready):%d %b %Y}.",
            code=LOAN_RULE_BROKEN,
        )
    days = rules.repayment_period_value * (
        30 if rules.repayment_period_unit == "months" else 1
    )
    if term_months is not None and term_months * 30 > days:
        raise Conflict(
            f"This SACCO's loans must be repaid within {rules.repayment_period_value} "
            f"{rules.repayment_period_unit}. You asked for {term_months} months.",
            code=LOAN_RULE_BROKEN,
        )
    most = rules.loan_cap_multiple * savings
    if principal > most:
        raise Conflict(
            f"You can borrow up to {rules.loan_cap_multiple} times your savings. Your "
            f"savings are {kes(savings)}, so the most is {kes(most)}. "
            f"You asked for {kes(principal)}.",
            code=LOAN_OVER_CAP,
        )
    pool = pool_cash(membership.sacco_id)
    if principal > pool:
        raise Conflict(
            f"The SACCO does not have enough cash for this loan right now. It holds "
            f"{kes(max(pool, 0))} and you asked for {kes(principal)}. "
            "Ask for less, or try "
            "again after more contributions come in.",
            code=LOAN_POOL_SHORT,
        )
