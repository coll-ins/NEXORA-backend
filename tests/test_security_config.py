"""CORS, security headers and the production startup guards."""

import pytest

from app import HSTS, create_app
from config import TestConfig

ORIGIN = "http://localhost:5173"
URL = "/api/v1/saccos"


def cors_app(origins=ORIGIN):
    return type("Cors", (TestConfig,), {"CORS_ORIGINS": origins})


def production_app(**overrides):
    """A production config that is safe apart from the overrides under test."""
    values = {
        "APP_ENV": "production",
        "SECRET_KEY": "p" * 40,
        "RATELIMIT_STORAGE_URI": "redis://localhost:6390/0",
        "CORS_ORIGINS": "https://app.example",
        "MAIL_BACKEND": "smtp",
        "MAIL_SERVER": "smtp.example",
    }
    values.update(overrides)
    return type("Production", (TestConfig,), values)


# ---------- CORS ----------


def test_an_allowed_origin_gets_the_headers():
    resp = create_app(cors_app()).test_client().get(URL, headers={"Origin": ORIGIN})
    assert resp.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "Origin" in resp.headers["Vary"]
    # Tokens travel in the Authorization header, so cookies are never allowed.
    assert "Access-Control-Allow-Credentials" not in resp.headers


def test_a_disallowed_origin_gets_nothing():
    resp = (
        create_app(cors_app())
        .test_client()
        .get(URL, headers={"Origin": "http://evil.example"})
    )
    assert "Access-Control-Allow-Origin" not in resp.headers
    assert "Origin" not in resp.headers.get("Vary", "")


def test_an_empty_list_adds_nothing():
    resp = create_app(cors_app("")).test_client().get(URL, headers={"Origin": ORIGIN})
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_a_preflight_is_204_with_methods_headers_and_max_age():
    resp = (
        create_app(cors_app())
        .test_client()
        .options(
            URL,
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )
    )
    assert resp.status_code == 204
    assert resp.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert resp.headers["Access-Control-Allow-Methods"] == (
        "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    )
    assert (
        resp.headers["Access-Control-Allow-Headers"] == "Authorization, Content-Type"
    )
    assert resp.headers["Access-Control-Max-Age"] == "600"


def test_a_preflight_from_a_disallowed_origin_is_not_204():
    resp = (
        create_app(cors_app())
        .test_client()
        .options(
            URL,
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
    )
    assert resp.status_code != 204
    assert "Access-Control-Allow-Origin" not in resp.headers


# ---------- security headers ----------


def test_every_api_response_is_uncacheable_and_unframeable(client):
    resp = client.get("/api/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Cache-Control"] == "no-store"


def test_an_error_response_keeps_the_headers(client):
    resp = client.get(f"{URL}/1")  # no token
    assert resp.status_code == 401
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"


def test_hsts_is_sent_in_production_only():
    prod = create_app(production_app()).test_client().get("/api/health")
    assert prod.headers["Strict-Transport-Security"] == HSTS
    dev = create_app(TestConfig).test_client().get("/api/health")
    assert "Strict-Transport-Security" not in dev.headers


@pytest.fixture(autouse=True)
def _no_debug_env(monkeypatch):
    """conftest loads backend/.env, which sets FLASK_DEBUG=1 for local work."""
    monkeypatch.delenv("FLASK_DEBUG", raising=False)


# ---------- production guards ----------


def test_a_safe_production_config_starts():
    assert create_app(production_app())


def test_debug_mode_stops_a_production_start(monkeypatch):
    monkeypatch.setenv("FLASK_DEBUG", "1")
    with pytest.raises(RuntimeError, match="FLASK_DEBUG"):
        create_app(production_app())


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"RATELIMIT_STORAGE_URI": "memory://"}, "memory://"),
        ({"SECRET_KEY": "too-short"}, "at least 32"),
        (
            {"SECRET_KEY": "replace-with-a-long-random-string"},
            ".env.example placeholder",
        ),
        ({"CORS_ORIGINS": "https://app.example,*"}, r"\*"),
        ({"MAIL_BACKEND": "log"}, "MAIL_BACKEND"),
        ({"MAIL_SERVER": ""}, "MAIL_SERVER"),
    ],
)
def test_an_unsafe_setting_stops_a_production_start(overrides, message):
    with pytest.raises(RuntimeError, match=message):
        create_app(production_app(**overrides))


def test_an_unknown_app_env_is_refused():
    with pytest.raises(RuntimeError, match="APP_ENV"):
        create_app(type("Typo", (TestConfig,), {"APP_ENV": "prod"}))


@pytest.mark.parametrize("secret", ["short", "x" * 31])
def test_a_short_secret_key_is_refused_in_every_environment(secret):
    class Dev(TestConfig):
        SECRET_KEY = secret

    with pytest.raises(RuntimeError, match="at least 32"):
        create_app(Dev)


# ---------- hosting: health check, proxy count, database URL ----------


def test_the_host_health_check_answers_ok_and_leaks_nothing(client):
    resp = client.get("/health")
    assert resp.status_code == 200 and resp.get_json() == {"status": "ok"}


def test_the_host_health_check_is_503_without_a_database_and_says_nothing(
    client, monkeypatch
):
    from app.extensions import db

    def down(*a, **k):
        raise RuntimeError("password authentication failed for user nexora_user")

    monkeypatch.setattr(db.session, "execute", down)
    resp = client.get("/health")
    assert resp.status_code == 503 and resp.get_json() == {"status": "unavailable"}
    assert "nexora" not in resp.get_data(as_text=True)


def _address_probe(proxies):
    cfg = type("Proxied", (TestConfig,), {"TRUSTED_PROXIES": proxies})
    app = create_app(cfg)
    from flask import request

    app.add_url_rule("/probe", "probe", lambda: request.remote_addr or "")
    return app.test_client()


def test_behind_one_proxy_the_client_is_the_address_the_proxy_appended():
    # A caller can send its own X-Forwarded-For; the proxy appends the real one.
    client = _address_probe("1")
    seen = client.get(
        "/probe", headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}
    ).get_data(as_text=True)
    assert seen == "203.0.113.9"


def test_with_no_proxy_a_forwarded_header_is_ignored():
    client = _address_probe("0")
    seen = client.get("/probe", headers={"X-Forwarded-For": "6.6.6.6"}).get_data(
        as_text=True
    )
    assert seen != "6.6.6.6"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        ("postgresql://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("", ""),
    ],
)
def test_a_plain_postgres_url_is_given_the_psycopg_driver(raw, expected):
    from config import database_url

    assert database_url(raw) == expected
