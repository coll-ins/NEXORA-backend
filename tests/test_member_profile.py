"""Member personal details, share capital and data-protection consent."""

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models.audit import AuditLog
from app.models.consent import MemberDataConsent
from app.models.membership import MembershipStatus, SaccoMembership
from test_sacco_detail import ADMIN, MEMBER, STAFF, auth, make_sacco, make_user

NAME = "Wanjiru Mwangi"
ID = "12345678"
FULL = {
    "full_name": NAME,
    "national_id": ID,
    "gender": "female",
    "date_of_birth": "1990-06-15",
}


@pytest.fixture(autouse=True)
def clean_tables(app):
    def truncate():
        db.session.remove()
        db.session.execute(
            text("TRUNCATE refresh_tokens, users, saccos RESTART IDENTITY CASCADE")
        )
        db.session.commit()

    truncate()
    yield
    truncate()


class P:
    """A person in a SACCO: their membership id and auth header."""

    def __init__(self, sacco_id, role=MEMBER, owner=False):
        self.user_id = make_user(owner=owner)
        self.sacco_id = sacco_id
        m = SaccoMembership(
            user_id=self.user_id,
            sacco_id=sacco_id,
            role=role,
            status=MembershipStatus.ACTIVE,
        )
        db.session.add(m)
        db.session.commit()
        self.mid = m.id
        self.headers = auth(self.user_id)


def base(p, tail):
    return f"/api/v1/saccos/{p.sacco_id}/members{tail}"


def consent(client, p, version=1):
    return client.post(
        base(p, "/me/data-consent"), json={"notice_version": version}, headers=p.headers
    )


def patch(client, actor, target, body):
    return client.patch(
        base(actor, f"/{target.mid}/profile"), json=body, headers=actor.headers
    )


def get(client, actor, target):
    return client.get(base(actor, f"/{target.mid}/profile"), headers=actor.headers)


def fill(client, admin, me):
    """Everything in FULL: the member's own details, then the ID an officer records."""
    own = {k: v for k, v in FULL.items() if k != "national_id"}
    assert patch(client, me, me, own).status_code == 200
    return patch(client, admin, me, {"national_id": ID})


def audit(action):
    db.session.expire_all()
    return db.session.query(AuditLog).filter_by(action=action).all()


@pytest.fixture
def world():
    sacco_id = make_sacco()
    return sacco_id, P(sacco_id, ADMIN), P(sacco_id, MEMBER), P(sacco_id, MEMBER)


# ---- consent --------------------------------------------------------------------


def test_the_member_reads_the_wording_and_has_no_consent_yet(client, world):
    _, _, me, _ = world
    seen = client.get(base(me, "/me/data-consent"), headers=me.headers).json
    assert seen["consent"] is None
    assert seen["notice"]["version"] == 1 and "national ID" in seen["notice"]["text"]
    assert "circle" not in seen["notice"]["text"].lower()


def test_agreeing_is_recorded_once_and_audited(client, world):
    _, _, me, _ = world
    first = consent(client, me)
    assert first.status_code == 201
    assert first.json["consent"]["notice_version"] == 1
    again = consent(client, me)
    assert again.status_code == 200
    assert db.session.query(MemberDataConsent).count() == 1
    (row,) = audit("consent.grant")
    assert row.actor_id == me.user_id and f"membership:{me.mid}" in row.target


@pytest.mark.parametrize("version", [2, 0, "1", True, None])
def test_the_member_must_name_the_wording_version_they_read(client, world, version):
    _, _, me, _ = world
    resp = consent(client, me, version)
    assert resp.status_code in (400, 409)
    assert db.session.query(MemberDataConsent).count() == 0


def test_only_a_member_can_give_consent_and_only_for_themselves(client, world):
    sacco_id, admin, me, _ = world
    stranger = P(make_sacco(), MEMBER)
    out = client.get(
        f"/api/v1/saccos/{sacco_id}/members/me/data-consent", headers=stranger.headers
    )
    assert out.status_code == 404
    owner = P(sacco_id, MEMBER, owner=True)
    db.session.delete(db.session.get(SaccoMembership, owner.mid))
    db.session.commit()
    assert client.post(
        base(me, "/me/data-consent"), json={"notice_version": 1}, headers=owner.headers
    ).status_code in (403, 404)
    # An admin cannot give consent for someone else: the route only has "me".
    consent(client, admin)
    assert (
        db.session.query(MemberDataConsent).filter_by(membership_id=me.mid).count() == 0
    )


def test_withdrawing_erases_the_four_details_and_keeps_the_rest(client, world):
    _, admin, me, _ = world
    consent(client, me)
    assert fill(client, admin, me).status_code == 200
    assert patch(client, admin, me, {"share_capital": 500000}).status_code == 200

    done = client.post(base(me, "/me/data-consent/withdraw"), headers=me.headers)
    assert done.status_code == 200
    assert sorted(done.json["erased"]) == sorted(FULL)
    kept = get(client, me, me).json
    assert kept["consent"] is None
    assert [kept[f] for f in FULL] == [None] * 4
    assert kept["share_capital"] == 500000  # not personal data

    (row,) = audit("consent.withdraw")
    assert "national_id" in row.target
    assert ID not in row.target and NAME not in row.target
    # The record that consent existed stays, with its end.
    stored = db.session.query(MemberDataConsent).one()
    assert stored.withdrawn_at is not None
    # Nothing left to withdraw; agreeing again is a new row.
    assert (
        client.post(
            base(me, "/me/data-consent/withdraw"), headers=me.headers
        ).status_code
        == 409
    )
    assert consent(client, me).status_code == 201
    assert db.session.query(MemberDataConsent).count() == 2


# ---- the profile ----------------------------------------------------------------


def test_personal_details_need_the_members_consent_first(client, world):
    _, admin, me, _ = world
    blocked = patch(
        client, me, me, {k: v for k, v in FULL.items() if k != "national_id"}
    )
    assert blocked.status_code == 409
    assert blocked.json["error"]["code"] == "CONSENT_REQUIRED"
    assert patch(client, admin, me, FULL).status_code == 409  # an admin cannot stand in
    assert get(client, admin, me).json["national_id"] is None
    assert audit("member.profile_update") == []
    # Clearing needs no consent, and share capital is not personal data.
    assert patch(client, me, me, {"full_name": None}).status_code == 200
    assert patch(client, admin, me, {"share_capital": 0}).status_code == 200


def test_the_member_and_their_admin_can_read_and_write_with_consent(client, world):
    _, admin, me, _ = world
    consent(client, me)
    done = fill(client, admin, me)
    assert done.status_code == 200, done.json
    assert done.json["consent"]["notice_version"] == 1
    assert {k: get(client, admin, me).json[k] for k in FULL} == FULL
    fix = patch(client, admin, me, {"full_name": "Wanjiru W. Mwangi"})
    assert fix.status_code == 200 and fix.json["full_name"] == "Wanjiru W. Mwangi"

    rows = audit("member.profile_update")
    assert len(rows) == 3  # member, officer (ID), officer (name fix)
    everything = " ".join(r.target for r in rows)
    assert "national_id" in everything
    for secret in (ID, "Mwangi", "1990-06-15"):
        assert secret not in everything


def test_other_members_cannot_read_or_write_it_and_cannot_set_share_capital(
    client, world
):
    sacco_id, admin, me, other = world
    consent(client, me)
    fill(client, admin, me)
    assert get(client, other, me).status_code == 403
    assert patch(client, other, me, {"full_name": "Eve Hacker"}).status_code == 403
    secretary = P(sacco_id, STAFF)  # a staff title that only views members
    assert get(client, secretary, me).status_code == 403
    mine = patch(client, me, me, {"share_capital": 1})
    assert mine.status_code == 403
    assert get(client, admin, me).json["share_capital"] is None


def test_the_platform_owner_does_not_read_personal_details(client, world):
    sacco_id, admin, me, _ = world
    consent(client, me)
    fill(client, admin, me)
    owner = P(make_sacco(), ADMIN, owner=True)
    resp = client.get(base(me, f"/{me.mid}/profile"), headers=owner.headers)
    assert resp.status_code in (403, 404)
    assert "12345678" not in resp.get_data(as_text=True)


def test_another_saccos_admin_gets_404(client, world):
    _, _, me, _ = world
    other_admin = P(make_sacco(), ADMIN)
    resp = client.get(base(me, f"/{me.mid}/profile"), headers=other_admin.headers)
    assert resp.status_code == 404


def test_a_membership_id_from_another_sacco_is_404_not_data(client, world):
    sacco_id, admin, _, _ = world
    elsewhere = P(make_sacco(), MEMBER)
    resp = client.get(
        f"/api/v1/saccos/{sacco_id}/members/{elsewhere.mid}/profile",
        headers=admin.headers,
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("full_name", "A"),
        ("full_name", "x" * 121),
        ("full_name", "Wanjiru‮Mwangi"),
        ("full_name", "Wanjiru123"),
        ("full_name", "<script>"),
        ("full_name", 5),
        ("national_id", "123456"),
        ("national_id", "123456789"),
        ("national_id", "1234 5678"),
        ("national_id", "٣٤٥٦٧٨٩٠"),
        ("national_id", 12345678),
        ("gender", "F"),
        ("gender", "Female"),
        ("gender", 1),
        ("date_of_birth", "15/06/1990"),
        ("date_of_birth", "19900615"),
        ("date_of_birth", "1990-02-30"),
        ("date_of_birth", "2999-01-01"),
        ("date_of_birth", "1800-01-01"),
        ("share_capital", -1),
        ("share_capital", 1.5),
        ("share_capital", "100"),
        ("share_capital", True),
        ("share_capital", 10**12 + 1),
    ],
)
def test_a_bad_value_is_400_with_the_field_named(client, world, field, value):
    _, admin, me, _ = world
    consent(client, me)
    actor = admin if field in ("share_capital", "national_id") else me
    resp = patch(client, actor, me, {field: value})
    assert resp.status_code == 400
    assert field in resp.json["error"]["fields"]
    assert audit("member.profile_update") == []


def test_unknown_or_empty_bodies_are_400(client, world):
    _, _, me, _ = world
    assert patch(client, me, me, {}).status_code == 400
    assert patch(client, me, me, {"email": "x@y.z"}).status_code == 400
    assert (
        patch(client, me, me, {"full_name": NAME, "role": "admin"}).status_code == 400
    )


def test_a_national_id_belongs_to_one_member_of_a_sacco(client, world):
    _, admin, me, other = world
    consent(client, me)
    consent(client, other)
    assert patch(client, admin, me, {"national_id": ID}).status_code == 200
    assert patch(client, admin, other, {"national_id": ID}).status_code == 409
    assert get(client, admin, other).json["national_id"] is None
    # The same number in a different SACCO is a different record.
    elsewhere = make_sacco()
    away_admin, away = P(elsewhere, ADMIN), P(elsewhere, MEMBER)
    consent(client, away)
    assert patch(client, away_admin, away, {"national_id": ID}).status_code == 200


def test_a_member_cannot_set_a_national_id_so_cannot_probe_for_taken_ones(
    client, world
):
    _, admin, me, other = world
    consent(client, me)
    consent(client, other)
    patch(client, admin, other, {"national_id": ID})
    for value in (ID, "87654321"):  # taken and free answer the same way
        assert patch(client, me, me, {"national_id": value}).status_code == 403


def test_a_deactivated_member_can_still_withdraw_and_erase(client, world):
    _, admin, me, _ = world
    consent(client, me)
    fill(client, admin, me)
    row = db.session.get(SaccoMembership, me.mid)
    row.status = MembershipStatus.INACTIVE
    db.session.commit()
    done = client.post(base(me, "/me/data-consent/withdraw"), headers=me.headers)
    assert done.status_code == 200, done.json
    db.session.expire_all()
    assert db.session.get(SaccoMembership, me.mid).national_id is None


def test_withdrawing_needs_a_login_and_a_membership_of_that_sacco(client, world):
    _, _, me, _ = world
    url = base(me, "/me/data-consent/withdraw")
    assert client.post(url).status_code == 401
    stranger = P(make_sacco(), MEMBER)
    assert client.post(url, headers=stranger.headers).status_code == 404


def test_saving_the_same_values_again_writes_no_audit_row(client, world):
    _, admin, me, _ = world
    consent(client, me)
    admin_fill = patch(client, admin, me, {"share_capital": 5})
    assert admin_fill.status_code == 200
    before = len(audit("member.profile_update"))
    assert patch(client, admin, me, {"share_capital": 5}).status_code == 200
    assert len(audit("member.profile_update")) == before


def test_the_member_list_never_carries_personal_details(client, world):
    sacco_id, admin, me, _ = world
    consent(client, me)
    fill(client, admin, me)
    patch(client, admin, me, {"share_capital": 100})
    listing = client.get(base(admin, ""), headers=admin.headers).get_data(as_text=True)
    one = client.get(base(admin, f"/{me.mid}"), headers=admin.headers).get_data(
        as_text=True
    )
    for body in (listing, one):
        assert ID not in body and "Mwangi" not in body and "1990" not in body


def test_the_database_refuses_bad_values_even_if_the_app_does_not(app, world):
    _, _, me, _ = world
    for column, value in (
        ("gender", "x"),
        ("national_id", "12"),
        ("date_of_birth", "1800-01-01"),
        ("share_capital", "-5"),
    ):
        with pytest.raises(Exception, match="ck_memberships"):
            db.session.execute(
                text(f"UPDATE sacco_memberships SET {column} = :v WHERE id = :i"),
                {"v": value, "i": me.mid},
            )
        db.session.rollback()


def test_a_request_that_waited_for_the_row_lock_compares_with_fresh_data(client, world):
    from app.members import profile_routes

    _, _, me, _ = world
    consent(client, me)
    patch(client, me, me, {"full_name": "Aaa Bbb"})
    real = profile_routes._locked

    def after_another_commit(sacco_id, membership_id):
        with db.engine.begin() as other:  # another request commits while this waits
            other.execute(
                text(
                    "UPDATE sacco_memberships SET full_name = 'Ccc Ddd' WHERE id = :i"
                ),
                {"i": membership_id},
            )
        return real(sacco_id, membership_id)

    profile_routes._locked = after_another_commit
    try:
        done = patch(client, me, me, {"full_name": "Aaa Bbb"})
    finally:
        profile_routes._locked = real
    assert done.status_code == 200 and done.json["full_name"] == "Aaa Bbb"
    db.session.expire_all()
    assert db.session.get(SaccoMembership, me.mid).full_name == "Aaa Bbb"
