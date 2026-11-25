"""NEXORA configuration.

Secrets and connection strings come from environment variables only.
"""

import os
from datetime import timedelta


def database_url(raw: str) -> str:
    """Hosts such as Render hand out `postgres://` or `postgresql://`; SQLAlchemy
    needs the driver named, or it looks for psycopg2, which is not installed."""
    for plain in ("postgres://", "postgresql://"):
        if raw.startswith(plain):
            return "postgresql+psycopg://" + raw[len(plain) :]
    return raw


class Config:
    """Default settings used by `flask run`. Values come from .env or your shell."""

    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    SQLALCHEMY_DATABASE_URI = database_url(os.environ.get("DATABASE_URL", ""))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # A managed Postgres drops idle connections; without this the first request
    # after a quiet spell gets a dead pooled connection and fails with a 500.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    # Reverse proxies in front of the app (0 = none). Per-IP rate limits need the
    # real client address, so set this to the proxy count when deployed behind one.
    TRUSTED_PROXIES = os.environ.get("TRUSTED_PROXIES", "").strip() or "0"
    # Rate-limit counters. Use redis://host:6379/0 in production so every worker
    # shares them; memory:// is per-process and only fine for local development.
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_IN_MEMORY_FALLBACK_ENABLED = True  # a Redis outage must not stop logins
    # Fail fast when Redis is unreachable instead of waiting on the OS connect timeout.
    RATELIMIT_STORAGE_OPTIONS = {"socket_connect_timeout": 1, "socket_timeout": 1}
    MAX_CONTENT_LENGTH = 16 * 1024  # auth bodies are tiny; reject anything bigger

    # Auth values from the Day 4 plan. Refresh lifetime is a placeholder until Move 3.
    ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    REFRESH_TOKEN_EXPIRES = timedelta(days=7)
    LOCKOUT_THRESHOLD = 5
    LOCKOUT_DURATION = timedelta(minutes=15)

    # "development" or "production". Production refuses the unsafe settings that
    # are harmless locally; see _check_required_settings in app/__init__.py.
    APP_ENV = os.environ.get("APP_ENV", "development").strip().lower() or "development"
    # Exact browser origins allowed to call the API, comma-separated
    # (e.g. http://localhost:5173). Empty means no cross-origin access at all.
    CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "")

    # Email. "log" prints the verification link to the log (development only);
    # "smtp" sends it. FRONTEND_URL is where the link points.
    MAIL_BACKEND = os.environ.get("MAIL_BACKEND", "log").strip().lower() or "log"
    MAIL_FROM = os.environ.get("MAIL_FROM", "no-reply@nexora.local")
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

    # Help bot (Gemini free tier). The key lives only in the environment; with no
    # key the bot answers 503 and the rest of the app is unaffected. The daily cap
    # sits below the provider's free requests-per-day, so we refuse before it does.
    # Without Redis (in-memory fallback) each worker counts alone, so the real cap
    # is this number times the workers.
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    ASSISTANT_TIMEOUT = int(os.environ.get("ASSISTANT_TIMEOUT", "8"))
    ASSISTANT_DAILY_CAP = int(os.environ.get("ASSISTANT_DAILY_CAP", "800"))

    # M-Pesa STK push. Off unless DARAJA_ENABLED=1. DARAJA_ENV picks Safaricom's
    # sandbox (default) or live API; with APP_ENV=production only the live API is
    # allowed (see _check_daraja_settings in app/__init__.py).
    DARAJA_ENV = os.environ.get("DARAJA_ENV", "sandbox")
    DARAJA_ENABLED = os.environ.get("DARAJA_ENABLED", "").strip().lower() in (
        "1",
        "true",
    )
    DARAJA_CONSUMER_KEY = os.environ.get("DARAJA_CONSUMER_KEY", "")
    DARAJA_CONSUMER_SECRET = os.environ.get("DARAJA_CONSUMER_SECRET", "")
    DARAJA_PASSKEY = os.environ.get("DARAJA_PASSKEY", "")
    DARAJA_SHORTCODE = os.environ.get("DARAJA_SHORTCODE", "")
    # Encrypts each SACCO's own Daraja credentials (see .env.example).
    DARAJA_ENCRYPTION_KEY = os.environ.get("DARAJA_ENCRYPTION_KEY", "")
    # The public URL Safaricom posts to, ending in /api/v1/daraja/callback/<token>.
    DARAJA_CALLBACK_URL = os.environ.get("DARAJA_CALLBACK_URL", "")
    DARAJA_CALLBACK_TOKEN = os.environ.get("DARAJA_CALLBACK_TOKEN", "")

    TESTING = False


class TestConfig(Config):
    """Used by pytest. A separate database, so tests never touch development data."""

    TESTING = True
    RATELIMIT_ENABLED = False  # tests make many logins from one address
    SECRET_KEY = "test-only-not-a-real-secret-0123456789"
    DARAJA_ENABLED = False  # a test turns it on for itself; the default stays off
    DARAJA_ENV = "sandbox"  # whatever the developer's environment says
    # A developer's .env may hold real mail settings: tests must never send mail.
    MAIL_BACKEND = "log"
    MAIL_USERNAME = MAIL_PASSWORD = ""
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "")
    # A test that waits on a database lock for more than 15s fails with an error
    # instead of hanging forever (e.g. a stale connection left on the test database).
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"options": "-c lock_timeout=15000"}}
