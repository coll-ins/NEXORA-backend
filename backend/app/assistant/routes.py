"""POST /assistant/chat: one question in, one plain-text answer out."""

import ipaddress

from flask import current_app, jsonify, request

from app import assistant_bp
from app.assistant import chat
from app.auth.limits import limiter
from app.errors import error_response
from app.security.tenant import AccessDenied, current_user

MAX_MESSAGE = 500
MAX_TURN = 1200  # >= chat.MAX_REPLY: the widget sends replies back as history
MAX_BODY = 32 * 1024  # 500 + 6 * 1200 characters at up to 4 bytes each
MAX_HISTORY = 6
BUSY = "The assistant has reached its free daily limit. Try again tomorrow."


def _signed_in_user():
    """The caller, or None when no Authorization header is sent (anonymous).

    A header that is present but invalid is an error, not anonymous, so the
    client refreshes its token instead of silently losing its account facts.
    """
    if not request.headers.get("Authorization"):
        return None
    return current_user()


def _ip_key() -> str:
    """The caller's address; IPv6 callers share one bucket per /64, because a host
    owns a whole /64 and could otherwise rotate addresses past every limit."""
    address = request.remote_addr or ""
    try:
        if ipaddress.ip_address(address).version == 6:
            return str(ipaddress.ip_network(f"{address}/64", strict=False))
    except ValueError:
        pass
    return address


def _user_key() -> str:
    """Per-person limit key: the user when signed in, else the address."""
    try:
        user = _signed_in_user()
    except AccessDenied:
        user = None
    return f"user:{user.id}" if user else f"ip:{_ip_key()}"


def _busy(request_limit):
    body, status = error_response(503, BUSY, code="assistant_busy")
    body.status_code = status
    return body


def _text(value, limit: int) -> str | None:
    """The stripped string, or None if empty, too long or not storable text."""
    if not isinstance(value, str) or "\x00" in value:
        return None
    try:
        value.encode()
    except UnicodeEncodeError:
        return None
    value = value.strip()
    return value if 0 < len(value) <= limit else None


def _history(raw) -> list[dict] | None:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_HISTORY:
        return None
    turns = []
    for item in raw:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            return None
        text = _text(item.get("text"), MAX_TURN)
        if text is None:
            return None
        turns.append({"role": item["role"], "text": text})
    return turns


def _daily_cap() -> str:
    return f"{current_app.config['ASSISTANT_DAILY_CAP']} per day"


@assistant_bp.post("/chat")
@limiter.limit("10 per minute", key_func=_ip_key)
@limiter.limit("30 per day", key_func=_ip_key)
@limiter.limit("20 per hour", key_func=_user_key)
# Shared by everyone, and only answers that reached a good reply use it up, so
# one abuser cannot burn the provider's free quota and junk costs nothing.
@limiter.limit(
    _daily_cap,
    key_func=lambda: "assistant",
    scope="assistant-global",
    deduct_when=lambda response: response.status_code < 400,
    on_breach=_busy,
)
def chat_route():
    # The app-wide 16 KB limit is under this route's legal maximum for non-ASCII text.
    request.max_content_length = MAX_BODY
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    message = _text(body.get("message"), MAX_MESSAGE)
    history = _history(body.get("history"))
    if message is None or history is None:
        return error_response(
            400,
            f"Send a message of 1 to {MAX_MESSAGE} characters and at most "
            f"{MAX_HISTORY} earlier turns.",
        )
    try:
        user = _signed_in_user()
    except AccessDenied as exc:
        return error_response(exc.status, exc.message)
    try:
        reply = chat.answer(message, history, user)
    except chat.AssistantUnavailable as exc:
        if exc.kind == "quota":
            return error_response(503, BUSY, code="assistant_busy")
        return error_response(
            503, "The assistant is unavailable right now.", code="assistant_unavailable"
        )
    return jsonify(reply=reply, signed_in=user is not None)
