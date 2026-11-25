"""Outgoing email: the verification and password-reset links.

MAIL_BACKEND=log writes the message to the application log (local development
only; production refuses it). MAIL_BACKEND=smtp sends through MAIL_SERVER.
"""

import logging
import smtplib
import threading
from collections.abc import Callable
from email.message import EmailMessage

from flask import current_app

log = logging.getLogger(__name__)


MESSAGES = {
    "verify": (
        "Confirm your NEXORA email",
        "verify-email",
        "Confirm your email address for NEXORA by opening this link within 24 hours:",
    ),
    "reset": (
        "Reset your NEXORA password",
        "reset-password",
        "Reset your NEXORA password by opening this link within 1 hour:",
    ),
}


def send_link_email(to: str, token: str, purpose: str = "verify") -> None:
    """Email a single-use link: verify an address, or reset a password."""
    config = current_app.config
    subject, page, intro = MESSAGES[purpose]
    link = f"{config['FRONTEND_URL'].rstrip('/')}/{page}?token={token}"
    body = f"{intro}\n\n{link}\n\nIf you did not ask for this, ignore this email."
    backend = config["MAIL_BACKEND"]
    if backend == "log":
        log.info("%s email to %s: %s", purpose, to, link)
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["MAIL_FROM"]
    message["To"] = to
    message.set_content(body)
    with smtplib.SMTP(config["MAIL_SERVER"], config["MAIL_PORT"], timeout=10) as smtp:
        smtp.starttls()
        if config["MAIL_USERNAME"]:
            smtp.login(config["MAIL_USERNAME"], config["MAIL_PASSWORD"])
        smtp.send_message(message)


def send_text_email(to: str, subject: str, body: str) -> None:
    """Send a plain message: logged on the log backend, delivered on smtp."""
    config = current_app.config
    if config["MAIL_BACKEND"] == "log":
        log.info("email to %s (%s):\n%s", to, subject, body)
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["MAIL_FROM"]
    message["To"] = to
    message.set_content(body)
    with smtplib.SMTP(config["MAIL_SERVER"], config["MAIL_PORT"], timeout=10) as smtp:
        smtp.starttls()
        if config["MAIL_USERNAME"]:
            smtp.login(config["MAIL_USERNAME"], config["MAIL_PASSWORD"])
        smtp.send_message(message)


def send_join_code_email(to: str, code: str, sacco_name: str, minutes: int) -> None:
    """Email a join code to the person an officer added by email."""
    body = (
        f"You were added to {sacco_name} on NEXORA.\n\n"
        f"Your join code is {code}\n\n"
        f"It works once and expires in {minutes} minutes. Sign in to NEXORA, open "
        "Join with a code, and enter it. Do not share it with anyone.\n\n"
        "If you did not expect this, ignore this email."
    )
    send_text_email(to, f"Your {sacco_name} join code", body)


def _send_in_background(deliver: Callable[[], None]) -> None:
    """A mail outage must never fail the request that asked for the email.

    The log backend runs `deliver` inline. SMTP runs it on a background thread
    with its own app context: a request for a known address would otherwise
    take visibly longer than one for an unknown address.
    """
    if current_app.config["MAIL_BACKEND"] != "smtp":
        deliver()
        return
    app = current_app._get_current_object()

    def work() -> None:
        with app.app_context():
            deliver()

    threading.Thread(target=work, daemon=True).start()


def send_join_code_safely(to: str, code: str, sacco_name: str, minutes: int) -> None:
    """Email a join code without letting a mail outage fail the request."""

    def deliver() -> None:
        try:
            send_join_code_email(to, code, sacco_name, minutes)
        except Exception:
            log.exception("Could not send the join code email")

    _send_in_background(deliver)


def _send_logged(to: str, token: str, purpose: str) -> None:
    try:
        send_link_email(to, token, purpose)
    except Exception:
        log.exception("Could not send the %s email", purpose)


def send_safely(to: str, token: str, purpose: str = "verify") -> None:
    """Email a single-use link without letting a mail outage fail the request."""
    _send_in_background(lambda: _send_logged(to, token, purpose))
