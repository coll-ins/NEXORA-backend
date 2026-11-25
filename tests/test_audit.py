# ruff: noqa: F811  (pytest fixtures imported from savings_support)
"""Audit read endpoints (Move 4). Read-only, and every query is SACCO-scoped."""

from datetime import UTC, datetime

import pytest

from app.extensions import db
from app.models.audit import AuditLog
from app.models.membership import MembershipRole
from savings_support import Setup, release_session, schema_app  # noqa: F401
from support_grants import approve_support
from test_accounts import make_owner
from test_history_admin import add_member, auth, suspend

pytestmark = pytest.mark.schema

ADMIN = "/api/v1/admin"
PLATFORM = f"{ADMIN}/audit"
FIELDS = {"id", "actor_id", "action", "target", "created_at"}


def reader(s):
    """The SACCO's audit reader: a secretary (legacy staff role), one per Setup."""
    if not hasattr(s, "audit_reader"):
        s.audit_reader = add_member(s, role=MembershipRole.STAFF, with_account=False)
    return s.audit_reader


def sacco_url(sacco_id):
    return f"{ADMIN}/saccos/{sacco_id}/audit"


def put(sacco_id, actor_id, action, target=None, when=None):
    """Insert one audit row directly and return its id."""
    row = AuditLog(sacco_id=sacco_id, actor_id=actor_id, action=action, target=target)
    if when is not None:
        row.created_at = when
    db.session.add(row)
    db.session.commit()
    row_id = row.id
    db.session.remove()
    return row_id


def test_an_admin_sees_only_their_sacco_newest_first(schema_app):
    s = Setup()
    other = Setup()
    first = put(s.sacco.id, s.admin.id, "audit_test.one", "t1")
    second = put(s.sacco.id, s.admin.id, "audit_test.two", "t2")
    foreign = put(other.sacco.id, other.admin.id, "audit_test.one", "t3")
    client = schema_app.test_client()

    body = client.get(
        sacco_url(s.sacco.id) + "?action=audit_test.", headers=auth(reader(s))
    ).get_json()
    assert body["total"] == 2
    assert [e["id"] for e in body["entries"]] == [second, first]
    assert all(set(e) == FIELDS for e in body["entries"])
    assert body["entries"][0]["actor_id"] == s.admin.id
    assert body["entries"][0]["target"] == "t2"

    everything = client.get(
        sacco_url(s.sacco.id) + "?per_page=100", headers=auth(reader(s))
    ).get_json()
    assert foreign not in [e["id"] for e in everything["entries"]]


def test_audit_access_rules(schema_app):
    s = Setup()
    outsider = Setup()
    staff = add_member(s, role=MembershipRole.STAFF, with_account=False)
    owner = make_owner()
    client = schema_app.test_client()
    url = sacco_url(s.sacco.id)

    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth(s.member)).status_code == 403
    # The secretary is the SACCO's reader; the admin never reads the audit log.
    assert client.get(url, headers=auth(staff)).status_code == 200
    assert client.get(url, headers=auth(outsider.admin)).status_code == 404
    assert client.get(url, headers=auth(s.admin)).status_code == 403
    assert client.get(url, headers=auth(owner)).status_code == 403  # no consent
    approve_support(owner.id, s.sacco.id, "audit", s.admin_membership.id)
    assert client.get(url, headers=auth(owner)).status_code == 200
    assert client.get(sacco_url(2_000_000_000), headers=auth(owner)).status_code == 404


def test_audit_of_a_suspended_sacco_is_423_for_the_secretary(schema_app):
    s = Setup()
    owner = make_owner()
    suspend(s.sacco.id)
    client = schema_app.test_client()
    url = sacco_url(s.sacco.id)
    assert client.get(url, headers=auth(reader(s))).status_code == 423
    # A platform owner reads a suspended SACCO only with its consent.
    approve_support(owner.id, s.sacco.id, "audit", s.admin_membership.id)
    assert client.get(url, headers=auth(owner)).status_code == 200


def test_action_filter_is_exact_or_a_prefix_and_escapes_wildcards(schema_app):
    s = Setup()
    put(s.sacco.id, s.admin.id, "audit_test.one")
    put(s.sacco.id, s.admin.id, "audit_test.two")
    put(s.sacco.id, s.admin.id, "auditXtest.three")  # "_" must not act as a wildcard
    client = schema_app.test_client()
    url = sacco_url(s.sacco.id)

    def total(query):
        resp = client.get(url + query, headers=auth(reader(s)))
        assert resp.status_code == 200
        return resp.get_json()["total"]

    assert total("?action=audit_test.one") == 1
    assert total("?action=audit_test.") == 2
    assert total("?action=auditXtest.") == 1
    assert total("?action=%25.") == 0  # a literal "%" prefix matches nothing
    assert total("?action=audit_test") == 0  # no trailing dot means exact match


def test_date_filters(schema_app):
    s = Setup()
    old = put(
        s.sacco.id,
        s.admin.id,
        "audit_test.dates",
        when=datetime(2020, 1, 15, 12, 0, tzinfo=UTC),
    )
    recent = put(s.sacco.id, s.admin.id, "audit_test.dates")
    client = schema_app.test_client()
    base = sacco_url(s.sacco.id) + "?action=audit_test.dates"

    def ids(query):
        resp = client.get(base + query, headers=auth(reader(s)))
        assert resp.status_code == 200
        return [e["id"] for e in resp.get_json()["entries"]]

    assert ids("") == [recent, old]
    assert ids("&from=2020-01-15&to=2020-01-15") == [old]
    assert ids("&from=2020-01-16") == [recent]
    assert ids("&to=2020-01-14") == []
    assert ids("&to=9999-12-31") == [recent, old]


def test_audit_paging(schema_app):
    s = Setup()
    made = [put(s.sacco.id, s.admin.id, "audit_test.page") for _ in range(5)]
    client = schema_app.test_client()
    url = sacco_url(s.sacco.id) + "?action=audit_test.page&per_page=2"

    pages = [
        client.get(url + f"&page={n}", headers=auth(reader(s))).get_json()
        for n in (1, 2, 3)
    ]
    assert [len(p["entries"]) for p in pages] == [2, 2, 1]
    assert all(p["total"] == 5 and p["per_page"] == 2 for p in pages)
    seen = [e["id"] for p in pages for e in p["entries"]]
    assert seen == sorted(made, reverse=True)

    huge = client.get(
        sacco_url(s.sacco.id) + "?page=99999999999999999999", headers=auth(reader(s))
    )
    assert huge.status_code == 200 and huge.get_json()["entries"] == []


def test_audit_bad_input_is_400(schema_app):
    s = Setup()
    client = schema_app.test_client()
    url = sacco_url(s.sacco.id)
    bad = (
        "?from=nope",
        "?to=2026-13-45",
        "?from=2026-02-01&to=2026-01-01",
        "?page=0",
        "?per_page=abc",
        "?per_page=-1",
        "?action=" + "a" * 101,
        "?action=%00",
        "?action=a%00b",
        "?from=20260101",  # one date form only
        "?to=2026-W01-1",
        "?from=２０２６-01-01",
    )
    for query in bad:
        resp = client.get(url + query, headers=auth(reader(s)))
        assert resp.status_code == 400, query
    capped = client.get(url + "?per_page=5000", headers=auth(reader(s)))
    assert capped.get_json()["per_page"] == 100


def test_the_platform_route_is_for_the_owner_only(schema_app):
    s = Setup()
    other = Setup()
    owner = make_owner()
    mine = put(s.sacco.id, s.admin.id, "audit_test.plat")
    theirs = put(other.sacco.id, other.admin.id, "audit_test.plat")
    global_row = put(None, owner.id, "audit_test.plat")
    client = schema_app.test_client()
    url = PLATFORM + "?action=audit_test.plat&per_page=100"

    assert client.get(PLATFORM).status_code == 401
    assert client.get(PLATFORM, headers=auth(reader(s))).status_code == 403
    assert client.get(PLATFORM, headers=auth(s.member)).status_code == 403

    body = client.get(url, headers=auth(owner)).get_json()
    assert [e["id"] for e in body["entries"]] == [global_row, theirs, mine]
    assert all(set(e) == FIELDS for e in body["entries"])

    scoped = client.get(url + f"&sacco_id={s.sacco.id}", headers=auth(owner))
    assert [e["id"] for e in scoped.get_json()["entries"]] == [mine]

    # The SACCO route never shows platform-wide rows (sacco_id is NULL).
    own = client.get(
        sacco_url(s.sacco.id) + "?action=audit_test.plat", headers=auth(reader(s))
    ).get_json()
    assert [e["id"] for e in own["entries"]] == [mine]


def test_the_platform_route_rejects_bad_input(schema_app):
    owner = make_owner()
    client = schema_app.test_client()
    bad = (
        "?sacco_id=abc",
        "?sacco_id=0",
        "?sacco_id=-3",
        "?sacco_id=99999999999999999999",
        "?from=nope",
        "?page=0",
        "?per_page=abc",
    )
    for query in bad:
        resp = client.get(PLATFORM + query, headers=auth(owner))
        assert resp.status_code == 400, query


def test_audit_routes_are_read_only(schema_app):
    s = Setup()
    owner = make_owner()
    client = schema_app.test_client()
    for url, user in ((sacco_url(s.sacco.id), reader(s)), (PLATFORM, owner)):
        for method in ("post", "put", "patch", "delete"):
            resp = getattr(client, method)(url, headers=auth(user))
            assert resp.status_code == 405, (method, url)
