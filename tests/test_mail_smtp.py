"""The smtp mail backend, with the SMTP connection replaced by a recorder."""

import threading

import pytest

from app import mail


class FakeSMTP:
    sent = []
    steps = []
    fail = False

    def __init__(self, host, port, timeout):
        self.steps.append(("connect", host, port, timeout))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.steps.append(("starttls",))

    def login(self, user, password):
        self.steps.append(("login", user))

    def send_message(self, message):
        if self.fail:
            raise OSError("smtp down")
        self.sent.append(message)


@pytest.fixture
def smtp(app, monkeypatch):
    FakeSMTP.sent, FakeSMTP.steps, FakeSMTP.fail = [], [], False
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    app.config.update(
        MAIL_BACKEND="smtp",
        MAIL_SERVER="smtp.example.test",
        MAIL_PORT=587,
        MAIL_USERNAME="mailer",
        MAIL_PASSWORD="not-a-real-password",
        MAIL_FROM="nexora@example.test",
        FRONTEND_URL="https://app.example.test/",
    )
    with app.app_context():
        yield FakeSMTP


def test_a_link_email_is_sent_over_tls_with_a_login(smtp):
    mail.send_link_email("a@example.com", "tok123", "reset")
    assert [step[0] for step in smtp.steps] == ["connect", "starttls", "login"]
    message = smtp.sent[0]
    assert message["To"] == "a@example.com" and message["Subject"].startswith("Reset")
    assert (
        "https://app.example.test/reset-password?token=tok123" in message.get_content()
    )


def test_a_text_email_is_sent_and_skips_the_login_without_a_username(smtp, app):
    app.config["MAIL_USERNAME"] = ""
    mail.send_text_email("b@example.com", "Subject here", "Body here")
    assert [step[0] for step in smtp.steps] == ["connect", "starttls"]
    assert smtp.sent[0]["Subject"] == "Subject here"


def test_the_log_backend_sends_nothing(smtp, app, caplog):
    app.config["MAIL_BACKEND"] = "log"
    with caplog.at_level("INFO", logger=mail.log.name):
        mail.send_text_email("c@example.com", "S", "B")
        mail.send_link_email("c@example.com", "tok", "verify")
    assert smtp.sent == []


def test_a_failed_send_is_logged_not_raised(smtp, caplog, monkeypatch):
    # Migrations run earlier in the suite call logging.fileConfig, which disables
    # loggers that already exist.
    monkeypatch.setattr(mail.log, "disabled", False)
    smtp.fail = True
    mail._send_logged("d@example.com", "tok", "verify")
    assert "Could not send the verify email" in caplog.text


def test_send_safely_goes_out_on_a_background_thread(smtp):
    threads = []
    original = smtp.send_message

    def record(self, message):
        threads.append(threading.current_thread())
        original(self, message)

    smtp.send_message = record
    try:
        mail.send_safely("e@example.com", "tok", "verify")
        for thread in threading.enumerate():
            if thread.daemon and thread is not threading.current_thread():
                thread.join(timeout=5)
    finally:
        smtp.send_message = original
    assert threads and threads[0] is not threading.main_thread()
