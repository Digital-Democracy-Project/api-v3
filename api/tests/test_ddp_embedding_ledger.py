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
