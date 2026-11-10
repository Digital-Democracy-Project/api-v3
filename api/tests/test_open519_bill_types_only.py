"""OPEN-519: `bill_types_only` leaves out items whose label is not a bill type, on the bill list and on search.

Ramon (2026-11-10, NEXT-182): only bill types are listed and counted as bills. A Massachusetts report, order,
communication or study request (OPEN-517/518) stays in the data and opens by direct link. Off by default, so every
other reader of api-v3 gets what it got before. Uses test_ddp_search's Alaska/Wyoming world (cleaned up after each
test) with three more Alaska items whose titles share the words the search tests look for.
"""
import pytest
from sqlalchemy import text

from api import bill_types
from api.db.models import Bill

from .conftest import engine
from .test_ddp_search import (  # noqa: F401 (fixtures)
    SESSIONS,
    T0,
    _ids,
    api,
    built,
    world,
)

REPORT = "ocd-bill/t309-ak-90"
UNLABELLED = "ocd-bill/t309-ak-91"
PETITION = "ocd-bill/t309-ak-92"
MEDICAID_BILL = "ocd-bill/t309-ak-1"


def _item(db, n, identifier, title, classification):
    db.add(
        Bill(
            id=f"ocd-bill/t309-ak-{n}",
            identifier=identifier,
            title=title,
            legislative_session_id=SESSIONS["ak"],
            from_organization_id="org309-ak-lower",
            subject=[],
            classification=classification,
            extras={},
            created_at=T0,
            updated_at=T0,
            latest_action_date="2026-01-03",
            latest_action_description="Placed on file",
        )
    )
    db.commit()


@pytest.fixture
def papers(world):  # noqa: F811
    db = world
    _item(db, 90, "HD 90", "Medicaid Expansion Commission Annual Report", ["report"])
    # a blank label is a bill (fail open): a scraper that leaves it empty never hides a bill
    _item(db, 91, "HD 91", "Medicaid Expansion Waiver Act", [])
    # on neither list: left out by the rule, and flagged by the check
    _item(db, 92, "HD 92", "Medicaid Expansion Petition", ["petition"])
    return db


@pytest.fixture
def searchable(papers, api):  # noqa: F811
    r = api.post("/ddp/search/refresh")
    assert r.status_code == 200 and not r.json()["more"]
    return api


# --- the lists ------------------------------------------------------------------------------------


def test_the_two_lists_do_not_overlap():
    assert not set(bill_types.BILL_TYPES) & set(bill_types.NOT_BILL_TYPES)


def test_the_lists_match_what_the_data_held_on_2026_11_10():
    # every label on the Mac replica that day, all jurisdictions; report/communication/order come with OPEN-517
    seen = {
        "bill",
        "resolution",
        "proposed bill",
        "concurrent resolution",
        "joint resolution",
        "memorial",
        "joint memorial",
        "concurrent memorial",
        "constitutional amendment",
        "study request",
    }
    assert seen <= set(bill_types.BILL_TYPES) | set(bill_types.NOT_BILL_TYPES)
    assert (
        "study request" in bill_types.NOT_BILL_TYPES
    )  # Louisiana's HSR 1 and 2 (Ramon)
    assert "proposed bill" in bill_types.BILL_TYPES  # 339 real Massachusetts measures


def test_the_check_names_a_label_on_neither_list(papers, monkeypatch, capsys):
    with engine.connect() as conn:
        assert bill_types.unknown_labels(conn) == ["petition"]
    monkeypatch.setattr("api.db.engine", engine)
    assert bill_types.main(["check"]) == 1
    assert "'petition'" in capsys.readouterr().out


def test_the_check_passes_when_every_label_is_known(papers, monkeypatch, capsys):
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE opencivicdata_bill SET classification = '{order}' WHERE id = :id"
            ),
            {"id": PETITION},
        )
    monkeypatch.setattr("api.db.engine", engine)
    assert bill_types.main(["check"]) == 0


# --- GET /bills -----------------------------------------------------------------------------------


def _bill_list(client, **params):
    r = client.get("/bills", params={"jurisdiction": "ak", "per_page": 20, **params})
    assert r.status_code == 200, r.text
    body = r.json()
    return {b["id"] for b in body["results"]}, body["pagination"]["total_items"]


def test_the_bill_list_is_unchanged_by_default(papers, client):
    ids, total = _bill_list(client)
    assert {REPORT, UNLABELLED, PETITION, MEDICAID_BILL} <= ids
    assert total == len(ids) == 6


def test_the_bill_list_lists_and_counts_only_bill_types(papers, client):
    ids, total = _bill_list(client, bill_types_only="true")
    assert REPORT not in ids and PETITION not in ids
    assert {UNLABELLED, MEDICAID_BILL} <= ids
    assert total == len(ids) == 4  # the count drops with the rows, so pages stay full


def test_the_bill_list_pages_count_only_bill_types(papers, client):
    r = client.get(
        "/bills",
        params={
            "jurisdiction": "ak",
            "per_page": 3,
            "page": 2,
            "bill_types_only": "true",
        },
    )
    assert r.status_code == 200, r.text
    p = r.json()["pagination"]
    assert (p["total_items"], p["max_page"], len(r.json()["results"])) == (4, 2, 1)


# --- /ddp/search, /suggest, /hydrate --------------------------------------------------------------


def _search(client, **params):
    r = client.get(
        "/ddp/search", params={"jurisdiction": "ak", "types": "bill", **params}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return set(_ids(body["exact"])), set(_ids(body["text"])), set(_ids(body["names"]))


def test_search_finds_the_papers_by_default(searchable):
    exact, text_hits, names = _search(searchable, q="medicaid expansion")
    assert {REPORT, PETITION, UNLABELLED, MEDICAID_BILL} <= text_hits
    assert {REPORT, PETITION} <= names
    assert _search(searchable, q="HD 90")[0] == {REPORT}


def test_search_leaves_out_what_is_not_a_bill_type(searchable):
    exact, text_hits, names = _search(
        searchable, q="medicaid expansion", bill_types_only="true"
    )
    assert REPORT not in text_hits | names and PETITION not in text_hits | names
    assert {UNLABELLED, MEDICAID_BILL} <= text_hits
    # the exact bill-number tier too
    assert _search(searchable, q="HD 90", bill_types_only="true")[0] == set()


def test_search_reads_the_label_live_not_from_the_index(searchable):
    # the OPEN-517 relabel does not move updated_at, so the index is never rebuilt for it
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE opencivicdata_bill SET classification = '{report}' WHERE id = :id"
            ),
            {"id": MEDICAID_BILL},
        )
    text_hits = _search(searchable, q="medicaid expansion", bill_types_only="true")[1]
    assert MEDICAID_BILL not in text_hits


def _suggest(client, q, **params):
    r = client.get(
        "/ddp/search/suggest", params={"q": q, "jurisdiction": "ak", **params}
    )
    assert r.status_code == 200, r.text
    return set(_ids(r.json()["results"]))


def test_suggest_leaves_out_what_is_not_a_bill_type(searchable):
    assert REPORT in _suggest(searchable, "HD 9")
    assert REPORT in _suggest(searchable, "medicaid expansion commission")
    only = _suggest(searchable, "HD 9", bill_types_only="true")
    assert REPORT not in only and PETITION not in only and UNLABELLED in only
    assert REPORT not in _suggest(
        searchable, "medicaid expansion commission", bill_types_only="true"
    )


def _hydrate(client, ids, **params):
    r = client.get(
        "/ddp/search/hydrate", params={"id": ids, "jurisdiction": "ak", **params}
    )
    assert r.status_code == 200, r.text
    return {h["id"]: h for h in r.json()["results"]}


def test_hydrate_returns_each_bills_label(searchable):
    hits = _hydrate(searchable, [REPORT, UNLABELLED, MEDICAID_BILL])
    assert hits[REPORT]["classification"] == ["report"]
    assert hits[UNLABELLED]["classification"] == []
    assert hits[MEDICAID_BILL]["classification"] == ["bill"]


def test_hydrate_leaves_out_what_is_not_a_bill_type(searchable):
    hits = _hydrate(
        searchable,
        [REPORT, PETITION, UNLABELLED, MEDICAID_BILL],
        bill_types_only="true",
    )
    assert set(hits) == {UNLABELLED, MEDICAID_BILL}


def test_people_are_untouched_by_the_option(searchable):
    r = searchable.get(
        "/ddp/search",
        params={
            "q": "Smithson",
            "jurisdiction": "ak",
            "types": "person",
            "bill_types_only": "true",
        },
    )
    assert "ocd-person/t309-ak-1" in _ids(r.json()["names"])


def test_an_item_with_a_bill_type_among_its_labels_is_kept(papers, api):  # noqa: F811
    # None exist today (Mac replica 2026-11-10). Any bill-type label keeps an item, failing open like a blank label.
    _item(papers, 93, "HD 93", "Medicaid Expansion Mixed Act", ["bill", "report"])
    assert "ocd-bill/t309-ak-93" in _bill_list(api, bill_types_only="true")[0]
    assert api.post("/ddp/search/refresh").status_code == 200
    text_hits = _search(api, q="medicaid expansion", bill_types_only="true")[1]
    assert "ocd-bill/t309-ak-93" in text_hits
