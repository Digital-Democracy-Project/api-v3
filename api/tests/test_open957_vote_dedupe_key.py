"""OPEN-957: each vote event carries its dedupe_key (include=votes), so the unique-vote-keys quality gate can read
it through the API. One additive field: nothing else in the vote event changes."""

from api.schemas import VoteEvent as VoteEventSchema


def _votes(client, path):
    response = client.get(path)
    assert response.status_code == 200
    return {v["id"]: v for v in response.json()["votes"]}


def test_single_bill_detail_returns_each_vote_events_key(client):
    votes = _votes(client, "/bills/oh/2021/HB 1?include=votes")
    # the stored key comes back verbatim; a vote event with none stored leaves the field out (the routes drop nulls)
    assert votes["ocd-vote/1"]["dedupe_key"] == "oh-2021-hb1-house-floor-vote"
    assert "dedupe_key" not in votes["ocd-vote/2"]


def test_the_list_endpoint_returns_it_too(client):
    response = client.get("/bills?q=HB1&include=votes")
    assert response.status_code == 200
    keys = {
        v["id"]: v.get("dedupe_key") for v in response.json()["results"][0]["votes"]
    }
    assert keys == {"ocd-vote/1": "oh-2021-hb1-house-floor-vote", "ocd-vote/2": None}


def test_the_other_vote_event_fields_are_unchanged(client):
    vote = _votes(client, "/bills/oh/2021/HB 1?include=votes")["ocd-vote/1"]
    assert set(vote) == {
        "id",
        "dedupe_key",
        "identifier",
        "motion_text",
        "motion_classification",
        "start_date",
        "result",
        "extras",
        "organization",
        "votes",
        "counts",
        "sources",
    }
    assert vote["identifier"] == "Vote on HB1"
    assert vote["motion_text"] == "Floor Vote"
    assert vote["counts"] == [
        {"option": "yes", "value": 2},
        {"option": "no", "value": 1},
    ]


def test_no_votes_include_means_no_votes_key(client):
    assert "votes" not in client.get("/bills/oh/2021/HB 1").json()


def test_a_vote_event_without_the_field_still_validates():
    """An older stored/cached response (no dedupe_key at all) must still parse."""
    old = {
        "id": "ocd-vote/old",
        "motion_text": "Floor Vote",
        "start_date": "2021-01-01",
        "result": "passed",
        "identifier": "Vote on HB1",
        "extras": {},
        "organization": {"id": "ohh", "name": "Ohio House", "classification": "lower"},
        "votes": [],
        "counts": [],
        "sources": [],
    }
    assert VoteEventSchema.parse_obj(old).dedupe_key is None
    assert VoteEventSchema.parse_obj({**old, "dedupe_key": "k"}).dedupe_key == "k"
