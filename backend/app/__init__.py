"""NEXORA application factory."""

import os
import re

import psycopg
from cryptography.fernet import Fernet
from flask import Blueprint, Flask, current_app, jsonify, request
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import IntegerConverter

from app.auth.limits import init_limiter, read_limit
from app.cors import init_cors
from app.errors import error_response
from app.extensions import db, migrate
from config import Config

API_PREFIX = "/api/v1"  # the one place the version prefix is set

# Empty blueprints for Move 1. Routes are added in Moves 3 to 5.
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
saccos_bp = Blueprint("saccos", __name__, url_prefix="/saccos")
savings_bp = Blueprint("savings", __name__, url_prefix="/savings")
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
members_bp = Blueprint("members", __name__, url_prefix="/members")
loans_bp = Blueprint("loans", __name__, url_prefix="/loans")
support_bp = Blueprint("support", __name__, url_prefix="/support-access")
daraja_bp = Blueprint("daraja", __name__, url_prefix="/daraja")
assistant_bp = Blueprint("assistant", __name__, url_prefix="/assistant")

BLUEPRINTS = (
    auth_bp,
    saccos_bp,
    savings_bp,
    admin_bp,
    members_bp,
    loans_bp,
    support_bp,
    daraja_bp,
    assistant_bp,
)
MAX_ID = 2**31 - 1  # ids are 32-bit integers in Postgres

PRODUCTION = "production"
# Only these two values are meaningful. Anything else is a typo that would
# silently switch the production guards off.
APP_ENVS = ("development", PRODUCTION)
# From .env.example: a secret that must never reach production.
MIN_SECRET_LENGTH = 32
EXAMPLE_SECRET_KEY = "replace-with-a-long-random-string"
DEBUG_VALUES = {"1", "true", "yes", "on"}
HSTS = "max-age=31536000; includeSubDomains"
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)


class IdConverter(IntegerConverter):
    """`<int:...>` accepts plain ASCII ids only. Anything else becomes 0, which no
    row has, so every lookup answers its normal 404 instead of a 500.

    Refused: ids Postgres cannot hold, leading zeros and non-ASCII digits (one
    record, one URL). Mapping instead of rejecting, because werkzeug would
    answer 405 for a rejected PATCH id.
    """

    def to_python(self, value: str) -> int:
        plain = value.isascii() and len(value) <= 10 and not value.startswith("0")
        number = int(value) if plain else 0
        return number if number <= MAX_ID else 0


def _json_http_error(exc: HTTPException):
    """Every HTTP error (404, 405, 413, ...) answers in the API's JSON shape."""
    # A limiter callback may attach its own response with another status (the help
    # bot's 503 "daily limit reached"); it is already in the JSON shape.
    embedded = getattr(exc, "response", None)
    if embedded is not None and embedded.status_code != exc.code:
        return embedded
    if exc.code == 429:
        message = "Too many requests. Try again later."
    else:
        message = "Not found." if exc.code == 404 else f"{exc.name}."
    body, status = error_response(exc.code or 500, message)
    response = exc.get_response()
    response.data = body.data
    response.content_type = "application/json"
    return response


def _json_server_error(exc: Exception):
    """Anything unexpected answers in the API's JSON shape, with no details."""
    app = current_app
    app.logger.exception("Unhandled error", exc_info=exc)
    return error_response(500, "Internal error.")


def _unencodable_text(exc: UnicodeEncodeError):
    """A lone surrogate (for example \\ud800) in a text field cannot be stored."""
    db.session.rollback()
    return error_response(400, "Text contains characters that cannot be stored.")


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    _check_required_settings(app)
    proxies = str(app.config["TRUSTED_PROXIES"])
    if not proxies.isdecimal():
        raise RuntimeError("TRUSTED_PROXIES must be 0 or a positive whole number.")
    if int(proxies):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=int(proxies))

    app.url_map.converters["int"] = IdConverter
    app.register_error_handler(HTTPException, _json_http_error)
    app.register_error_handler(UnicodeEncodeError, _unencodable_text)
    app.register_error_handler(Exception, _json_server_error)
    db.init_app(app)
    migrate.init_app(app, db)

    # Import models so Alembic's autogenerate can see every table.
    from app.admin import owners as admin_owners  # noqa: F401
    from app.admin import routes as admin_routes  # noqa: F401
    from app.assistant import routes as assistant_routes  # noqa: F401

    # Importing the routes attaches them to auth_bp.
    from app.auth import routes  # noqa: F401

    # The ledger and history routes import from the savings routes, so they come after.
    from app.ledger import routes as ledger_routes  # noqa: F401
    from app.ledger.reconcile import reconcile_command
    from app.ledger.status import eod_status_command
    from app.loans import routes as loans_routes  # noqa: F401
    from app.members import profile_routes  # noqa: F401
    from app.members import routes as members_routes  # noqa: F401
    from app.models import (  # noqa: F401
        access,
        audit,
        consent,
        daraja_settings,
        deposit_controls,
        identity,
        ledger,
        loan,
        membership,
        payment_details,
        payment_request,
        refresh_token,
        reversal_request,
        rules,
        sacco,
        savings,
        stk_push,
        support,
        user,
    )
    from app.payments import routes as payments_routes  # noqa: F401
    from app.saccos import access_routes as saccos_access_routes  # noqa: F401
    from app.saccos import payment_details_routes  # noqa: F401
    from app.saccos import routes as saccos_routes  # noqa: F401
    from app.saccos import rules_routes as saccos_rules_routes  # noqa: F401
    from app.savings import accounts as savings_accounts  # noqa: F401
    from app.savings import controls_routes  # noqa: F401
    from app.savings import history as savings_history  # noqa: F401
    from app.savings import requests_routes as savings_requests_routes  # noqa: F401
    from app.savings import routes as savings_routes  # noqa: F401
    from app.support import routes as support_routes  # noqa: F401

    init_limiter(app)
    init_cors(app)
    _install_security_headers(app)

    for blueprint in BLUEPRINTS:
        prefix = f"{API_PREFIX}{blueprint.url_prefix}"
        app.register_blueprint(blueprint, url_prefix=prefix)

    _register_health(app)
    app.cli.add_command(reconcile_command)
    app.cli.add_command(eod_status_command)

    return app


def _register_health(app):
    @app.get("/health")
    def host_health():
        """For the host's health check: up or not, nothing about the database."""
        try:
            db.session.execute(text("SELECT 1"))
        except Exception:
            db.session.rollback()
            return jsonify(status="unavailable"), 503
        return jsonify(status="ok")

    @app.get("/api/health")
    @read_limit  # each call opens a database connection
    def health():
        database_url = os.getenv("DATABASE_URL")

        if not database_url:
            return jsonify({"status": "error", "database": "not_configured"}), 503

        try:
            # libpq does not understand SQLAlchemy's driver suffix, and .env.example
            # tells everyone to use postgresql+psycopg://. Strip it before connecting.
            dsn = make_url(database_url).set(drivername="postgresql")
            dsn_text = dsn.render_as_string(hide_password=False)
            with psycopg.connect(dsn_text) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()

            return jsonify({"status": "healthy", "database": "connected"}), 200

        except (psycopg.Error, ArgumentError):
            return jsonify({"status": "error", "database": "disconnected"}), 503


def _check_required_settings(app):
    required = ("SECRET_KEY", "SQLALCHEMY_DATABASE_URI")
    missing = [name for name in required if not app.config.get(name)]
    if missing:
        raise RuntimeError(
            f"Missing settings: {', '.join(missing)}. "
            "Set them in your .env file or shell."
        )

    if len(str(app.config.get("SECRET_KEY"))) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters in every "
            'environment. Generate one: python -c "import secrets; '
            'print(secrets.token_urlsafe(48))"'
        )

    app_env = str(app.config.get("APP_ENV") or "development").strip().lower()
    if app_env not in APP_ENVS:
        raise RuntimeError(
            f'APP_ENV must be "development" or "production", not {app_env!r}.'
        )
    daraja_env = str(app.config.get("DARAJA_ENV") or "sandbox").strip().lower()
    if daraja_env not in DARAJA_ENVS:
        raise RuntimeError(
            f'DARAJA_ENV must be "sandbox" or "production", not {daraja_env!r}.'
        )
    app.config["DARAJA_ENV"] = daraja_env
    if app_env == PRODUCTION:
        _check_production_settings(app)
    if app.config.get("DARAJA_ENABLED"):
        _check_daraja_settings(app, app_env)


DARAJA_ENVS = ("sandbox", "production")
# The shared keys are sandbox keys. Live Daraja never reads them: every SACCO
# brings its own, so only the callback is needed here.
DARAJA_SETTINGS = (
    "DARAJA_CONSUMER_KEY",
    "DARAJA_CONSUMER_SECRET",
    "DARAJA_PASSKEY",
    "DARAJA_SHORTCODE",
    "DARAJA_CALLBACK_URL",
    "DARAJA_CALLBACK_TOKEN",
)
LIVE_DARAJA_SETTINGS = ("DARAJA_CALLBACK_URL", "DARAJA_CALLBACK_TOKEN")


def _check_daraja_settings(app, app_env):
    """Never half configured. In a production app only the live API is allowed,
    and live Daraja needs https, the token in the callback URL and the key that
    encrypts each SACCO's credentials."""
    live = app.config.get("DARAJA_ENV") == "production"
    if app_env == PRODUCTION and not live:
        raise RuntimeError(
            "DARAJA_ENABLED with APP_ENV=production needs DARAJA_ENV=production: "
            "sandbox Daraja never runs in a production app."
        )
    needed = LIVE_DARAJA_SETTINGS if live else DARAJA_SETTINGS
    missing = [k for k in needed if not app.config.get(k)]
    if missing:
        raise RuntimeError(f"DARAJA_ENABLED needs: {', '.join(missing)}.")
    # The path token is the only thing that proves a callback came from Safaricom.
    token = app.config["DARAJA_CALLBACK_TOKEN"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token) or len(set(token)) < 10:
        raise RuntimeError(
            "DARAJA_CALLBACK_TOKEN must be 32 or more random letters, digits, - or _."
        )
    key = app.config.get("DARAJA_ENCRYPTION_KEY")
    if live and not key:
        raise RuntimeError(
            "DARAJA_ENCRYPTION_KEY is required when DARAJA_ENV=production: live "
            "prompts use each SACCO's saved credentials (see .env.example)."
        )
    if key:
        try:
            Fernet(key.encode())
        except ValueError as exc:
            raise RuntimeError(
                "DARAJA_ENCRYPTION_KEY must be a Fernet key (see .env.example)."
            ) from exc
    url = app.config["DARAJA_CALLBACK_URL"]
    if not (url.startswith("https://") and url.endswith("/daraja/callback/" + token)):
        raise RuntimeError(
            "DARAJA_CALLBACK_URL must be https and end in /daraja/callback/<token>."
        )


def _check_production_settings(app):
    """Refuse to start in production with a setting that is only safe locally."""
    debug = str(os.environ.get("FLASK_DEBUG", "")).strip().lower()
    if app.config.get("DEBUG") or debug in DEBUG_VALUES:
        raise RuntimeError(
            "FLASK_DEBUG must be off when APP_ENV=production; debug mode "
            "exposes the interactive debugger."
        )

    if str(app.config.get("RATELIMIT_STORAGE_URI") or "").startswith("memory://"):
        raise RuntimeError(
            "RATELIMIT_STORAGE_URI must not use memory:// when APP_ENV=production; "
            "per-process counters multiply every client's allowance per worker."
        )

    if str(app.config.get("SECRET_KEY") or "") == EXAMPLE_SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY must not be the .env.example placeholder when "
            "APP_ENV=production."
        )

    if "*" in str(app.config.get("CORS_ORIGINS") or ""):
        raise RuntimeError(
            "CORS_ORIGINS must list exact origins; '*' is not allowed when "
            "APP_ENV=production."
        )

    if app.config.get("MAIL_BACKEND") != "smtp" or not app.config.get("MAIL_SERVER"):
        raise RuntimeError(
            "MAIL_BACKEND must be smtp, with MAIL_SERVER set, when "
            "APP_ENV=production; the log backend would print verification links."
        )


def _install_security_headers(app):
    """Every /api/ response is uncacheable and un-frameable; HSTS in production."""

    @app.after_request
    def set_security_headers(response):
        if not request.path.startswith("/api/"):
            return response
        for name, value in SECURITY_HEADERS:
            response.headers[name] = value
        if app.config.get("APP_ENV") == PRODUCTION:
            response.headers["Strict-Transport-Security"] = HSTS
        return response
