"""SYNC-95 / OPEN-319: the embedding ledger (/ddp/embedding/ledger) and the archived_updated_at version
field. The ledger must never disagree with the bill detail's own archived documents, so most tests
compare the two directly; the controlled shapes use test_bill_version_fields._temp_bill.
"""
import datetime

from api.db.models import BillVersionDocument

from .conftest import TestingSessionLocal
from .test_bill_version_fields import _temp_bill

LEDGER = "/ddp/embedding/ledger"
_STAMP = datetime.datetime(2026, 8, 14, 9, 21, 7, tzinfo=datetime.timezone.utc)


def _ledger_all(client, jurisdiction="oh", limit=500):
    """Every page of a jurisdiction's ledger, as {bare bill id: LedgerBill}."""
    out, after = {}, None
    while True:
        params = {"jurisdiction": jurisdiction, "limit": limit}
        if after:
            params["after"] = after
        page = client.get(LEDGER, params=params).json()
        out.update({b["ocd_bill_id"]: b for b in page["results"]})
        after = page["next_after"]
        if not after:
            return out


def _detail_docs(client, bare_id):
    versions = client.get(f"/bills/ocd-bill/{bare_id}?include=versions").json()[
        "versions"
    ]
    return {
        v["archived_document_id"]: v
        for v in versions
        if v.get("archived_document_id") is not None
    }


def _stamp(bill_id):
    """Give a temp bill's archived rows a known updated_at (the fixtures leave it unset)."""
    db = TestingSessionLocal()
    try:
        db.query(BillVersionDocument).filter(
            BillVersionDocument.bill_id == bill_id
        ).update({"updated_at": _STAMP})
        db.commit()
    finally:
        db.close()


def test_the_ledger_lists_exactly_the_documents_the_bill_detail_returns_for_every_bill(
    client,
):
    ledger = _ledger_all(client)
    assert ledger  # the Ohio fixtures include archived bills
    for bare_id, bill in ledger.items():
        detail = _detail_docs(client, bare_id)
        assert {d["archived_document_id"] for d in bill["documents"]} == set(
            detail
        ), bare_id
        for d in bill["documents"]:
            assert d["updated_at"] == detail[d["archived_document_id"]].get(
                "archived_updated_at"
            ), bare_id  # absent when null


def test_a_bill_with_no_archived_document_is_not_in_the_ledger(client):
    with _temp_bill(
        "HB 9301", [("Introduced", [("https://x/none.pdf", "application/pdf", None)])]
    ) as bill_id:
        assert bill_id.removeprefix("ocd-bill/") not in _ledger_all(client)


def test_the_ledger_uses_the_pickers_choice_xml_over_pdf_then_lowest_id(client):
    shape = [
        (
            "Introduced",
            [
                ("https://x/a.pdf", "application/pdf", "pdf text"),
                ("https://x/a.xml", "text/xml", "xml text"),
            ],
        ),
        (
            "Engrossed",
            [
                ("https://x/b1.pdf", "application/pdf", "first"),
                ("https://x/b2.pdf", "application/pdf", "second"),
            ],
        ),
    ]
    with _temp_bill("HB 9302", shape) as bill_id:
        _stamp(bill_id)
        bare = bill_id.removeprefix("ocd-bill/")
        docs = _ledger_all(client)[bare]["documents"]
        detail = _detail_docs(client, bare)
        assert {d["archived_document_id"] for d in docs} == set(detail) and len(
            docs
        ) == 2
        db = TestingSessionLocal()
        try:
            rows = {
                r.source_url: r.id
                for r in db.query(BillVersionDocument).filter(
                    BillVersionDocument.bill_id == bill_id
                )
            }
        finally:
            db.close()
        assert (
            rows["https://x/a.xml"] in detail and rows["https://x/a.pdf"] not in detail
        )  # XML won
        assert (
            rows["https://x/b1.pdf"] in detail
            and rows["https://x/b2.pdf"] not in detail
        )  # lowest id won


def test_the_ledger_pages_by_bill_and_the_pages_add_up_to_the_unpaged_ledger(client):
    unpaged = _ledger_all(client, limit=500)
    paged = _ledger_all(
        client, limit=1
    )  # one bill per page: the cursor must walk every bill
    assert paged == unpaged
    first = client.get(LEDGER, params={"jurisdiction": "oh", "limit": 1}).json()
    assert first["next_after"] is not None and len(first["results"]) <= 1


def test_the_ledger_rejects_an_unknown_jurisdiction_and_a_bad_limit(client):
    assert client.get(LEDGER, params={"jurisdiction": "zz"}).status_code == 400
    assert (
        client.get(LEDGER, params={"jurisdiction": "oh", "limit": 0}).status_code == 422
    )
    assert (
        client.get(LEDGER, params={"jurisdiction": "oh", "limit": 501}).status_code
        == 422
    )
    assert client.get(LEDGER).status_code == 422


def test_the_detail_carries_archived_updated_at_next_to_the_document_id(client):
    shape = [
        ("Introduced", [("https://x/c.xml", "text/xml", "t")]),
        ("Mystery note", [("https://x/d.xml", "text/xml", "u")]),
    ]
    with _temp_bill("HB 9303", shape) as bill_id:
        _stamp(bill_id)
        versions = client.get(f"/bills/{bill_id}?include=versions").json()["versions"]
    assert len(versions) == 2 and all(
        v.get("archived_document_id") is not None for v in versions
    )
    # the classifiable and the stage-unknown version both carry it, as the row's own timestamp
    assert all(
        v["archived_updated_at"].startswith("2026-08-14T09:21:07") for v in versions
    )


def test_a_version_with_no_archived_document_has_no_archived_updated_at(client):
    with _temp_bill(
        "HB 9304", [("Introduced", [("https://x/e.pdf", "application/pdf", None)])]
    ) as bill_id:
        versions = client.get(f"/bills/{bill_id}?include=versions").json()["versions"]
    assert (
        versions[0].get("archived_document_id") is None
        and versions[0].get("archived_updated_at") is None
    )


def test_the_ledger_route_requires_the_api_key():
    from api.auth import apikey_auth
    from api.main import app

    route = next(r for r in app.routes if getattr(r, "path", "") == LEDGER)
    assert any(d.call is apikey_auth for d in route.dependant.dependencies)


def _bill_count(jurisdiction="oh"):
    from openstates.metadata import lookup

    from api.db.models import Bill, LegislativeSession

    db = TestingSessionLocal()
    try:
        return (
            db.query(Bill)
            .join(
                LegislativeSession, Bill.legislative_session_id == LegislativeSession.id
            )
            .filter(
                LegislativeSession.jurisdiction_id
                == lookup(abbr=jurisdiction).jurisdiction_id
            )
            .count()
        )
    finally:
        db.close()


def test_the_last_page_has_a_null_cursor_even_when_the_bill_count_is_a_multiple_of_the_limit(
    client,
):
    total = _bill_count()
    assert 1 < total <= 500
    exact = client.get(LEDGER, params={"jurisdiction": "oh", "limit": total}).json()
    assert (
        exact["next_after"] is None
    )  # every bill fit in one page: no empty last request needed
    first = client.get(LEDGER, params={"jurisdiction": "oh", "limit": total - 1}).json()
    assert first["next_after"] is not None  # one bill is left, so there is a next page
    last = client.get(
        LEDGER,
        params={"jurisdiction": "oh", "limit": total - 1, "after": first["next_after"]},
    ).json()
    assert last["next_after"] is None


def test_a_page_of_only_bills_without_archived_documents_is_empty_but_not_the_end(
    client,
):
    pages, after = [], None
    while True:
        params = {"jurisdiction": "oh", "limit": 1}
        if after:
            params["after"] = after
        page = client.get(LEDGER, params=params).json()
        pages.append(page)
        after = page["next_after"]
        if not after:
            break
    assert any(
        not p["results"] and p["next_after"] for p in pages
    )  # a docless bill: short page, more to come


def test_after_is_an_opaque_position_and_a_malformed_one_is_not_an_error(client):
    for odd in ("not-an-id", "%%%", "ocd-bill/", "zzzz" * 40):
        assert (
            client.get(LEDGER, params={"jurisdiction": "oh", "after": odd}).status_code
            == 200
        )


def test_a_stage_unknown_version_with_several_eligible_rows_matches_the_detail(client):
    shape = [
        (
            "Mystery note",  # no known stage: the detail's separate stage-unknown branch
            [
                ("https://x/m.pdf", "application/pdf", "pdf"),
                ("https://x/m.xml", "text/xml", "xml"),
                ("https://x/m2.xml", "text/xml", "xml again"),
            ],
        )
    ]
    with _temp_bill("HB 9305", shape) as bill_id:
        _stamp(bill_id)
        bare = bill_id.removeprefix("ocd-bill/")
        detail = _detail_docs(client, bare)
        ledger = {
            d["archived_document_id"]: d["updated_at"]
            for d in _ledger_all(client)[bare]["documents"]
        }
        assert set(ledger) == set(detail) and len(ledger) == 1
        (doc_id,) = detail
        assert ledger[doc_id] == detail[doc_id]["archived_updated_at"]
        db = TestingSessionLocal()
        try:
            chosen = db.query(BillVersionDocument).get(doc_id)
            assert (
                chosen.media_type == "text/xml"
                and chosen.source_url == "https://x/m.xml"
            )  # XML, then lowest id
        finally:
            db.close()


def test_two_versions_that_resolve_to_one_row_are_listed_once_and_the_detail_names_it_twice(
    client,
):
    shape = [
        ("Introduced", [("https://x/s.xml", "text/xml", "same")]),
        ("Introduced", [("https://x/s.xml", "text/xml", "same")]),
    ]
    with _temp_bill("HB 9306", shape) as bill_id:
        _stamp(bill_id)
        bare = bill_id.removeprefix("ocd-bill/")
        versions = client.get(f"/bills/{bill_id}?include=versions").json()["versions"]
        ids = [v["archived_document_id"] for v in versions]
        assert (
            len(ids) == 2 and ids[0] == ids[1]
        )  # both versions resolve to the lowest-id row
        docs = _ledger_all(client)[bare]["documents"]
        assert [d["archived_document_id"] for d in docs] == [ids[0]]  # listed once


def test_a_row_with_no_updated_at_is_null_in_the_ledger_and_absent_from_the_detail(
    client,
):
    with _temp_bill(
        "HB 9307", [("Introduced", [("https://x/n.xml", "text/xml", "t")])]
    ) as bill_id:
        bare = bill_id.removeprefix(
            "ocd-bill/"
        )  # not stamped: the fixtures leave updated_at unset
        (doc,) = _ledger_all(client)[bare]["documents"]
        (version,) = client.get(f"/bills/{bill_id}?include=versions").json()["versions"]
    assert doc["updated_at"] is None and "archived_updated_at" not in version
    assert version["archived_document_id"] == doc["archived_document_id"]


def test_without_the_conftest_override_the_ledger_refuses_a_request_with_no_key_like_the_search_route():
    from fastapi.testclient import TestClient

    from api.auth import apikey_auth
    from api.main import app

    override = app.dependency_overrides.pop(apikey_auth)
    try:
        bare = TestClient(app)
        ledger = bare.get(LEDGER, params={"jurisdiction": "oh"})
        search = bare.get("/ddp/search", params={"q": "hb", "jurisdiction": "oh"})
    finally:
        app.dependency_overrides[apikey_auth] = override
    assert ledger.status_code == search.status_code == 403
