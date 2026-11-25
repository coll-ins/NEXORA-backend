import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import jwt
import pytest
import redis
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.auth.limits import limiter
from app.auth.suspension import SUSPENDED_MESSAGE
from app.auth.tokens import decode_access_token
from app.extensions import db
from app.models.membership import MembershipRole, MembershipStatus, SaccoMembership
from app.models.refresh_token import RefreshToken
from app.models.sacco import Sacco, SaccoStatus
from app.models.user import User
from config import TestConfig

EMAIL = "jane@example.com"
PASSWORD = "correct-horse-battery"
WRONG = "wrong-password-123"


def _truncate():
    db.session.remove()
    db.session.execute(
        text("TRUNCATE refresh_tokens, users, saccos RESTART IDENTITY CASCADE")
    )
    db.session.commit()


@pytest.fixture(autouse=True)
def clean_tables(app):
    _truncate()
    yield
    _truncate()


def post(client, path, **body):
    return client.post(f"/api/v1/auth/{path}", json=body)


def register(client, email=EMAIL, password=PASSWORD):
    return post(client, "register", email=email, password=password)


def login(client, email=EMAIL, password=PASSWORD):
    return post(client, "login", email=email, password=password)


def logged_in(client):
    register(client)
    return login(client).get_json()


def refresh(client, token):
    return post(client, "refresh", refresh_token=token)


def parallel(app, n, fn):
    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(lambda _: fn(app.test_client()), range(n)))


def forge(app, **claims):
    now = datetime.now(UTC)
    base = {
        "sub": "1",
        "type": "refresh",
        "jti": "a" * 32,
        "iat": now,
        "iss": "nexora-api",
        "aud": "nexora",
    }
    base |= {"exp": now + timedelta(days=1)} | claims
    return jwt.encode(base, app.config["SECRET_KEY"], algorithm="HS256")


# ---------- register ----------


def test_register_creates_user_and_hashes_password(client):
    resp = register(client, email="Jane@Example.com")
    assert resp.status_code == 201
    assert resp.get_json() == {"id": 1, "email": EMAIL}
    assert db.session.query(User).one().password_hash != PASSWORD


def test_register_rejects_duplicates_in_any_case(client):
    assert register(client).status_code == 201
    assert register(client, email="JANE@example.COM").status_code == 409


def test_database_refuses_emails_that_differ_only_by_case(client):
    db.session.add(User(email="A@x.com", password_hash="h"))
    db.session.commit()
    db.session.add(User(email="a@x.com", password_hash="h"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": "not-an-email", "password": PASSWORD},
        {"email": EMAIL},
        {"email": EMAIL, "password": "short"},
        {"email": EMAIL, "password": 12345678},
        {"email": EMAIL, "password": "x" * 129},
        {"email": "pass@example.com", "password": "pass@example.com"},
        {"email": ["a@b.com"], "password": PASSWORD},
        {"email": "a\x00b@example.com", "password": PASSWORD},
        {"email": "a\ud800b@example.com", "password": PASSWORD},
        {"email": EMAIL, "password": "\ud800abcdefgh"},
        {"email": EMAIL, "password": "abc\x00defgh"},
    ],
)
def test_register_rejects_bad_input_with_400(client, payload):
    assert client.post("/api/v1/auth/register", json=payload).status_code == 400


def test_non_json_body_is_400(client):
    resp = client.post("/api/v1/auth/register", data="nope", content_type="text/plain")
    assert resp.status_code == 400


def test_oversized_body_is_413(client):
    assert login(client, password="x" * 20_000).status_code == 413


# ---------- login ----------


def test_login_returns_15_minute_access_and_stored_refresh_token(client):
    register(client)
    body = login(client).get_json()
    claims = decode_access_token(body["access_token"])
    assert claims["sub"] == "1"
    assert claims["exp"] - claims["iat"] == 15 * 60
    assert db.session.query(RefreshToken).count() == 1


def test_wrong_and_unknown_logins_are_identical_401s(client):
    register(client)
    wrong, unknown = login(client, password=WRONG), login(client, email="no@x.com")
    assert wrong.status_code == unknown.status_code == 401
    assert (
        wrong.get_json()
        == unknown.get_json()
        == {"error": {"code": "unauthorized", "message": "Invalid email or password."}}
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": EMAIL},
        {"email": EMAIL, "password": 5},
        {"email": 5, "password": "x"},
        {"email": "a\ud800@x.com", "password": PASSWORD},
        {"email": EMAIL, "password": "\ud800abcdefgh"},
        {"email": "a\x00@x.com", "password": PASSWORD},
    ],
)
def test_login_malformed_body_is_401_not_500(client, payload):
    register(client)
    assert client.post("/api/v1/auth/login", json=payload).status_code == 401


def test_five_failures_lock_and_then_refuse_the_right_password(client):
    register(client)
    assert [login(client, password=WRONG).status_code for _ in range(5)] == [401] * 5
    assert login(client).status_code == 423
    assert db.session.query(User).one().failed_login_count == 5


def test_parallel_guesses_are_turned_away_not_counted_past_the_limit(app, client):
    register(client)
    codes = [
        r.status_code for r in parallel(app, 12, lambda c: login(c, password=WRONG))
    ]
    # How many requests overlap depends on the machine: overlapping ones get 429,
    # and once five failures are counted the rest get 423. Either way, no more
    # than five failures may ever be counted.
    assert set(codes) <= {401, 423, 429} and codes.count(401) <= 5
    for _ in range(10):  # bounded: never spin forever if the lockout fails to engage
        if login(client, password=WRONG).status_code != 401:
            break
    assert login(client).status_code == 423
    db.session.expire_all()
    assert db.session.query(User).one().failed_login_count == 5


def test_login_with_unicode_email_whose_case_mapping_differs_between_engines(client):
    email = "\ua7ce@example.com"
    assert register(client, email=email).status_code == 201
    assert login(client, email=email).status_code == 200


def test_auth_responses_are_not_cacheable(client):
    assert login(client).headers["Cache-Control"] == "no-store"


def test_lock_expiry_allows_login_and_clears_counters(client):
    register(client)
    for _ in range(5):
        login(client, password=WRONG)
    user = db.session.query(User).one()
    user.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    db.session.commit()
    assert login(client).status_code == 200
    db.session.expire_all()
    user = db.session.query(User).one()
    assert (user.failed_login_count, user.locked_until) == (0, None)


# ---------- refresh ----------


def test_refresh_rotates_and_the_old_token_dies_immediately(client):
    first = logged_in(client)
    second = refresh(client, first["refresh_token"])
    assert second.status_code == 200
    decode_access_token(second.get_json()["access_token"])
    assert refresh(client, first["refresh_token"]).status_code == 401


def test_quick_replay_is_refused_but_leaves_other_sessions_alone(client):
    register(client)
    a, b = login(client).get_json(), login(client).get_json()
    assert refresh(client, a["refresh_token"]).status_code == 200
    assert refresh(client, a["refresh_token"]).status_code == 401
    assert refresh(client, b["refresh_token"]).status_code == 200


def test_replaying_an_old_spent_token_revokes_the_whole_chain(client):
    first = logged_in(client)
    second = refresh(client, first["refresh_token"]).get_json()
    db.session.query(RefreshToken).filter(RefreshToken.revoked_at.isnot(None)).update(
        {"revoked_at": datetime.now(UTC) - timedelta(minutes=1)}
    )
    db.session.commit()
    assert refresh(client, first["refresh_token"]).status_code == 401
    assert refresh(client, second["refresh_token"]).status_code == 401


def test_parallel_refresh_with_one_token_keeps_the_winner_alive(app, client):
    token = logged_in(client)["refresh_token"]
    results = parallel(app, 2, lambda c: refresh(c, token))
    assert sorted(r.status_code for r in results) == [200, 401]
    winner = next(r for r in results if r.status_code == 200).get_json()
    assert refresh(client, winner["refresh_token"]).status_code == 200


def test_refresh_requires_a_refresh_token(client):
    tokens = logged_in(client)
    assert refresh(client, tokens["access_token"]).status_code == 401
    assert refresh(client, "garbage").status_code == 401
    assert refresh(client, "").status_code == 400
    assert post(client, "refresh").status_code == 400


def test_refresh_rejects_forged_expired_and_unknown_tokens(app, client):
    logged_in(client)
    wrong_key = jwt.encode(
        {"sub": "1", "type": "refresh", "jti": "x", "iat": 1, "exp": 4_000_000_000},
        "another-secret-another-secret-0123",
        algorithm="HS256",
    )
    bad = [
        forge(app, exp=datetime.now(UTC) - timedelta(seconds=5)),
        wrong_key,
        forge(app),  # valid signature, jti not in the database
        forge(app, sub="abc"),
        forge(app, sub="99999999999999999999"),
        forge(app, sub="-1"),
        forge(app, sub="+1"),
        forge(app, sub=" 1"),
        forge(app, sub="\u0661"),
        forge(app, jti="a\x00" * 16),
        forge(app, jti="not-hex"),
        forge(app, type="access"),
    ]
    assert {refresh(client, t).status_code for t in bad} == {401}
    assert {
        refresh(client, jwt.encode({}, key=None, algorithm="none")).status_code
    } == {401}


def test_unknown_jti_does_not_revoke_the_users_real_tokens(app, client):
    tokens = logged_in(client)
    refresh(client, forge(app))
    assert refresh(client, tokens["refresh_token"]).status_code == 200


def test_expired_database_row_is_rejected(client):
    tokens = logged_in(client)
    db.session.query(RefreshToken).update(
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    db.session.commit()
    assert refresh(client, tokens["refresh_token"]).status_code == 401


def test_deleted_users_tokens_stop_working(client):
    tokens = logged_in(client)
    db.session.query(User).delete()
    db.session.commit()
    assert refresh(client, tokens["refresh_token"]).status_code == 401


# ---------- logout ----------


def test_logout_invalidates_only_the_sent_refresh_token(client):
    register(client)
    a, b = login(client).get_json(), login(client).get_json()
    assert post(client, "logout", refresh_token=a["refresh_token"]).status_code == 200
    assert refresh(client, a["refresh_token"]).status_code == 401
    assert refresh(client, b["refresh_token"]).status_code == 200


def test_logout_is_idempotent_and_rejects_bad_tokens(client):
    tokens = logged_in(client)
    for _ in range(2):
        assert (
            post(client, "logout", refresh_token=tokens["refresh_token"]).status_code
            == 200
        )
    assert post(client, "logout", refresh_token="garbage").status_code == 401
    assert (
        post(client, "logout", refresh_token=tokens["access_token"]).status_code == 401
    )
    assert post(client, "logout").status_code == 400


# ---------- suspended SACCO ----------


def join_sacco(status=SaccoStatus.ACTIVE, role=MembershipRole.MEMBER):
    sacco = Sacco(name=f"Test Sacco {uuid.uuid4().hex[:8]}", status=status)
    db.session.add(sacco)
    db.session.flush()
    db.session.add(SaccoMembership(user_id=1, sacco_id=sacco.id, role=role))
    db.session.commit()
    return sacco


def test_member_of_active_sacco_can_log_in(client):
    register(client)
    join_sacco()
    assert login(client).status_code == 200


@pytest.mark.parametrize("role", list(MembershipRole))
def test_a_suspended_sacco_does_not_block_login_its_endpoints_answer_423(
    client, role
):
    # Login is not SACCO-scoped: the user still sees the suspension in
    # GET /members/me/saccos. The suspended SACCO itself answers 423.
    register(client)
    sacco = join_sacco(SaccoStatus.SUSPENDED, role)
    resp = login(client)
    assert resp.status_code == 200
    token = resp.get_json()["access_token"]
    scoped = client.get(
        f"/api/v1/saccos/{sacco.id}/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert scoped.status_code == 423
    assert scoped.get_json()["error"]["message"] == SUSPENDED_MESSAGE


def test_a_user_with_an_active_sacco_still_logs_in_when_another_is_suspended(client):
    # Otherwise any admin could lock a user out everywhere by adding them to a
    # SACCO that later gets suspended. The suspended SACCO itself still answers
    # 423 through the tenant guard.
    register(client)
    join_sacco(SaccoStatus.ACTIVE)
    join_sacco(SaccoStatus.SUSPENDED)
    assert login(client).status_code == 200


def test_a_user_whose_only_usable_sacco_is_suspended_still_logs_in(client):
    register(client)
    active = join_sacco(SaccoStatus.ACTIVE)
    db.session.query(SaccoMembership).filter_by(sacco_id=active.id).update(
        {"status": MembershipStatus.INACTIVE}
    )
    db.session.commit()
    join_sacco(SaccoStatus.SUSPENDED)
    assert login(client).status_code == 200


def test_suspension_is_not_revealed_to_a_wrong_password(client):
    register(client)
    join_sacco(SaccoStatus.SUSPENDED)
    body = login(client, password=WRONG).get_json()
    assert body["error"]["message"] == "Invalid email or password."


def test_suspending_a_sacco_cuts_off_open_sessions_from_that_sacco(client):
    # The session itself lives on (other SACCOs, my SACCOs); the suspended
    # SACCO answers 423 at once, even to a token issued before the suspension.
    tokens = logged_in(client)
    sacco = join_sacco(SaccoStatus.SUSPENDED)
    assert refresh(client, tokens["refresh_token"]).status_code == 200
    scoped = client.get(
        f"/api/v1/saccos/{sacco.id}/settings",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert scoped.status_code == 423


# ---------- rate limiting ----------


@pytest.fixture
def limited_client(app):
    """A client for an app with Neo's real limits on (the shared app has them off)."""

    class Limited(TestConfig):
        RATELIMIT_ENABLED = True

    limited = create_app(Limited)
    limiter.reset()
    yield limited.test_client()
    limiter.reset()
    limiter.enabled = False


@pytest.mark.parametrize("path", ["login", "register"])
def test_sixth_request_in_a_minute_is_429(limited_client, path):
    codes = [
        post(limited_client, path, email=EMAIL, password=WRONG).status_code
        for _ in range(6)
    ]
    assert codes[-1] == 429 and 429 not in codes[:-1]
    limited = post(limited_client, path, email=EMAIL, password=WRONG)
    assert limited.get_json()["error"]["message"]


def test_rate_limit_does_not_touch_refresh(limited_client):
    register(limited_client)
    tokens = login(limited_client).get_json()
    for _ in range(8):
        tokens = refresh(limited_client, tokens["refresh_token"]).get_json()
    assert "access_token" in tokens


def test_rate_limit_uses_the_client_address_forwarded_by_a_trusted_proxy(app):
    class Behind(TestConfig):
        RATELIMIT_ENABLED = True
        TRUSTED_PROXIES = 1

    client = create_app(Behind).test_client()
    limiter.reset()
    try:

        def attempt(ip):
            body = {"email": EMAIL, "password": WRONG}
            return client.post(
                "/api/v1/auth/login", json=body, headers={"X-Forwarded-For": ip}
            ).status_code

        codes = [attempt("1.1.1.1") for _ in range(6)]
        assert codes[-1] == 429 and 429 not in codes[:-1]
        assert attempt("2.2.2.2") != 429  # a different client has its own allowance
    finally:
        limiter.reset()
        limiter.enabled = False


# ---------- rate-limit storage ----------


def _clear_limiter_keys(url):
    store = redis.Redis.from_url(url)
    for key in store.scan_iter("LIMITS:*"):
        store.delete(key)


@pytest.fixture
def redis_server():
    """Redis for the storage tests.

    Uses the server at TEST_REDIS_URL when set (point it at a throwaway Redis),
    otherwise an in-process server that speaks the Redis protocol.
    """
    if url := os.environ.get("TEST_REDIS_URL"):
        try:
            redis.Redis.from_url(url).ping()
        except redis.exceptions.ConnectionError:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"TEST_REDIS_URL={url} is unreachable")
        _clear_limiter_keys(url)
        yield url
        _clear_limiter_keys(url)
        return
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    server.block_on_close = False
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"redis://127.0.0.1:{server.server_address[1]}/0"
    server.shutdown()
    server.server_close()


def limited_client_with(storage_uri):
    class Config(TestConfig):
        RATELIMIT_ENABLED = True
        RATELIMIT_STORAGE_URI = storage_uri

    return create_app(Config).test_client()


def test_rate_limit_counters_live_in_redis(app, redis_server):
    client = limited_client_with(redis_server)
    try:
        for _ in range(3):
            post(client, "login", email=EMAIL, password=WRONG)
        store = redis.Redis.from_url(redis_server)
        (key,) = store.keys("LIMITS:*")
        assert int(store.get(key)) == 3
        codes = [
            post(client, "login", email=EMAIL, password=WRONG).status_code
            for _ in range(3)
        ]
        assert codes == [401, 401, 429]
    finally:
        limiter.enabled = False


def test_logins_keep_working_and_stay_limited_when_redis_is_down(app):
    client = limited_client_with("redis://127.0.0.1:1/0")
    try:
        codes = [
            post(client, "login", email=EMAIL, password=WRONG).status_code
            for _ in range(6)
        ]
        assert 500 not in codes and codes[-1] == 429
    finally:
        limiter.enabled = False


def test_unreachable_redis_is_reported_at_startup_without_leaking_the_password(caplog):
    try:
        limited_client_with("redis://:s3cretpw@127.0.0.1:1/0")
    finally:
        limiter.enabled = False
    assert "127.0.0.1" in caplog.text and "unreachable" in caplog.text
    assert "s3cretpw" not in caplog.text


def test_reachable_redis_logs_no_warning(redis_server, caplog):
    try:
        limited_client_with(redis_server)
    finally:
        limiter.enabled = False
    assert "unreachable" not in caplog.text


@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "1 2"])
def test_invalid_trusted_proxies_stops_startup_with_a_clear_message(value):
    class Bad(TestConfig):
        TRUSTED_PROXIES = value

    with pytest.raises(RuntimeError, match="TRUSTED_PROXIES must be 0 or a positive"):
        create_app(Bad)


@pytest.mark.parametrize("value", ["0", "1", "2", 1])
def test_valid_trusted_proxies_values_start_fine(value):
    class Good(TestConfig):
        TRUSTED_PROXIES = value

    assert create_app(Good)


def test_login_answers_with_who_signed_in_and_their_memberships(client):
    register(client)
    body = login(client).get_json()
    assert body["token_type"] == "Bearer"
    assert body["user"]["email"] == EMAIL
    assert body["user"]["memberships"] == []
    assert body["user"]["is_platform_owner"] is False


def test_login_lists_who_signed_in_first_and_the_tokens_last(client):
    register(client)
    keys = list(login(client).get_json())
    assert keys == ["user", "token_type", "access_token", "refresh_token"]
