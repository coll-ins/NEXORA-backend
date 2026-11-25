"""Admin report and audit endpoints, scoped by the tenant guard."""

import re
from datetime import date

from flask import jsonify, request

from app import admin_bp
from app.admin import audit, reports
from app.audit.log import MAX_ACTION_LENGTH
from app.auth.limits import read_limit
from app.errors import error_response
from app.savings.accounts import list_sacco_accounts
from app.savings.history import page_params
from app.security.permissions import is_platform_owner
from app.security.tenant import (
    AccessDenied,
    current_user,
    permission_scoped,
)

MAX_DB_ID = 2_147_483_647  # largest id an INTEGER column holds


def _date_param(name: str) -> date | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    # fromisoformat also accepts 20260101 and 2026-W01-1; the API takes one form.
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
        raise ValueError(raw)
    return date.fromisoformat(raw)


def _audit_filters():
    """(action, start, end, page, per_page) from the query string.

    Raises ValueError with a message that is safe to show the client.
    """
    try:
        start, end = _date_param("from"), _date_param("to")
    except ValueError:
        raise ValueError("Dates must look like 2026-01-31.") from None
    if start and end and start > end:
        raise ValueError("'from' must not be after 'to'.")
    action = request.args.get("action") or None
    if action and len(action) > MAX_ACTION_LENGTH:
        raise ValueError(f"action cannot exceed {MAX_ACTION_LENGTH} characters.")
    if action and "\x00" in action:  # Postgres text cannot hold NUL
        raise ValueError("action must be plain text.")
    page, per_page = page_params()
    return action, start, end, page, per_page


def _sacco_id_param() -> int | None:
    raw = request.args.get("sacco_id")
    if raw is None or raw == "":
        return None
    if not (raw.isascii() and raw.isdecimal() and len(raw) <= 10):
        raise ValueError("sacco_id must be a whole number of 1 or more.")
    value = int(raw)
    if not 1 <= value <= MAX_DB_ID:
        raise ValueError("sacco_id must be a whole number of 1 or more.")
    return value


@admin_bp.get("/saccos/<int:sacco_id>/reports/summary")
@read_limit
@permission_scoped("report.view")
def sacco_summary(sacco_id: int):
    try:
        start, end = _date_param("from"), _date_param("to")
    except ValueError:
        return error_response(400, "Dates must look like 2026-01-31.")
    if start and end and start > end:
        return error_response(400, "'from' must not be after 'to'.")
    return jsonify(reports.sacco_summary(sacco_id, start, end))


@admin_bp.get("/saccos/<int:sacco_id>/reports/member-activity")
@read_limit
@permission_scoped("report.view")
def member_activity(sacco_id: int):
    try:
        page, per_page = page_params()
    except ValueError as exc:
        return error_response(400, str(exc))
    return jsonify(
        members=reports.member_activity(sacco_id, page, per_page),
        page=page,
        per_page=per_page,
    )


@admin_bp.get("/saccos/<int:sacco_id>/accounts")
@read_limit
@permission_scoped("savings.view_all")
def sacco_accounts(sacco_id: int):
    try:
        page, per_page = page_params()
    except ValueError as exc:
        return error_response(400, str(exc))
    accounts, total = list_sacco_accounts(sacco_id, page, per_page)
    return jsonify(accounts=accounts, page=page, per_page=per_page, total=total)


@admin_bp.get("/saccos/<int:sacco_id>/audit")
@read_limit
@permission_scoped("audit.view")  # secretary or platform owner; the admin gets 403
def sacco_audit(sacco_id: int):
    try:
        action, start, end, page, per_page = _audit_filters()
    except ValueError as exc:
        return error_response(400, str(exc))
    return jsonify(audit.list_audit(sacco_id, action, start, end, page, per_page))


@admin_bp.get("/audit")
@read_limit
def platform_audit():
    try:
        user = current_user()
    except AccessDenied as exc:
        return error_response(exc.status, exc.message)
    if not is_platform_owner(user):
        return error_response(403, "You are not allowed to do this.")
    try:
        action, start, end, page, per_page = _audit_filters()
        sacco_id = _sacco_id_param()
    except ValueError as exc:
        return error_response(400, str(exc))
    return jsonify(audit.list_audit(sacco_id, action, start, end, page, per_page))
