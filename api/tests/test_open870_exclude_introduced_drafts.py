"""OPEN-870: `exclude_introduced_drafts` asks for the OPEN-861 rule alone (an introduced draft's own record is left out),
without the bill-type rule of `bill_types_only`, on /bills, /ddp/search, /suggest, /hydrate, /ddp/sitemap/bills and
/ddp/embedding/ledger. Ramon (2026-11-24): LegBot and the sitemap may cover drafts while they are drafts and drop one once
a bill replaces it. The AK world of test_open861 covers the routes that read bills one by one; the Ohio fixtures cover the
sitemap and the ledger.
"""
import uuid

import pytest
from sqlalchemy import text

from api import bill_types
from api.db.models import Bill, LegislativeSession, RelatedBill

from .conftest import TestingSessionLocal, engine
from .test_ddp_embedding_ledger import _ledger_all
from .test_ddp_sitemap_bills import _all
from .test_open861_introduced_draft import (  # noqa: F401 (fixtures)
    DRAFT,
    INTRODUCED,
    WAITING,
    _bill_list,
    _item,
    _search,
    drafts,
    searchable,
)
from .test_ddp_search import api, world  # noqa: F401 (fixtures)

PAPER = "ocd-bill/t309-ak-84"  # a report filed under a bill number: not a bill type, not a draft


@pytest.fixture
def with_paper(drafts):  # noqa: F811
    _item(drafts, "ak", 84, "R 84")
    drafts.query(Bill).filter(Bill.id == PAPER).update({"classification": ["report"]})
    drafts.commit()
    return drafts


def test_the_switch_alone_leaves_out_the_introduced_draft_and_keeps_a_paper(
    with_paper, client
):
    ids, total = _bill_list(client, exclude_introduced_drafts="true")
    assert DRAFT not in ids
    assert {INTRODUCED, WAITING, PAPER} <= ids
    assert total == len(ids)


def test_bill_types_only_still_leaves_out_the_paper_and_the_draft(with_paper, client):
    ids, _ = _bill_list(client, bill_types_only="true")
    assert DRAFT not in ids and PAPER not in ids


def test_the_bill_list_is_unchanged_when_the_switch_is_off(with_paper, client):
    ids, _ = _bill_list(client, exclude_introduced_drafts="false")
    assert {DRAFT, INTRODUCED, WAITING, PAPER} <= ids


def test_search_suggest_and_hydrate_take_the_switch(searchable):
    text_hits = _search(
        searchable, q="pathology direct billing", exclude_introduced_drafts="true"
    )[1]
    assert DRAFT not in text_hits and {INTRODUCED, WAITING} <= text_hits
    assert _search(searchable, q="LC 80", exclude_introduced_drafts="true")[0] == set()
    r = searchable.get(
        "/ddp/search/suggest",
        params={"q": "LC 8", "jurisdiction": "ak", "exclude_introduced_drafts": "true"},
    )
    got = {h["id"] for h in r.json()["results"]}
    assert DRAFT not in got and WAITING in got
    r = searchable.get(
        "/ddp/search/hydrate",
        params={
            "id": [DRAFT, INTRODUCED],
            "jurisdiction": "ak",
            "exclude_introduced_drafts": "true",
        },
    )
    assert {h["id"] for h in r.json()["results"]} == {INTRODUCED}


def test_with_no_links_the_switch_hides_nothing(drafts, client, monkeypatch):
    monkeypatch.setattr(bill_types, "INTRODUCED_DRAFT_LINKS", [])
    ids, _ = _bill_list(client, exclude_introduced_drafts="true")
    assert DRAFT in ids


# --- the sitemap and the ledger (Ohio fixtures) ----------------------------------------------------


@pytest.fixture
def oh_replaced(client, monkeypatch):
    """The first ledger bill X is replaced by another Ohio bill: the link names X as the draft."""
    ledger = _ledger_all(client)
    assert len(ledger) >= 2
    x = "ocd-bill/" + sorted(ledger)[0]
    db = TestingSessionLocal()
    try:
        bill = db.query(Bill).filter(Bill.id == x).one()
        jid = (
            db.query(LegislativeSession.jurisdiction_id)
            .filter(LegislativeSession.id == bill.legislative_session_id)
            .scalar()
        )
        other = (
            db.query(Bill)
            .filter(
                Bill.id != x, Bill.legislative_session_id == bill.legislative_session_id
            )
            .first()
        )
        link = RelatedBill(
            id=uuid.uuid4(),
            bill_id=other.id,
            related_bill_id=x,
            identifier=bill.identifier,
            legislative_session="x",
            relation_type="replaces",
        )
        db.add(link)
        db.commit()
        link_id = link.id
    finally:
        db.close()
    monkeypatch.setattr(bill_types, "INTRODUCED_DRAFT_LINKS", [f"{jid}#replaces"])
    yield x.removeprefix("ocd-bill/")
    with engine.begin() as c:
        c.execute(
            text("DELETE FROM opencivicdata_relatedbill WHERE id = :i"), {"i": link_id}
        )


def test_the_sitemap_leaves_out_a_replaced_draft_only_when_asked(client, oh_replaced):
    assert oh_replaced in {b["ocd_bill_id"] for b in _all(client)}
    got = {
        b["ocd_bill_id"]
        for p in _pages_with(client, exclude_introduced_drafts="true")
        for b in p["results"]
    }
    assert oh_replaced not in got and len(got) == len(_all(client)) - 1


def test_the_ledger_leaves_out_a_replaced_draft_only_when_asked(client, oh_replaced):
    assert oh_replaced in _ledger_all(client)
    r = client.get(
        "/ddp/embedding/ledger",
        params={
            "jurisdiction": "oh",
            "limit": 500,
            "exclude_introduced_drafts": "true",
        },
    )
    assert oh_replaced not in {b["ocd_bill_id"] for b in r.json()["results"]}


def _pages_with(client, **extra):
    pages, after = [], None
    while True:
        params = {"jurisdiction": "oh", "limit": 1000, **extra}
        if after:
            params["after"] = after
        page = client.get("/ddp/sitemap/bills", params=params).json()
        pages.append(page)
        after = page["next_after"]
        if not after:
            return pages
