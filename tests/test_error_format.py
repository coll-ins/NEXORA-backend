"""One error body on every route: {"error": {"code": ..., "message": ...}}."""


from app.errors import CODES, error_response
from app.models.sacco import SaccoStatus
from test_auth_routes import (  # noqa: F401  (clean_tables is an autouse fixture)
    EMAIL,
    PASSWORD,
    clean_tables,
    join_sacco,
    limited_client,
    logged_in,
    login,
    register,
)


def assert_error(resp, status, code, message=None):
    assert resp.status_code == status
    body = resp.get_json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    if message is not None:
        assert body["error"]["message"] == message


def test_the_table_has_one_code_per_status():
    assert CODES == {
        400: "validation_error",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        423: "locked",
        429: "rate_limited",
        500: "internal_error",
    }


def test_error_response_uses_the_table_unless_given_a_code(app):
    with app.app_context():
        resp, status = error_response(404, "Gone.")
        assert (status, resp.get_json()) == (
            404,
            {"error": {"code": "not_found", "message": "Gone."}},
        )
        assert error_response(404, "Gone.", "custom")[0].get_json()["error"][
            "code"
        ] == "custom"


def test_400_validation_error(client):
    resp = client.post("/api/v1/auth/register", json={"email": "nope"})
    assert_error(resp, 400, "validation_error")


def test_401_unauthorized(client):
    assert_error(client.get("/api/v1/members/me"), 401, "unauthorized")


def test_403_forbidden(client):
    tokens = logged_in(client)
    resp = client.get(
        "/api/v1/admin/audit",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert_error(resp, 403, "forbidden")


def test_404_not_found(client):
    assert_error(client.get("/api/v1/nope"), 404, "not_found", "Not found.")


def test_405_method_not_allowed(client):
    resp = client.delete("/api/v1/auth/login")
    assert_error(resp, 405, "method_not_allowed")


def test_409_conflict(client):
    register(client)
    assert_error(register(client), 409, "conflict")


def test_413_payload_too_large(client):
    resp = client.post(
        "/api/v1/auth/login",
        data=b"x" * 20_000,
        content_type="application/json",
    )
    assert_error(resp, 413, "payload_too_large")


def test_423_locked_for_a_suspended_sacco(client):
    register(client)
    sacco = join_sacco(SaccoStatus.SUSPENDED)
    token = login(client).get_json()["access_token"]
    resp = client.get(
        f"/api/v1/saccos/{sacco.id}/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert_error(resp, 423, "locked")


def test_429_rate_limited(limited_client):  # noqa: F811
    for _ in range(5):
        limited_client.post("/api/v1/auth/login", json={})
    resp = limited_client.post("/api/v1/auth/login", json={})
    assert_error(resp, 429, "rate_limited", "Too many requests. Try again later.")


def test_500_internal_error_hides_details(app, client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr("app.auth.routes.hash_password", boom)
    monkeypatch.setitem(app.config, "PROPAGATE_EXCEPTIONS", False)
    resp = client.post(
        "/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD}
    )
    assert_error(resp, 500, "internal_error", "Internal error.")
    assert "secret" not in resp.get_data(as_text=True)


# Deliberately answers 200 to anything, so it cannot be used to find registered emails.
ALWAYS_OK = {"/api/v1/auth/resend-verification", "/api/v1/auth/forgot-password"}


def test_every_route_answers_errors_in_the_one_shape(app, client):
    """Walks the URL map, so a route added later without the helper is caught."""
    checked = 0
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static" or rule.rule in (
            {"/api/health", "/health"} | ALWAYS_OK
        ):
            continue
        method = next(m for m in sorted(rule.methods) if m not in ("HEAD", "OPTIONS"))
        path = rule.rule
        for arg in rule.arguments:
            path = path.replace(f"<int:{arg}>", "1").replace(f"<{arg}>", "1")
        resp = client.open(path, method=method)  # no token, no body
        assert resp.status_code >= 400, (method, path)
        error = resp.get_json()["error"]
        assert set(error) == {"code", "message"}, (method, path, error)
        assert error["code"] == CODES[resp.status_code], (method, path)
        checked += 1
    assert checked >= 30
