# ruff: noqa: F811  (pytest fixtures imported from test_daraja)
"""DARAJA_ENV: sandbox by default; production only with every guard met, only
with a SACCO's own credentials, and never sandbox credentials on the live URL."""

import pytest
from cryptography.fernet import Fernet

from app import _check_daraja_settings, create_app
from app.extensions import db
from app.models.stk_push import StkPush
from app.payments import credentials, daraja
from config import TestConfig
from test_daraja import clean_tables, on, push  # noqa: F401
from test_daraja_sacco import CREDS, keyed, set_payin, url  # noqa: F401
from test_member_profile import P
from test_sacco_detail import ADMIN, MEMBER, make_sacco

TOKEN = "Zk3_x9QvLm2-Pw8RtYb7NcHd5JfGs1Aa"
KEY = Fernet.generate_key().decode()
CALLBACK = f"https://nexora.example/api/v1/daraja/callback/{TOKEN}"


def prod_app(**overrides):
    """A production app that is safe apart from the overrides under test."""
    values = {
        "APP_ENV": "production",
        "SECRET_KEY": "p" * 40,
        "RATELIMIT_STORAGE_URI": "redis://localhost:6390/0",
        "CORS_ORIGINS": "https://app.example",
        "MAIL_BACKEND": "smtp",
        "MAIL_SERVER": "smtp.example",
        "DARAJA_ENABLED": True,
        "DARAJA_ENV": "production",
        "DARAJA_CALLBACK_TOKEN": TOKEN,
        "DARAJA_CALLBACK_URL": CALLBACK,
        "DARAJA_ENCRYPTION_KEY": KEY,
        # The shared sandbox keys are not needed in production.
        "DARAJA_CONSUMER_KEY": "",
        "DARAJA_CONSUMER_SECRET": "",
        "DARAJA_PASSKEY": "",
        "DARAJA_SHORTCODE": "",
    }
    values.update(overrides)
    return type("ProductionDaraja", (TestConfig,), values)


# ---- the environment is a setting ---------------------------------------------


def test_the_default_is_sandbox_and_the_base_urls_are_the_two_known_hosts(app):
    assert TestConfig.DARAJA_ENV == "sandbox"
    assert daraja.BASES == {
        "sandbox": "https://sandbox.safaricom.co.ke",
        "production": "https://api.safaricom.co.ke",
    }
    with app.app_context():
        assert daraja.base_url() == "https://sandbox.safaricom.co.ke"


@pytest.mark.parametrize(
    ("env", "host"),
    [("sandbox", "sandbox.safaricom.co.ke"), ("production", "api.safaricom.co.ke")],
)
def test_the_real_call_goes_to_the_host_of_the_environment(app, monkeypatch, env, host):
    import io
    import urllib.request

    seen = []

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(request, timeout=None):
        seen.append(request.full_url)
        return Reply(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    old = app.config["DARAJA_ENV"]
    app.config["DARAJA_ENV"] = env
    try:
        with app.app_context():
            daraja._call("/oauth/v1/generate")
    finally:
        app.config["DARAJA_ENV"] = old
    assert seen == [f"https://{host}/oauth/v1/generate"]


@pytest.mark.parametrize("bad", ["staging", "live", "Sandbox ", "1"])
def test_an_unknown_daraja_env_stops_the_app_even_when_daraja_is_off(bad):
    cfg = type("Bad", (TestConfig,), {"DARAJA_ENV": bad})
    if bad.strip().lower() == "sandbox":  # case and spaces are forgiven
        assert create_app(cfg)
    else:
        with pytest.raises(RuntimeError, match="DARAJA_ENV"):
            create_app(cfg)


# ---- the production guard -----------------------------------------------------


def test_a_complete_production_setup_starts():
    assert create_app(prod_app())


def test_production_without_daraja_enabled_starts_with_daraja_off(app):
    created = create_app(prod_app(DARAJA_ENABLED=False, DARAJA_ENV="sandbox"))
    with created.app_context():
        assert daraja.enabled() is False


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"DARAJA_ENV": "sandbox"}, "DARAJA_ENV=production"),
        ({"DARAJA_CALLBACK_URL": CALLBACK.replace("https", "http")}, "https"),
        ({"DARAJA_CALLBACK_URL": "https://nexora.example/api/v1/daraja/cb"}, "end in"),
        ({"DARAJA_CALLBACK_URL": ""}, "DARAJA_CALLBACK_URL"),
        ({"DARAJA_ENCRYPTION_KEY": ""}, "DARAJA_ENCRYPTION_KEY"),
        ({"DARAJA_ENCRYPTION_KEY": "not-a-fernet-key"}, "Fernet"),
        ({"DARAJA_CALLBACK_TOKEN": "short"}, "32 or more"),
    ],
)
def test_production_daraja_refuses_to_start_with_any_guard_unmet(override, match):
    with pytest.raises(RuntimeError, match=match):
        create_app(prod_app(**override))


def test_a_development_app_may_use_the_live_url_but_needs_the_key_too(app):
    old = {
        k: app.config.get(k)
        for k in ("DARAJA_ENV", "DARAJA_ENCRYPTION_KEY", "DARAJA_CALLBACK_URL")
    }
    app.config.update(
        DARAJA_ENV="production",
        DARAJA_ENCRYPTION_KEY="",
        DARAJA_CALLBACK_URL=CALLBACK,
        DARAJA_CALLBACK_TOKEN=TOKEN,
    )
    try:
        with pytest.raises(RuntimeError, match="DARAJA_ENCRYPTION_KEY"):
            _check_daraja_settings(app, "development")
    finally:
        app.config.update(old)


def test_enabled_in_production_needs_the_production_environment(app):
    old = {k: app.config[k] for k in ("DARAJA_ENABLED", "APP_ENV", "DARAJA_ENV")}
    try:
        app.config.update(DARAJA_ENABLED=True, APP_ENV="production")
        with app.app_context():
            app.config["DARAJA_ENV"] = "sandbox"
            assert daraja.enabled() is False
            app.config["DARAJA_ENV"] = "production"
            assert daraja.enabled() is True
            app.config["DARAJA_ENABLED"] = False
            assert daraja.enabled() is False
    finally:
        app.config.update(old)


# ---- no fallback to the shared keys, never sandbox keys on the live URL -------


@pytest.fixture
def live(app, on, keyed):
    """Production Daraja for one test, with the shared sandbox keys still set in
    the environment (a developer's .env, or a mistake): they must not be used."""
    old = app.config["DARAJA_ENV"]
    app.config["DARAJA_ENV"] = "production"
    yield on
    app.config["DARAJA_ENV"] = old


def save_creds(client, admin):
    done = client.put(url(admin), json=CREDS, headers=admin.headers)
    assert done.status_code == 200


def test_in_production_a_sacco_without_its_own_credentials_cannot_push(client, live):
    # `on` sets the shared sandbox keys: they exist, and must be ignored.
    p = P(make_sacco(), MEMBER)
    refused = push(client, p)
    assert refused.status_code == 409
    assert "not set up" in refused.json["error"]["message"]
    assert live == []  # nothing was sent to Safaricom
    assert db.session.query(StkPush).count() == 0


def test_the_sandbox_still_falls_back_to_the_shared_keys(client, on, keyed):
    assert credentials.for_sacco(make_sacco()) is not None  # development behaviour
    assert push(client, P(make_sacco(), MEMBER)).status_code == 201


def test_in_production_a_push_needs_the_approved_details(client, live):
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    save_creds(client, admin)
    refused = push(client, P(sacco, MEMBER))
    assert refused.status_code == 409
    assert "approve matching pay-in details" in refused.json["error"]["message"]
    assert live == []


def test_in_production_the_paybill_must_match_the_approved_one(client, live):
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    save_creds(client, admin)  # shortcode 600123
    set_payin(sacco, admin.user_id, "paybill", "600999")
    refused = push(client, P(sacco, MEMBER))
    assert refused.status_code == 409 and live == []


def test_in_production_a_matching_paybill_pushes_with_the_saccos_own_keys(client, live):
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    save_creds(client, admin)
    set_payin(sacco, admin.user_id, "paybill", "600123")
    done = push(client, P(sacco, MEMBER))
    assert done.status_code == 201
    payload, headers = live[0]
    assert payload["BusinessShortCode"] == payload["PartyB"] == "600123"
    assert "174379" not in str(payload)  # the shared sandbox shortcode


def test_in_production_a_till_is_prompted_at_the_approved_till(client, live):
    sacco = make_sacco()
    admin = P(sacco, ADMIN)
    save_creds(client, admin)
    set_payin(sacco, admin.user_id, "till", "555444")
    assert push(client, P(sacco, MEMBER)).status_code == 201
    payload, _ = live[0]
    assert payload["PartyB"] == "555444" and payload["BusinessShortCode"] == "600123"


def test_the_stk_call_itself_refuses_shared_keys_on_the_live_url(app):
    old = app.config["DARAJA_ENV"]
    app.config["DARAJA_ENV"] = "production"
    try:
        with app.app_context():
            with pytest.raises(daraja.DarajaError, match="own"):
                daraja.stk_push("254712345678", 10, "NX1")  # no credentials given
            shared = credentials.Credentials("174379", "k", "s", "p")  # own=False
            with pytest.raises(daraja.DarajaError, match="own"):
                daraja.stk_push("254712345678", 10, "NX1", shared)
    finally:
        app.config["DARAJA_ENV"] = old


def test_a_token_fetched_for_one_host_is_not_reused_for_the_other(app, monkeypatch):
    hosts = []

    def fake(path, payload=None, headers=None):
        hosts.append(daraja.base_url())
        return {"access_token": "t", "expires_in": "3599"}

    monkeypatch.setattr(daraja, "_call", fake)
    daraja._tokens.clear()
    old = app.config["DARAJA_ENV"]
    try:
        with app.app_context():
            daraja._bearer("k", "s")
            app.config["DARAJA_ENV"] = "production"
            daraja._bearer("k", "s")
    finally:
        app.config["DARAJA_ENV"] = old
        daraja._tokens.clear()
    assert hosts == [daraja.BASES["sandbox"], daraja.BASES["production"]]
