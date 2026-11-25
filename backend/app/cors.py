"""CORS for the browser frontend.

Origins are matched exactly against `CORS_ORIGINS`; an empty list means no
cross-origin access, and the wildcard is never sent. Credentials stay off
because tokens travel in the Authorization header, not cookies.
"""

from flask import request

ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
ALLOWED_HEADERS = ("Authorization", "Content-Type")
# The browser may read these on every answer: a 429 tells the page to wait.
EXPOSE_HEADERS = ("Retry-After",)
PREFLIGHT_MAX_AGE = "600"


def allowed_origins(config) -> tuple[str, ...]:
    """The exact origins listed in CORS_ORIGINS, in order and without duplicates."""
    raw = config.get("CORS_ORIGINS") or ""
    if not isinstance(raw, str):
        raw = ",".join(str(part) for part in raw)
    origins: list[str] = []
    for part in raw.split(","):
        origin = part.strip().rstrip("/")  # browsers never send a trailing slash
        if origin and origin not in origins:
            origins.append(origin)
    return tuple(origins)


def _apply_origin(response, origin: str):
    """Name the one caller's origin, and say the answer depends on Origin."""
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Expose-Headers"] = ", ".join(EXPOSE_HEADERS)
    response.vary.add("Origin")
    return response


def init_cors(app):
    """Attach the two CORS hooks: preflight answers and per-response headers."""

    @app.before_request
    def answer_preflight():
        if request.method != "OPTIONS":
            return None
        origin = request.headers.get("Origin")
        if origin not in allowed_origins(app.config):
            return None  # not an allowed origin: the route answers as usual
        if not request.headers.get("Access-Control-Request-Method"):
            return None  # a plain OPTIONS, not a preflight
        response = app.make_response(("", 204))
        _apply_origin(response, origin)
        response.headers["Access-Control-Allow-Methods"] = ", ".join(ALLOWED_METHODS)
        response.headers["Access-Control-Allow-Headers"] = ", ".join(ALLOWED_HEADERS)
        response.headers["Access-Control-Max-Age"] = PREFLIGHT_MAX_AGE
        return response

    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get("Origin")
        if origin and origin in allowed_origins(app.config):
            _apply_origin(response, origin)
        return response
