"""End-of-day status: one SACCO's ledger for one day, checked and emailed.

A mismatch is reported, never repaired: a stored balance that disagrees with its
ledger is a bug to investigate, and rewriting it would hide that.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import click
from flask.cli import with_appcontext
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.extensions import db
from app.ledger.reconcile import BalanceMismatch, run_nightly_reconciliation
from app.mail import send_text_email
from app.models.ledger import Transaction, TransactionType
from app.models.membership import MembershipStatus, SaccoMembership
from app.models.sacco import Sacco
from app.models.user import User
from app.security.permissions import can

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DayStatus:
    sacco_id: int
    day: date
    entries: int
    reversals: int
    deposits_total: int
    withdrawals_total: int
    accounts_checked: int
    mismatches: list[BalanceMismatch]

    @property
    def ok(self) -> bool:
        return not self.mismatches


def day_status(sacco_id: int, day: date) -> DayStatus:
    """Entries posted on `day` (UTC) and the balance check for every account."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    in_day = (
        Transaction.sacco_id == sacco_id,
        Transaction.created_at >= start,
        Transaction.created_at < start + timedelta(days=1),
    )

    undone = aliased(Transaction)

    def total(kind):
        """Same meaning as the admin summary: reversal rows and the rows they
        cancelled are both left out."""
        return db.session.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                *in_day,
                Transaction.type == kind,
                Transaction.reverses_id.is_(None),
                ~select(undone.id)
                .where(
                    undone.sacco_id == sacco_id,
                    undone.reverses_id == Transaction.id,
                )
                .exists(),
            )
        ).scalar_one()

    entries = db.session.execute(select(func.count()).where(*in_day)).scalar_one()
    reversals = db.session.execute(
        select(func.count()).where(*in_day, Transaction.reverses_id.is_not(None))
    ).scalar_one()
    report = run_nightly_reconciliation(sacco_id=sacco_id)
    return DayStatus(
        sacco_id=sacco_id,
        day=day,
        entries=entries,
        reversals=reversals,
        deposits_total=int(total(TransactionType.DEPOSIT)),
        withdrawals_total=int(total(TransactionType.WITHDRAWAL)),
        accounts_checked=report.accounts_checked,
        mismatches=report.mismatches,
    )


def render(status: DayStatus, sacco_name: str) -> tuple[str, str]:
    """(subject, body). Amounts are minor units, as everywhere in the API."""
    verdict = "OK" if status.ok else f"{len(status.mismatches)} MISMATCH(ES)"
    subject = f"{sacco_name}: end of day {status.day.isoformat()} ({verdict})"
    lines = [f"End-of-day status for {sacco_name}, {status.day.isoformat()} (UTC)."]
    if status.entries == 0:
        lines.append("No activity: no ledger entries were posted today.")
    else:
        lines += [
            f"Entries posted: {status.entries} (reversals: {status.reversals})",
            f"Deposits total (minor units): {status.deposits_total}",
            f"Withdrawals total (minor units): {status.withdrawals_total}",
        ]
    lines.append(f"Accounts checked against the ledger: {status.accounts_checked}")
    if status.ok:
        lines.append("Every stored balance matches its ledger.")
    else:
        lines.append("These accounts disagree with their ledger. Investigate:")
        lines += [
            f"  account {m.account_id}: stored {m.stored_balance}, "
            f"ledger {m.ledger_balance}"
            for m in status.mismatches
        ]
    return subject, "\n".join(lines)


def report_readers(sacco_id: int) -> list[str]:
    """Emails of the active members who hold `report.view` in this SACCO."""
    sacco = db.session.get(Sacco, sacco_id)
    users = db.session.execute(
        select(User)
        .join(SaccoMembership, SaccoMembership.user_id == User.id)
        .where(
            SaccoMembership.sacco_id == sacco_id,
            SaccoMembership.status == MembershipStatus.ACTIVE,
        )
        .order_by(User.email)
    ).scalars()
    return [u.email for u in users if can(u, sacco, "report.view")]


def send_day_status(sacco_id: int, day: date) -> DayStatus:
    """Check the SACCO, email its report readers, return the status."""
    sacco = db.session.get(Sacco, sacco_id)
    status = day_status(sacco_id, day)
    subject, body = render(status, sacco.name)
    for address in report_readers(sacco_id):
        try:
            send_text_email(address, subject, body)
        except Exception:
            log.exception("Could not send the end-of-day status to a report reader")
    return status


@click.command("eod-status")
@click.option("--sacco-id", type=int, required=True)
@click.option(
    "--date",
    "day",
    type=click.DateTime(["%Y-%m-%d"]),
    default=None,
    help="UTC day, YYYY-MM-DD. Default: today.",
)
@with_appcontext
def eod_status_command(sacco_id: int, day: datetime | None):
    """Check one SACCO's ledger for a day and email its report readers."""
    if db.session.get(Sacco, sacco_id) is None:
        raise click.ClickException(f"No SACCO with id {sacco_id}.")
    chosen = day.date() if day else datetime.now(UTC).date()
    status = send_day_status(sacco_id, chosen)
    click.echo(f"{status.entries} entries; mismatches={len(status.mismatches)}")
    if not status.ok:
        raise SystemExit(1)
