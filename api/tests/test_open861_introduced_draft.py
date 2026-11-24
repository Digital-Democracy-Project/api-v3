"""OPEN-861: `bill_types_only` leaves out a bill draft's own record once the draft is introduced as a bill.

Ramon (2026-11-24): hide the old draft record, store it, never delete it. Montana's pre-filing scrape stores each draft
("LC nnnn") as a bill; once introduced, the bill links to its draft number with the relation Montana's core
rules name (`INTRODUCED_DRAFT_RELATION`, "replaces"), and the draft's record stays. Here Alaska stands in for Montana:
the links are set to Alaska's, as core would give them for a state with the value. Uses test_ddp_search's
Alaska/Wyoming world.
"""
import uuid

import pytest
from sqlalchemy import text

from api import bill_types
from api.db.models import Bill, RelatedBill

from .conftest import engine
from .test_ddp_search import (  # noqa: F401 (fixtures)
    AK,
    SESSIONS,
    T0,
    _ids,
    api,
    world,
)

DRAFT = "ocd-bill/t309-ak-80"  # the draft's record, introduced as INTRODUCED
INTRODUCED = "ocd-bill/t309-ak-81"
WAITING = "ocd-bill/t309-ak-82"  # a draft not introduced yet: stays listed (pre-filing)
COMPANION = "ocd-bill/t309-ak-83"  # linked to by another kind of relation: stays listed
WY_REPLACED = "ocd-bill/t309-wy-80"  # "replaces" in a state whose rules name no draft relation: stays listed
WY_REPLACING = "ocd-bill/t309-wy-81"
TITLE = "Pathology Direct Billing Revisions"


def _item(db, code, n, identifier):
    db.add(
        Bill(
            id=f"ocd-bill/t309-{code}-{n}",
            identifier=identifier,
            title=TITLE,
            legislative_session_id=SESSIONS[code],
            from_organization_id=f"org309-{code}-lower",
            subject=[],
            classification=["bill"],
            extras={},
            created_at=T0,
            updated_at=T0,
            latest_action_date="2026-01-04",
            latest_action_description="Introduced",
        )
    )
    db.commit()


def _link(db, bill_id, related_bill_id, identifier, relation_type):
    db.add(
        RelatedBill(
            id=uuid.uuid4(),
            bill_id=bill_id,
            related_bill_id=related_bill_id,
            identifier=identifier,
            legislative_session="2026",
            relation_type=relation_type,
        )
    )
    db.commit()


@pytest.fixture
def drafts(world, monkeypatch):  # noqa: F811
    db = world
    _item(db, "ak", 80, "LC 80")
    _item(db, "ak", 81, "SB 81")
    _item(db, "ak", 82, "LC 82")
    _item(db, "ak", 83, "HB 83")
    _item(db, "wy", 80, "HB 80")
    _item(db, "wy", 81, "HB 81")
    _link(db, INTRODUCED, DRAFT, "LC 80", "replaces")
    # a draft never stored: the link stays unresolved and hides nothing
    _link(db, INTRODUCED, None, "LC 99", "replaces")
    _link(db, INTRODUCED, COMPANION, "HB 83", "companion")
    _link(db, WY_REPLACING, WY_REPLACED, "HB 80", "replaces")
    monkeypatch.setattr(bill_types, "INTRODUCED_DRAFT_LINKS", [f"{AK}#replaces"])
    yield db
    with engine.begin() as c:
        c.execute(
            text(
                "DELETE FROM opencivicdata_relatedbill WHERE bill_id LIKE 'ocd-bill/t309-%'"
            )
        )


@pytest.fixture
def searchable(drafts, api):  # noqa: F811
    r = api.post("/ddp/search/refresh")
    assert r.status_code == 200 and not r.json()["more"]
    return api


# --- where the links come from --------------------------------------------------------------------


def test_the_links_come_from_each_states_core_rules(monkeypatch):
    monkeypatch.setattr(
        bill_types,
        "state_values",
        lambda attr: {"mt": "replaces"} if attr == "INTRODUCED_DRAFT_RELATION" else {},
    )
    assert bill_types.introduced_draft_links() == [
        "ocd-jurisdiction/country:us/state:mt/government#replaces"
    ]
    monkeypatch.setattr(bill_types, "state_values", lambda attr: {})
    # a core without the value hides nothing
    assert bill_types.introduced_draft_links() == []


# --- GET /bills -----------------------------------------------------------------------------------


def _bill_list(client, jurisdiction="ak", **params):
    r = client.get(
        "/bills", params={"jurisdiction": jurisdiction, "per_page": 20, **params}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return {b["id"] for b in body["results"]}, body["pagination"]["total_items"]


def test_the_bill_list_is_unchanged_by_default(drafts, client):
    ids, total = _bill_list(client)
    assert {DRAFT, INTRODUCED, WAITING, COMPANION} <= ids
    assert total == len(ids) == 7


def test_the_bill_list_leaves_out_and_does_not_count_an_introduced_draft(
    drafts, client
):
    ids, total = _bill_list(client, bill_types_only="true")
    assert DRAFT not in ids
    assert {INTRODUCED, WAITING, COMPANION} <= ids
    assert total == len(ids) == 6


def test_another_states_replaced_bill_stays_listed(drafts, client):
    ids, total = _bill_list(client, jurisdiction="wy", bill_types_only="true")
    assert {WY_REPLACED, WY_REPLACING} <= ids


def test_the_draft_stays_stored_and_opens_by_direct_link(drafts, client):
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM opencivicdata_bill WHERE id = :id"),
                {"id": DRAFT},
            ).scalar()
            == 1
        )
    r = client.get("/bills/" + DRAFT)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == DRAFT


def test_with_no_links_nothing_is_hidden(drafts, client, monkeypatch):
    monkeypatch.setattr(bill_types, "INTRODUCED_DRAFT_LINKS", [])
    ids, _ = _bill_list(client, bill_types_only="true")
    assert DRAFT in ids


# --- /ddp/search, /suggest, /hydrate --------------------------------------------------------------


def _search(client, **params):
    r = client.get(
        "/ddp/search", params={"jurisdiction": "ak", "types": "bill", **params}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return set(_ids(body["exact"])), set(_ids(body["text"])), set(_ids(body["names"]))


def test_search_leaves_out_an_introduced_draft(searchable):
    assert DRAFT in _search(searchable, q="pathology direct billing")[1]
    assert _search(searchable, q="LC 80")[0] == {DRAFT}
    text_hits = _search(
        searchable, q="pathology direct billing", bill_types_only="true"
    )[1]
    assert DRAFT not in text_hits and {INTRODUCED, WAITING, COMPANION} <= text_hits
    assert _search(searchable, q="LC 80", bill_types_only="true")[0] == set()


def test_suggest_leaves_out_an_introduced_draft(searchable):
    def suggest(**params):
        r = searchable.get(
            "/ddp/search/suggest", params={"q": "LC 8", "jurisdiction": "ak", **params}
        )
        assert r.status_code == 200, r.text
        return set(_ids(r.json()["results"]))

    assert DRAFT in suggest()
    only = suggest(bill_types_only="true")
    assert DRAFT not in only and WAITING in only


def test_hydrate_leaves_out_an_introduced_draft(searchable):
    def hydrate(**params):
        r = searchable.get(
            "/ddp/search/hydrate",
            params={"id": [DRAFT, INTRODUCED], "jurisdiction": "ak", **params},
        )
        assert r.status_code == 200, r.text
        return {h["id"] for h in r.json()["results"]}

    assert hydrate() == {DRAFT, INTRODUCED}
    assert hydrate(bill_types_only="true") == {INTRODUCED}
