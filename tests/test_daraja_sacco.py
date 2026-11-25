# ruff: noqa: F811  (pytest fixtures imported from test_daraja)
"""A SACCO's own Daraja credentials (encrypted), the till request, and Pochi."""

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app.extensions import db
from app.models.audit import AuditLog
from app.models.daraja_settings import SaccoDarajaSettings
from app.models.payment_details import SaccoPaymentDetails
from app.models.stk_push import StkPush
from test_daraja import clean_tables, on, push  # noqa: F401
from test_member_profile import P
from test_sacco_detail import ADMIN, MEMBER, make_sacco

CREDS = {
    "shortcode": "600123",
    "consumer_key": "own-key-AAAA",
    "consumer_secret": "own-secret-BBBB",
    "passkey": "own-passkey-CCCC",
}


@pytest.fixture
def keyed(app):
    old = app.config.get("DARAJA_ENCRYPTION_KEY")
    app.config["DARAJA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    yield
    app.config["DARAJA_ENCRYPTION_KEY"] = old


def url(p):
    return f"/api/v1/saccos/{p.sacco_id}/daraja-settings"


def set_payin(sacco_id, user_id, kind, number):
    db.session.add(
        SaccoPaymentDetails(
            sacco_id=sacco_id,
            mpesa_type=kind,
            mpesa_number=number,
            display_name="Umoja",
            updated_by=user_id,
        )
    )
    db.session.commit()


def test_an_admin_saves_credentials_and_they_are_encrypted_at_rest(client, on, keyed):
    admin = P(make_sacco(), ADMIN)
    done = client.put(url(admin), json=CREDS, headers=admin.headers)
    assert done.status_code == 200 and done.json == {
        "configured": True,
        "shortcode": "600123",
    }
    row = db.session.query(SaccoDarajaSettings).one()
    stored = (row.consumer_key_enc, row.consumer_secret_enc, row.passkey_enc)
    assert all(v not in " ".join(stored) for v in list(CREDS.values())[1:])
    raw = db.session.execute(text("SELECT * FROM sacco_daraja_settings")).one()
    assert not any(v in str(tuple(raw)) for v in list(CREDS.values())[1:])
    seen = client.get(url(admin), headers=admin.headers)
    assert seen.json["configured"] and set(seen.json) == {
        "configured",
        "shortcode",
        "updated_at",
    }
    log = db.session.query(AuditLog).filter_by(action="daraja.credentials_set").one()
    assert not any(v in log.target for v in CREDS.values())


def test_a_member_cannot_read_or_save_credentials(client, on, keyed):
    sacco = make_sacco()
    member = P(sacco, MEMBER)
    assert (
        client.put(url(member), json=CREDS, headers=member.headers).status_code == 403
    )
    assert client.get(url(member), headers=member.headers).status_code == 403
    assert db.session.query(SaccoDarajaSettings).count() == 0


def test_saving_needs_the_key_and_valid_values(client, on, keyed, app):
    admin = P(make_sacco(), ADMIN)
    for bad in ({"shortcode": "12"}, {"passkey": "has space"}, {"consumer_key": ""}):
        res = client.put(url(admin), json={**CREDS, **bad}, headers=admin.headers)
        assert res.status_code == 400
    assert client.put(url(admin), json={}, headers=admin.headers).status_code == 400
    app.config["DARAJA_ENCRYPTION_KEY"] = ""
    res = client.put(url(admin), json=CREDS, headers=admin.headers)
    assert (
        res.status_code == 503
        and "DARAJA_ENCRYPTION_KEY" in res.json["error"]["message"]
    )
    assert db.session.query(SaccoDarajaSettings).count() == 0


def test_the_routes_do_not_exist_while_the_feature_is_off(client, keyed):
    admin = P(make_sacco(), ADMIN)
    assert client.put(url(admin), json=CREDS, headers=admin.headers).status_code == 404


def test_a_push_uses_the_saccos_own_credentials_for_a_paybill(client, on, keyed):
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    client.put(url(admin), json=CREDS, headers=admin.headers)
    set_payin(sacco, admin.user_id, "paybill", "600123")
    assert push(client, P(sacco, MEMBER)).status_code == 201
    payload, _ = on[0]
    assert payload["BusinessShortCode"] == payload["PartyB"] == "600123"
    assert payload["TransactionType"] == "CustomerPayBillOnline"


def test_a_till_is_prompted_as_buy_goods_with_the_head_office_shortcode(
    client, on, keyed
):
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    client.put(url(admin), json=CREDS, headers=admin.headers)
    set_payin(sacco, admin.user_id, "till", "555444")
    assert push(client, P(sacco, MEMBER)).status_code == 201
    payload, _ = on[0]
    assert payload["TransactionType"] == "CustomerBuyGoodsOnline"
    assert payload["BusinessShortCode"] == "600123" and payload["PartyB"] == "555444"


def test_pochi_is_record_only_and_never_prompted(client, on, keyed):
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    set_payin(sacco, admin.user_id, "pochi", "254712345678")
    res = push(client, P(sacco, MEMBER))
    assert res.status_code == 409 and "Pochi" in res.json["error"]["message"]
    assert on == [] and db.session.query(StkPush).count() == 0


def test_a_sacco_with_no_credentials_and_no_environment_ones_is_refused(
    client, on, app
):
    old = app.config["DARAJA_CONSUMER_KEY"]
    app.config["DARAJA_CONSUMER_KEY"] = ""
    try:
        res = push(client, P(make_sacco(), MEMBER))
    finally:
        app.config["DARAJA_CONSUMER_KEY"] = old
    assert res.status_code == 409 and on == []


def test_credentials_that_cannot_be_decrypted_are_a_clean_409(client, on, keyed, app):
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    client.put(url(admin), json=CREDS, headers=admin.headers)
    app.config["DARAJA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()  # lost key
    res = push(client, P(sacco, MEMBER))
    assert res.status_code == 409 and "save them again" in res.json["error"]["message"]
    assert on == [] and db.session.query(StkPush).count() == 0


def test_the_database_refuses_a_pochi_number_that_is_not_a_phone(app):
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    with pytest.raises(Exception, match="mpesa_number"):
        set_payin(sacco, admin.user_id, "pochi", "400200")
    db.session.rollback()


def test_a_pochi_pay_in_is_a_normalised_phone_number():
    from app.saccos import payment_details as logic

    pochi = {"mpesa_type": "pochi", "display_name": "Umoja"}
    values, errors = logic.validate({**pochi, "mpesa_number": "0712 345 678"})
    assert not errors and values["mpesa_number"] == "254712345678"
    for bad in ("400200", "0212345678", "254712"):
        _, errors = logic.validate({**pochi, "mpesa_number": bad})
        assert "mpesa_number" in errors
    _, errors = logic.validate(
        {**pochi, "mpesa_number": "0712345678", "account_reference_hint": "x"}
    )
    assert "account_reference_hint" in errors


def test_a_lone_admin_cannot_aim_prompts_at_a_shortcode_officers_never_approved(
    client, on, keyed
):
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    set_payin(sacco, admin.user_id, "paybill", "600123")  # two-officer approved
    client.put(url(admin), json={**CREDS, "shortcode": "999999"}, headers=admin.headers)
    res = push(client, P(sacco, MEMBER))
    assert res.status_code == 409 and "approved" in res.json["error"]["message"]
    assert on == [] and db.session.query(StkPush).count() == 0


def test_own_credentials_without_approved_pay_in_details_are_refused(client, on, keyed):
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    client.put(url(admin), json=CREDS, headers=admin.headers)
    assert push(client, P(sacco, MEMBER)).status_code == 409 and on == []


def test_officers_are_emailed_when_credentials_change_without_the_values(
    client, on, keyed, monkeypatch
):
    from app.saccos import payment_details as logic

    sent = []
    monkeypatch.setattr(logic, "send_text_email", lambda *a: sent.append(a))
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    client.put(url(admin), json=CREDS, headers=admin.headers)
    assert sent and all(v not in str(sent) for v in list(CREDS.values()))


@pytest.mark.parametrize("field", ["consumer_key", "consumer_secret", "passkey"])
def test_multibyte_secrets_are_a_clean_400_not_a_500(client, on, keyed, field):
    admin = P(make_sacco(), ADMIN)
    res = client.put(
        url(admin), json={**CREDS, field: "\U0001f600" * 300}, headers=admin.headers
    )
    assert res.status_code == 400


def test_two_first_saves_at_once_both_succeed(app, keyed, on):
    import threading

    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    codes = []

    def save():
        codes.append(
            app.test_client()
            .put(url(admin), json=CREDS, headers=admin.headers)
            .status_code
        )

    threads = [threading.Thread(target=save) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [200] * 8
    db.session.remove()
    assert db.session.query(SaccoDarajaSettings).count() == 1


def test_a_callback_with_a_nul_in_the_checkout_id_is_acknowledged(client, on):
    from test_daraja import CALLBACK

    res = client.post(
        CALLBACK, json={"Body": {"stkCallback": {"CheckoutRequestID": "a\u0000b"}}}
    )
    assert res.status_code == 200


@pytest.mark.parametrize("receipt", [None, "", {"a": 1}, 5, "   "])
def test_a_paid_answer_without_a_receipt_number_is_not_success(client, on, receipt):
    from test_daraja import CALLBACK

    p = P(make_sacco(), MEMBER)
    push(client, p, amount=10_000)
    items = [{"Name": "Amount", "Value": 100}]
    if receipt is not None:
        items.append({"Name": "MpesaReceiptNumber", "Value": receipt})
    body = {
        "Body": {
            "stkCallback": {
                "CheckoutRequestID": "ws_CO_1",
                "ResultCode": 0,
                "ResultDesc": "ok",
                "CallbackMetadata": {"Item": items},
            }
        }
    }
    assert client.post(CALLBACK, json=body).status_code == 200
    row = db.session.query(StkPush).one()
    assert row.status == "failed" and row.mpesa_receipt is None
