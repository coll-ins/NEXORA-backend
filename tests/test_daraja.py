"""STK push: off by default, sandbox unless DARAJA_ENV says otherwise."""

import base64
from pathlib import Path

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models.audit import AuditLog
from app.models.savings import SavingsAccount
from app.models.stk_push import StkPush
from app.payments import daraja
from app.payments import routes as stk_routes
from config import TestConfig
from test_member_profile import P
from test_sacco_detail import ADMIN, MEMBER, make_sacco

TOKEN = "cb-secret-token"
ROOT = Path(__file__).resolve().parents[1]
CALLBACK = f"/api/v1/daraja/callback/{TOKEN}"


@pytest.fixture(autouse=True)
def clean_tables(app):
    def truncate():
        db.session.remove()
        db.session.execute(
            text("TRUNCATE refresh_tokens, users, saccos RESTART IDENTITY CASCADE")
        )
        db.session.commit()

    truncate()
    yield
    truncate()


@pytest.fixture
def on(app):
    """Switch the feature on for one test, with a stubbed network."""
    sent = []
    old = {k: app.config.get(k) for k in ("DARAJA_ENABLED", "DARAJA_CALLBACK_TOKEN")}
    app.config.update(
        DARAJA_ENABLED=True,
        DARAJA_CALLBACK_TOKEN=TOKEN,
        DARAJA_CONSUMER_KEY="k",
        DARAJA_CONSUMER_SECRET="s",
        DARAJA_PASSKEY="pass",
        DARAJA_SHORTCODE="174379",
        DARAJA_CALLBACK_URL="https://example.test" + CALLBACK,
    )
    daraja._tokens.clear()

    def fake(path, payload=None, headers=None):
        if "oauth" in path:
            return {"access_token": "tok", "expires_in": "3599"}
        sent.append((payload, headers))
        return {"ResponseCode": "0", "CheckoutRequestID": f"ws_CO_{len(sent)}"}

    daraja._call = fake
    yield sent
    app.config.update(old)
    daraja._call = _real_call


_real_call = daraja._call


def push(client, p, amount=10_000, phone="0712345678"):
    return client.post(
        f"/api/v1/saccos/{p.sacco_id}/stk-push",
        json={"amount": amount, "phone": phone},
        headers=p.headers,
    )


def answer(client, checkout, code=0, amount=100, receipt="QWE123ABCD", token=TOKEN):
    body = {
        "Body": {
            "stkCallback": {
                "CheckoutRequestID": checkout,
                "ResultCode": code,
                "ResultDesc": "ok" if code == 0 else "Request cancelled by user",
                **(
                    {
                        "CallbackMetadata": {
                            "Item": [
                                {"Name": "Amount", "Value": amount},
                                {"Name": "MpesaReceiptNumber", "Value": receipt},
                            ]
                        }
                    }
                    if code == 0
                    else {}
                ),
            }
        }
    }
    return client.post(f"/api/v1/daraja/callback/{token}", json=body)


def test_it_is_off_by_default_and_the_routes_do_not_exist(client):
    # Config reads the developer's own .env, so the shipped default is checked.
    assert TestConfig.DARAJA_ENABLED is False
    assert "DARAJA_ENABLED=0" in (ROOT / "backend/.env.example").read_text()
    p = P(make_sacco(), MEMBER)
    assert push(client, p).status_code == 404
    assert client.post(CALLBACK, json={}).status_code == 404
    assert (
        client.get(
            f"/api/v1/saccos/{p.sacco_id}/stk-push/1", headers=p.headers
        ).status_code
        == 404
    )
    assert db.session.query(StkPush).count() == 0


def test_it_refuses_production_and_a_half_configured_setup(app):
    from app import _check_daraja_settings

    with pytest.raises(RuntimeError, match="DARAJA_ENV=production"):
        _check_daraja_settings(app, "production")  # a sandbox env in a production app
    old = app.config["DARAJA_CONSUMER_KEY"]
    app.config["DARAJA_CONSUMER_KEY"] = ""  # a developer's .env may set it
    try:
        with pytest.raises(RuntimeError, match="DARAJA_CONSUMER_KEY"):
            _check_daraja_settings(app, "development")
    finally:
        app.config["DARAJA_CONSUMER_KEY"] = old
    assert not daraja.enabled()  # off in the test config


def test_enabled_is_false_in_production_even_if_the_flag_is_set(app):
    old = (app.config["DARAJA_ENABLED"], app.config["APP_ENV"])
    app.config.update(DARAJA_ENABLED=True, APP_ENV="production")
    try:
        with app.app_context():
            assert daraja.enabled() is False
    finally:
        app.config.update(DARAJA_ENABLED=old[0], APP_ENV=old[1])


def test_a_member_starts_a_push_and_daraja_gets_the_right_request(client, on):
    p = P(make_sacco(), MEMBER)
    done = push(client, p, amount=25_000)
    assert done.status_code == 201, done.json
    assert done.json["status"] == "pending" and done.json["phone_last3"] == "678"
    payload, headers = on[0]
    assert headers == {"Authorization": "Bearer tok"}
    assert payload["Amount"] == 250 and payload["PhoneNumber"] == "254712345678"
    assert payload["BusinessShortCode"] == payload["PartyB"] == "174379"
    stamp = payload["Timestamp"]
    assert base64.b64decode(payload["Password"]).decode() == f"174379pass{stamp}"
    assert payload["CallBackURL"].endswith(CALLBACK)
    assert len(payload["AccountReference"]) <= 12


@pytest.mark.parametrize("amount", [0, -100, 150, 15_000_100, "100", 100.0, True, None])
def test_a_bad_amount_is_400_and_nothing_is_sent(client, on, amount):
    p = P(make_sacco(), MEMBER)
    assert push(client, p, amount=amount).status_code == 400
    assert on == [] and db.session.query(StkPush).count() == 0


@pytest.mark.parametrize(
    "phone", ["", "123", "0812345678", "+254 7123", None, 712345678]
)
def test_a_bad_phone_is_400(client, on, phone):
    p = P(make_sacco(), MEMBER)
    assert push(client, p, phone=phone).status_code == 400


@pytest.mark.parametrize(
    ("given", "wire"),
    [
        ("0712345678", "254712345678"),
        ("+254712345678", "254712345678"),
        ("254 7123", None),
        ("0112345678", "254112345678"),
    ],
)
def test_phone_numbers_become_254_form(given, wire):
    assert daraja.msisdn(given) == wire


def test_only_a_member_can_start_one_and_an_officer_cannot_for_someone(client, on):
    sacco = make_sacco()
    assert push(client, P(sacco, ADMIN)).status_code == 403  # no savings.request
    assert push(client, P(make_sacco(), MEMBER)).status_code == 201
    stranger = P(make_sacco(), MEMBER)
    other = client.post(
        f"/api/v1/saccos/{sacco}/stk-push",
        json={"amount": 100, "phone": "0712345678"},
        headers=stranger.headers,
    )
    assert other.status_code == 404


def test_a_network_failure_is_502_and_recorded_as_failed(client, on):
    def down(*a, **k):
        raise daraja.DarajaError("M-Pesa did not answer. Try again in a moment.")

    daraja._call = down
    p = P(make_sacco(), MEMBER)
    assert push(client, p).status_code == 502
    (row,) = db.session.query(StkPush).all()
    assert row.status == "failed"


def test_a_refusal_from_daraja_is_502(client, on):
    daraja._call = lambda path, payload=None, headers=None: (
        {"access_token": "t"} if "oauth" in path else {"ResponseCode": "1"}
    )
    assert push(client, P(make_sacco(), MEMBER)).status_code == 502


def test_a_successful_answer_is_recorded_once_and_credits_nothing(client, on):
    p = P(make_sacco(), MEMBER)
    push(client, p, amount=10_000)
    before = db.session.query(SavingsAccount).count()
    assert answer(client, "ws_CO_1").json == daraja_ack()
    db.session.expire_all()
    row = db.session.query(StkPush).one()
    assert (row.status, row.mpesa_receipt) == ("succeeded", "QWE123ABCD")
    # A replay of the same callback changes nothing.
    answer(client, "ws_CO_1", code=1)
    db.session.expire_all()
    assert db.session.query(StkPush).one().status == "succeeded"
    assert db.session.query(SavingsAccount).count() == before
    assert (
        db.session.query(AuditLog).filter_by(action="payment.stk_result").count() == 1
    )
    got = client.get(
        f"/api/v1/saccos/{p.sacco_id}/stk-push/{row.id}", headers=p.headers
    )
    assert got.json["status"] == "succeeded"


def daraja_ack():
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


def test_a_cancelled_payment_is_failed_with_the_reason(client, on):
    push(client, P(make_sacco(), MEMBER))
    answer(client, "ws_CO_1", code=1032)
    db.session.expire_all()
    row = db.session.query(StkPush).one()
    assert row.status == "failed" and "cancelled" in row.result_desc


def test_a_paid_amount_that_differs_is_not_counted_as_success(client, on):
    push(client, P(make_sacco(), MEMBER), amount=10_000)
    answer(client, "ws_CO_1", amount=1)
    db.session.expire_all()
    row = db.session.query(StkPush).one()
    assert row.status == "failed" and "did not match" in row.result_desc


def test_a_wrong_token_or_unknown_checkout_changes_nothing(client, on):
    push(client, P(make_sacco(), MEMBER))
    assert answer(client, "ws_CO_1", token="guess").status_code == 404
    assert answer(client, "ws_CO_999").status_code == 200  # acknowledged, ignored
    assert client.post(CALLBACK, json={"junk": 1}).status_code == 200
    assert client.post(CALLBACK, data="not json").status_code == 200
    db.session.expire_all()
    assert db.session.query(StkPush).one().status == "pending"


def test_a_receipt_number_cannot_close_two_pushes(client, on):
    p = P(make_sacco(), MEMBER)
    push(client, p)
    push(client, p)
    answer(client, "ws_CO_1")
    answer(client, "ws_CO_2")  # the same receipt on a second push
    db.session.expire_all()
    first, second = db.session.query(StkPush).order_by(StkPush.id).all()
    assert (first.status, second.status) == ("succeeded", "failed")
    # Both answers are on the audit trail, the failed one too.
    assert (
        db.session.query(AuditLog).filter_by(action="payment.stk_result").count() == 2
    )


def test_only_the_owner_or_a_manager_reads_a_push(client, on):
    sacco = make_sacco()
    me, other, admin = P(sacco, MEMBER), P(sacco, MEMBER), P(sacco, ADMIN)
    push(client, me)
    url = f"/api/v1/saccos/{sacco}/stk-push/1"
    assert client.get(url, headers=me.headers).status_code == 200
    assert client.get(url, headers=admin.headers).status_code == 200
    assert client.get(url, headers=other.headers).status_code == 404


def test_odd_callbacks_are_never_a_500(client, on):
    assert client.post("/api/v1/daraja/callback/t%C3%A9st", json={}).status_code == 404
    for body in (
        [1],
        "x",
        5,
        {"Body": []},
        {"Body": {"stkCallback": []}},
        {"Body": {"stkCallback": {"CallbackMetadata": {"Item": "x"}}}},
    ):
        assert client.post(CALLBACK, json=body).status_code == 200


def test_a_reply_that_is_not_a_json_object_is_a_clean_502(client, on, monkeypatch):
    import io
    import urllib.request

    daraja._call = _real_call  # exercise the real parsing, with a fake wire
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"[1, 2]")
    )
    done = push(client, P(make_sacco(), MEMBER))
    assert done.status_code == 502
    assert db.session.query(StkPush).one().status == "failed"


def test_a_member_cannot_stack_prompts_on_phones(client, on):
    p = P(make_sacco(), MEMBER)
    assert [push(client, p).status_code for _ in range(3)] == [201, 201, 201]
    assert push(client, p).status_code == 429
    assert len(on) == 3  # the fourth never reached M-Pesa


def test_the_row_is_saved_before_daraja_is_called(client, on):
    seen = []
    real = daraja._call

    def peek(path, payload=None, headers=None):
        if "processrequest" in path:
            db.session.rollback()
            seen.append(db.session.query(StkPush).count())
        return real(path, payload, headers)

    daraja._call = peek
    push(client, P(make_sacco(), MEMBER))
    assert seen == [1]


def test_a_connection_reset_while_reading_daraja_is_a_clean_502(client, on):
    def reset(path, payload=None, headers=None):
        if "oauth" in path:
            return {"access_token": "t"}
        raise ConnectionResetError("reset by peer")

    daraja._call = reset
    assert push(client, P(make_sacco(), MEMBER)).status_code == 502
    assert db.session.query(StkPush).one().status == "failed"


def test_real_read_failures_become_a_daraja_error(monkeypatch):
    import http.client
    import urllib.request

    for boom in (ConnectionResetError(), http.client.IncompleteRead(b"")):

        def fail(*a, _boom=boom, **k):
            raise _boom

        monkeypatch.setattr(urllib.request, "urlopen", fail)
        with pytest.raises(daraja.DarajaError):
            _real_call("/x")


@pytest.mark.parametrize(
    "token", ["x", "short-token", "a" * 31, "a" * 31 + "!", "é" * 40]
)
def test_a_weak_callback_token_is_refused_at_startup(app, token):
    from app import _check_daraja_settings

    old = {k: app.config.get(k) for k in ("DARAJA_CALLBACK_TOKEN",)}
    app.config.update(
        DARAJA_CONSUMER_KEY="k",
        DARAJA_CONSUMER_SECRET="s",
        DARAJA_PASSKEY="p",
        DARAJA_SHORTCODE="1",
        DARAJA_CALLBACK_URL="https://x",
        DARAJA_CALLBACK_TOKEN=token,
    )
    try:
        with pytest.raises(RuntimeError, match="32 or more"):
            _check_daraja_settings(app, "development")
        good = "Zk3_x9QvLm2-Pw8RtYb7NcHd5JfGs1Aa"
        app.config.update(
            DARAJA_CALLBACK_TOKEN=good,
            DARAJA_CALLBACK_URL=f"https://x.test/api/v1/daraja/callback/{good}",
        )
        _check_daraja_settings(app, "development")  # a good one passes
    finally:
        app.config.update(old)


@pytest.mark.parametrize(
    "url",
    [
        "http://x.test/api/v1/daraja/callback/Zk3_x9QvLm2-Pw8RtYb7NcHd5JfGs1Aa",
        "https://x.test/api/v1/daraja/callback/another-token-value-here-0123456",
        "https://x.test/",
    ],
)
def test_a_callback_url_without_the_token_or_https_is_refused(app, url):
    from app import _check_daraja_settings

    old = app.config.get("DARAJA_CALLBACK_TOKEN")
    app.config.update(
        DARAJA_CONSUMER_KEY="k",
        DARAJA_CONSUMER_SECRET="s",
        DARAJA_PASSKEY="p",
        DARAJA_SHORTCODE="1",
        DARAJA_CALLBACK_URL=url,
        DARAJA_CALLBACK_TOKEN="Zk3_x9QvLm2-Pw8RtYb7NcHd5JfGs1Aa",
    )
    try:
        with pytest.raises(RuntimeError, match="DARAJA_CALLBACK_URL"):
            _check_daraja_settings(app, "development")
    finally:
        app.config["DARAJA_CALLBACK_TOKEN"] = old


def test_a_boolean_amount_does_not_count_as_one_shilling(client, on):
    push(client, P(make_sacco(), MEMBER), amount=100)
    answer(client, "ws_CO_1", amount=True)
    db.session.expire_all()
    assert db.session.query(StkPush).one().status == "failed"


def test_a_push_with_no_answer_stops_being_pending_after_15_minutes(client, on):
    p = P(make_sacco(), MEMBER)
    push(client, p)
    url = f"/api/v1/saccos/{p.sacco_id}/stk-push/1"
    assert client.get(url, headers=p.headers).json["status"] == "pending"
    db.session.execute(
        text("UPDATE stk_pushes SET created_at = now() - interval '16 minutes'")
    )
    db.session.commit()
    got = client.get(url, headers=p.headers).json
    assert got["status"] == "failed" and "did not answer" in got["message"]


def test_sacco_login_is_registered_once(app):
    rules = [
        r for r in app.url_map.iter_rules() if r.rule.endswith("/auth/sacco-login")
    ]
    assert len(rules) == 1


def test_stale_read_must_not_overwrite_a_callback_that_lands_meanwhile(
    client, on, monkeypatch
):
    """GET marks a 15-minute-old pending push failed with an unconditional
    UPDATE. A success callback committed after GET read the row but before GET
    commits is overwritten: M-Pesa took the money, the record says failed."""
    p = P(make_sacco(), MEMBER)
    push(client, p)
    db.session.execute(
        text("UPDATE stk_pushes SET created_at = now() - interval '20 minutes'")
    )
    db.session.commit()
    row_id = db.session.query(StkPush).one().id
    real = stk_routes.datetime

    class Stub:
        fired = False

        @staticmethod
        def now(tz=None):
            if not Stub.fired:
                Stub.fired = True
                with db.engine.begin() as other:  # the callback commits here
                    other.execute(
                        text(
                            "UPDATE stk_pushes SET status='succeeded', "
                            "mpesa_receipt='ABC1234567' WHERE id=:i"
                        ),
                        {"i": row_id},
                    )
            return real.now(tz)

    monkeypatch.setattr(stk_routes, "datetime", Stub)
    client.get(f"/api/v1/saccos/{p.sacco_id}/stk-push/{row_id}", headers=p.headers)
    db.session.remove()
    assert db.session.query(StkPush).one().status == "succeeded"


def _cb(**extra):
    return {"Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_1", **extra}}}


def _items(*pairs):
    return {"Item": [{"Name": n, "Value": v} for n, v in pairs]}


@pytest.mark.parametrize(
    "body",
    [
        _cb(ResultCode=0, CallbackMetadata={"Item": [{"Name": ["x"], "Value": 1}]}),
        _cb(ResultCode=1, ResultDesc="bad\u0000desc"),
        _cb(ResultCode=1, ResultDesc=["x"]),
        _cb(
            ResultCode=0,
            CallbackMetadata=_items(("Amount", 1), ("MpesaReceiptNumber", "A\u0000B")),
        ),
        _cb(
            ResultCode=0,
            CallbackMetadata=_items(("Amount", 1), ("MpesaReceiptNumber", {"a": 1})),
        ),
        _cb(ResultCode=0, CallbackMetadata=_items(("Amount", float("nan")))),
        _cb(ResultCode=10**40),
        _cb(ResultCode=1, ResultDesc="\ud800"),
    ],
)
def test_a_malformed_callback_never_fails_with_500(client, on, body):
    push(client, P(make_sacco(), MEMBER), amount=100)
    assert client.post(CALLBACK, json=body).status_code == 200


@pytest.mark.parametrize("code", [False, 0.0, "0", None])
def test_only_the_integer_zero_means_paid(client, on, code):
    push(client, P(make_sacco(), MEMBER), amount=100)
    client.post(
        CALLBACK,
        json=_cb(
            ResultCode=code,
            CallbackMetadata=_items(
                ("Amount", 1), ("MpesaReceiptNumber", "ABC1234567")
            ),
        ),
    )
    db.session.expire_all()
    assert db.session.query(StkPush).one().status == "failed"


def test_a_late_success_upgrades_a_push_the_15_minute_rule_closed(client, on):
    p = P(make_sacco(), MEMBER)
    push(client, p, amount=100)
    db.session.execute(
        text("UPDATE stk_pushes SET created_at = now() - interval '20 minutes'")
    )
    db.session.commit()
    client.get(f"/api/v1/saccos/{p.sacco_id}/stk-push/1", headers=p.headers)
    answer(client, "ws_CO_1", amount=1)
    db.session.expire_all()
    assert db.session.query(StkPush).one().status == "succeeded"
    # An answer that failed for any other reason stays final.
    push(client, p, amount=100)
    answer(client, "ws_CO_2", code=1032)
    answer(client, "ws_CO_2", amount=1, receipt="ZZZ9999999")
    db.session.expire_all()
    assert db.session.query(StkPush).order_by(StkPush.id).all()[1].status == "failed"
