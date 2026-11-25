"""Every resource route is served under /api/v1 (see docs/api-conventions.md)."""

from app import API_PREFIX


def test_prefix_is_api_v1():
    assert API_PREFIX == "/api/v1"


def test_every_route_lives_under_the_prefix(app):
    rules = [
        r.rule
        for r in app.url_map.iter_rules()
        if r.endpoint not in ("static", "health", "host_health")
    ]
    assert rules, "no routes registered"
    assert all(rule.startswith(f"{API_PREFIX}/") for rule in rules), rules


def test_health_stays_unversioned(client):
    assert client.get("/api/health").status_code in (200, 503)
    assert client.get(f"{API_PREFIX}/health").status_code == 404


def test_unprefixed_auth_path_is_not_served(client):
    assert client.post("/auth/login", json={}).status_code == 404


def test_prefixed_auth_path_is_served(client):
    assert client.post(f"{API_PREFIX}/auth/login", json={}).status_code != 404
