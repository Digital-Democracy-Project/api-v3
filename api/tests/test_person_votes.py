"""OPEN-481: GET /people/{person_id}/votes, one person's votes, newest first."""
import datetime

import pytest
from sqlalchemy import select

from .conftest import TestingSessionLocal
from ..db.models import (
    Bill,
    Jurisdiction,
    LegislativeSession,
    Organization,
    Person,
    PersonVote,
    VoteCount,
    VoteEvent,
)

ADAMS = (
    "ocd-person/00000481-0000-0000-0000-00000000000a"  # many votes, two jurisdictions
)
BERRI = "ocd-person/00000481-0000-0000-0000-00000000000b"  # votes on the same events
NOBODY = "ocd-person/00000481-0000-0000-0000-00000000000c"  # exists, has no votes
UNKNOWN = "ocd-person/00000481-0000-0000-0000-0000000000ff"  # not in the database

# Adams's votes, newest first, written out by hand (the order the endpoint must return).
# Three events share 2026-03-01, so only the vote event id decides their order.
ADAMS_ORDER = [
    "ocd-vote/p481-nobill",  # 2026-04-01, no bill
    "ocd-vote/p481-c",  # 2026-03-01
    "ocd-vote/p481-b",
    "ocd-vote/p481-a",
    "ocd-vote/p481-ne",  # 2025-01-01, Nebraska
]
ADAMS_COUNTS = {"yes": 2, "no": 1, "absent": 1, "abstain": 1}


def _utc(year, month, day):
    return datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)


@pytest.fixture(scope="module", autouse=True)
def person_vote_data():
    db = TestingSessionLocal()
    created = []

    def add(obj):
        db.add(obj)
        created.append(obj)
        return obj

    try:
        sessions = {}
        for abbr in ("ohio", "nebraska"):
            jur = db.execute(
                select(Jurisdiction).where(Jurisdiction.name.ilike(abbr))
            ).scalar_one()
            sessions[abbr] = (
                db.execute(
                    select(LegislativeSession).where(
                        LegislativeSession.jurisdiction_id == jur.id
                    )
                )
                .scalars()
                .first()
            )
        orgs = {
            name: db.execute(
                select(Organization).where(
                    Organization.jurisdiction_id == sess.jurisdiction_id,
                    Organization.classification.in_(["lower", "legislature"]),
                )
            )
            .scalars()
            .first()
            for name, sess in sessions.items()
        }

        for pid, name in (
            (ADAMS, "Jay Adams"),
            (BERRI, "Bea Berri"),
            (NOBODY, "Nel Nobody"),
        ):
            add(Person(id=pid, name=name, party="Independent", extras={}))

        def bill(key):
            sess = sessions[key]
            return add(
                Bill(
                    id=f"ocd-bill/p481-{key}",
                    identifier="HB 481",
                    title=f"A {key} test bill",
                    legislative_session_id=sess.id,
                    from_organization_id=orgs[key].id,
                    extras={},
                    classification=["bill"],
                    subject=[],
                    created_at=_utc(2025, 1, 1),
                    updated_at=_utc(2025, 1, 1),
                )
            )

        oh_bill, ne_bill = bill("ohio"), bill("nebraska")
        db.flush()

        def event(suffix, date, key, bill_obj, votes):
            ev = add(
                VoteEvent(
                    id=f"ocd-vote/p481-{suffix}",
                    identifier=f"V {suffix}",
                    motion_text="Passage",
                    motion_classification=["passage"],
                    start_date=date,
                    result="pass",
                    extras={},
                    organization_id=orgs[key].id,
                    legislative_session_id=str(sessions[key].id),
                    bill_id=bill_obj.id if bill_obj else None,
                )
            )
            db.flush()
            add(VoteCount(vote_event_id=ev.id, option="yes", value=7))
            for voter_id, option, name in votes:
                add(
                    PersonVote(
                        vote_event_id=ev.id,
                        option=option,
                        voter_name=name,
                        voter_id=voter_id,
                    )
                )

        event(
            "a",
            "2026-03-01",
            "ohio",
            oh_bill,
            [(ADAMS, "yes", "Adams"), (BERRI, "no", "Berri")],
        )
        event("b", "2026-03-01", "ohio", oh_bill, [(ADAMS, "no", "Adams")])
        event("c", "2026-03-01", "ohio", oh_bill, [(ADAMS, "yes", "Adams")])
        event(
            "nobill",
            "2026-04-01",
            "ohio",
            None,
            [(ADAMS, "absent", "Adams"), (BERRI, "yes", "Berri")],
        )
        event("ne", "2025-01-01", "nebraska", ne_bill, [(ADAMS, "abstain", "Adams")])
        # a name nobody matched to a legislator: never returned for anyone
        event("unmatched", "2026-05-01", "ohio", oh_bill, [(None, "yes", "Adams")])
        db.commit()
        yield
    finally:
        db.rollback()
        for obj in reversed(created):
            db.delete(db.merge(obj))
            db.flush()
        db.commit()
        db.close()


def _get(client, person_id=ADAMS, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/people/{person_id}/votes" + (f"?{query}" if query else ""))


def _event_ids(response):
    assert response.status_code == 200, response.text
    return [row["vote_event"]["id"] for row in response.json()["results"]]


def test_newest_first_with_total_and_counts(client):
    response = _get(client, per_page=50)
    assert _event_ids(response) == ADAMS_ORDER
    body = response.json()
    assert body["pagination"]["total_items"] == 5
    assert body["summary"]["counts"] == ADAMS_COUNTS


def test_row_shape(client):
    rows = _get(client, per_page=50).json()["results"]
    by_event = {r["vote_event"]["id"]: r for r in rows}
    row = by_event["ocd-vote/p481-c"]
    assert row["option"] == "yes" and row["voter_name"] == "Adams"
    event = row["vote_event"]
    assert event["start_date"] == "2026-03-01"
    assert event["motion_text"] == "Passage"
    assert event["motion_classification"] == ["passage"]
    assert event["result"] == "pass"
    assert event["identifier"] == "V c"
    assert event["organization"]["classification"] in ("lower", "legislature")
    assert event["counts"] == [{"option": "yes", "value": 7}]
    assert row["bill"]["id"] == "ocd-bill/p481-ohio"
    assert row["bill"]["identifier"] == "HB 481"
    assert row["bill"]["session"]
    assert row["bill"]["jurisdiction"]["name"] == "Ohio"


def test_pages_do_not_overlap_or_skip_and_order_is_stable(client):
    seen = []
    for page in (1, 2, 3):
        response = _get(client, per_page=2, page=page)
        seen += _event_ids(response)
        assert response.json()["pagination"]["max_page"] == 3
    assert seen == ADAMS_ORDER
    # the three same-day events sit on a page boundary; a second call gives the same order
    assert _event_ids(_get(client, per_page=2, page=2)) == ADAMS_ORDER[2:4]


def test_summary_is_on_page_one_only_and_covers_every_page(client):
    first = _get(client, per_page=2, page=1).json()
    assert first["summary"]["counts"] == ADAMS_COUNTS
    assert "summary" not in _get(client, per_page=2, page=2).json()


def test_person_with_no_votes_is_an_empty_page(client):
    response = _get(client, NOBODY)
    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert body["pagination"]["total_items"] == 0
    assert body["summary"]["counts"] == {"yes": 0, "no": 0, "absent": 0, "abstain": 0}


def test_unknown_person_is_404(client):
    assert _get(client, UNKNOWN).status_code == 404


def test_bad_paging_is_the_same_4xx_as_other_endpoints(client):
    assert _get(client, per_page=0).status_code == 400
    assert _get(client, per_page=51).status_code == 400
    assert _get(client, page=4, per_page=2).status_code == 404
    assert _get(client, page=0).status_code == 404


def test_no_leakage_between_people(client):
    berri = _get(client, BERRI, per_page=50)
    assert sorted(_event_ids(berri)) == ["ocd-vote/p481-a", "ocd-vote/p481-nobill"]
    assert {r["option"] for r in berri.json()["results"]} == {"no", "yes"}
    assert berri.json()["summary"]["counts"] == {
        "yes": 1,
        "no": 1,
        "absent": 0,
        "abstain": 0,
    }
    for row in _get(client, per_page=50).json()["results"]:
        assert row["voter_name"] == "Adams"


def test_vote_with_no_matched_person_is_returned_for_nobody(client):
    # Adams's name is on p481-unmatched, but its voter_id is NULL: no match on the name
    assert "ocd-vote/p481-unmatched" not in _event_ids(_get(client, per_page=50))
    assert _get(client, NOBODY).json()["pagination"]["total_items"] == 0


def test_vote_with_no_bill_returns_bill_null(client):
    rows = _get(client, per_page=50).json()["results"]
    first = rows[0]
    assert first["vote_event"]["id"] == "ocd-vote/p481-nobill"
    assert "bill" not in first  # null fields are left out, as on the other endpoints


def test_jurisdiction_filter(client):
    ohio = _get(client, jurisdiction="oh", per_page=50)
    assert _event_ids(ohio) == ADAMS_ORDER[:4]
    assert ohio.json()["summary"]["counts"] == {
        "yes": 2,
        "no": 1,
        "absent": 1,
        "abstain": 0,
    }
    nebraska = _get(client, jurisdiction="Nebraska", per_page=50)
    assert _event_ids(nebraska) == ["ocd-vote/p481-ne"]
    assert nebraska.json()["pagination"]["total_items"] == 1
    by_id = _get(
        client,
        jurisdiction="ocd-jurisdiction/country:us/state:ne/government",
        per_page=50,
    )
    assert _event_ids(by_id) == ["ocd-vote/p481-ne"]
    # without the filter, both jurisdictions come back
    assert _get(client, per_page=50).json()["pagination"]["total_items"] == 5


def test_session_filter(client):
    ne_session = _get(client, per_page=50).json()["results"][-1]["bill"]["session"]
    response = _get(client, session=ne_session, per_page=50)
    assert "ocd-vote/p481-ne" in _event_ids(response)
    assert _event_ids(_get(client, session="no-such-session")) == []
