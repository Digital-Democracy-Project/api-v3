"""OPEN-311: archived_document_id, version_stage, version_ordinal and archived_raw_text on
bill versions (single-bill detail with include=versions only). The fixture bills HB 9101-9106
come from fixtures.create_test_bills_with_archived_versions; bills that need shapes those
don't cover are created per-test by _temp_bill and removed again, so no other test sees them."""
import datetime
import uuid
from contextlib import contextmanager

from api.db.models import Bill, BillVersion, BillVersionDocument, BillVersionLink

from .conftest import TestingSessionLocal

NEW_FIELDS = (
    "archived_document_id",
    "version_stage",
    "version_ordinal",
    "archived_raw_text",
)


def _doc_id(bill_identifier, source_url):
    db = TestingSessionLocal()
    try:
        row = (
            db.query(BillVersionDocument)
            .join(Bill, Bill.id == BillVersionDocument.bill_id)
            .filter(
                Bill.identifier == bill_identifier,
                BillVersionDocument.source_url == source_url,
            )
            .one()
        )
        return row.id
    finally:
        db.close()


@contextmanager
def _temp_bill(identifier, versions):
    """Create an Ohio 2021 bill. `versions` is a list of (note, [(url, media_type, raw_text)]);
    a link whose raw_text is None gets no archived document. Deleted again on exit."""
    db = TestingSessionLocal()
    template = db.query(Bill).filter(Bill.identifier == "HB 9106").one()
    bill_id = f"ocd-bill/{uuid.uuid4()}"
    try:
        bill = Bill(
            id=bill_id,
            identifier=identifier,
            title="Temp bill",
            legislative_session_id=template.legislative_session_id,
            from_organization_id=template.from_organization_id,
            subject=[],
            classification=["bill"],
            extras={},
            created_at=datetime.datetime.utcnow(),
            updated_at=datetime.datetime.utcnow(),
            latest_action_date="2026-01-01",
        )
        db.add(bill)
        for note, links in versions:
            version = BillVersion(bill=bill, note=note, date="", classification="")
            for url, media_type, raw_text in links:
                db.add(BillVersionLink(version=version, url=url, media_type=media_type))
                if raw_text is not None:
                    db.add(
                        BillVersionDocument(
                            bill=bill,
                            version_note=note,
                            version_date="",
                            source_url=url,
                            media_type=media_type,
                            raw_text=raw_text,
                            is_error=False,
                        )
                    )
        db.commit()
        yield bill_id
    finally:
        db.rollback()
        db.query(BillVersionDocument).filter(
            BillVersionDocument.bill_id == bill_id
        ).delete()
        db.query(BillVersionLink).filter(
            BillVersionLink.version_id.in_(
                db.query(BillVersion.id).filter(BillVersion.bill_id == bill_id)
            )
        ).delete(synchronize_session=False)
        db.query(BillVersion).filter(BillVersion.bill_id == bill_id).delete()
        db.query(Bill).filter(Bill.id == bill_id).delete()
        db.commit()
        db.close()


def _get(client, bill_id):
    response = client.get(f"/bills/{bill_id}?include=versions")
    assert response.status_code == 200
    return response.json()["versions"]


def test_multi_version_bill_ids_stages_and_ordinals(client):
    """HB 9105: fixture rows are inserted out of order, so the returned order is the
    classifier's; ids must be the archived rows' own, stages the note_stage labels, and
    ordinals 0..n-1 in the returned order."""
    versions = client.get("/bills/oh/2021/HB 9105?include=versions").json()["versions"]
    assert [v["note"] for v in versions] == [
        "Introduced",
        "Committee Substitute",
        "Enrolled",
    ]
    assert [v["version_stage"] for v in versions] == [
        "introduced",
        "amendment",
        "final_passage",
    ]
    assert [v["version_ordinal"] for v in versions] == [0, 1, 2]
    expected = {
        "Introduced": "hb9105-introduced.pdf",
        "Committee Substitute": "hb9105-committee-substitute.pdf",
        "Enrolled": "hb9105-enrolled.pdf",
    }
    for version in versions:
        url = "https://example.com/" + expected[version["note"]]
        assert version["archived_document_id"] == _doc_id("HB 9105", url)
        # Classifiable versions never carry the unknown-stage text field.
        assert "archived_raw_text" not in version
    assert len({v["archived_document_id"] for v in versions}) == 3


def test_every_stage_label(client):
    with _temp_bill(
        "HB 9201",
        [
            (
                "Public Act 12",
                [("https://example.com/t1-act.pdf", "application/pdf", "act")],
            ),
            (
                "Enrolled",
                [("https://example.com/t1-enr.pdf", "application/pdf", "enr")],
            ),
            (
                "Engrossed",
                [("https://example.com/t1-eng.pdf", "application/pdf", "eng")],
            ),
            (
                "Committee Substitute",
                [("https://example.com/t1-cs.pdf", "application/pdf", "cs")],
            ),
            (
                "Introduced",
                [("https://example.com/t1-int.pdf", "application/pdf", "int")],
            ),
        ],
    ) as bill_id:
        versions = _get(client, bill_id)
    assert [
        (v["note"], v["version_stage"], v["version_ordinal"]) for v in versions
    ] == [
        ("Introduced", "introduced", 0),
        ("Committee Substitute", "amendment", 1),
        ("Engrossed", "chamber_passage", 2),
        ("Enrolled", "final_passage", 3),
        ("Public Act 12", "enacted", 4),
    ]


def test_same_stage_versions_are_ordered_by_date_and_numbered(client):
    """HB 9103 has Filed (2025-12-01) and Introduced (2026-01-01), both stage introduced,
    plus an unarchived Committee Substitute: ordinals follow the returned order and the
    unarchived version has a null id."""
    versions = client.get("/bills/oh/2021/HB 9103?include=versions").json()["versions"]
    assert [v["note"] for v in versions] == [
        "Filed",
        "Introduced",
        "Committee Substitute",
    ]
    assert [v["version_ordinal"] for v in versions] == [0, 1, 2]
    assert [v["version_stage"] for v in versions] == [
        "introduced",
        "introduced",
        "amendment",
    ]
    assert versions[0]["archived_document_id"] == _doc_id(
        "HB 9103", "https://example.com/hb9103-filed.pdf"
    )
    assert versions[1]["archived_document_id"] == _doc_id(
        "HB 9103", "https://example.com/hb9103-introduced.pdf"
    )
    assert "archived_document_id" not in versions[2]


def test_pdf_row_preferred_over_html_for_archived_document_id(client):
    versions = client.get("/bills/oh/2021/HB 9101?include=versions").json()["versions"]
    assert len(versions) == 1
    pdf_id = _doc_id("HB 9101", "https://example.com/hb9101.pdf")
    html_id = _doc_id("HB 9101", "https://example.com/hb9101.html")
    assert pdf_id != html_id
    assert versions[0]["archived_document_id"] == pdf_id
    assert versions[0]["version_stage"] == "introduced"
    assert versions[0]["version_ordinal"] == 0


def test_html_only_version_uses_the_html_row(client):
    with _temp_bill(
        "HB 9202",
        [("Introduced", [("https://example.com/t2.html", "text/html", "html text")])],
    ) as bill_id:
        versions = _get(client, bill_id)
        expected_id = _doc_id("HB 9202", "https://example.com/t2.html")
    assert versions[0]["archived_document_id"] == expected_id


def test_version_without_archived_row_has_no_id_but_keeps_stage(client):
    versions = client.get("/bills/oh/2021/HB 9102?include=versions").json()["versions"]
    assert len(versions) == 1
    assert "archived_document_id" not in versions[0]  # null, dropped by exclude_none
    assert versions[0]["version_stage"] == "introduced"
    assert versions[0]["version_ordinal"] == 0
    assert "archived_raw_text" not in versions[0]


def test_unknown_stage_version_exposes_id_and_text_but_no_link_text_or_diff(client):
    """HB 9106: the middle, unclassifiable version is first in the array (SYNC-16 reorder),
    has stage unknown, no ordinal, its own id and archived text -- and still no link-level
    raw_text or diff. The classifiable pair keeps ordinals 0 and 1 and the unchanged
    versions[-1]/[-2] behaviour."""
    versions = client.get("/bills/oh/2021/HB 9106?include=versions").json()["versions"]
    assert [v["note"] for v in versions] == [
        "Some Never-Before-Seen Document Type",
        "Introduced",
        "Enrolled",
    ]
    unknown, introduced, enrolled = versions

    assert unknown["version_stage"] == "unknown"
    assert "version_ordinal" not in unknown
    assert unknown["archived_document_id"] == _doc_id(
        "HB 9106", "https://example.com/hb9106-mystery.pdf"
    )
    assert unknown["archived_raw_text"] == "AN ACT relating to newts (mystery version)."
    assert "raw_text" not in unknown["links"][0]
    assert "diff_from_previous_version" not in unknown

    assert [introduced["version_ordinal"], enrolled["version_ordinal"]] == [0, 1]
    assert [introduced["version_stage"], enrolled["version_stage"]] == [
        "introduced",
        "final_passage",
    ]
    assert enrolled["archived_document_id"] == _doc_id(
        "HB 9106", "https://example.com/hb9106-enrolled.pdf"
    )
    # Lineage untouched: latest/previous are still the classifiable pair, and the diff skips
    # the unknown version.
    assert versions[-1]["note"] == "Enrolled"
    assert versions[-2]["note"] == "Introduced"
    assert "Some Never-Before-Seen" not in enrolled["diff_from_previous_version"]
    assert (
        enrolled["links"][0]["raw_text"]
        == "AN ACT relating to newts (enrolled version)."
    )


def test_unknown_stage_without_archived_row_has_stage_but_no_id_or_text(client):
    with _temp_bill(
        "HB 9203",
        [
            (
                "Introduced",
                [("https://example.com/t3-int.pdf", "application/pdf", "int")],
            ),
            (
                "Strange Thing",
                [("https://example.com/t3-x.pdf", "application/pdf", None)],
            ),
        ],
    ) as bill_id:
        versions = _get(client, bill_id)
    unknown = versions[0]
    assert unknown["note"] == "Strange Thing"
    assert unknown["version_stage"] == "unknown"
    for field in ("archived_document_id", "archived_raw_text", "version_ordinal"):
        assert field not in unknown
    assert versions[1]["version_ordinal"] == 0


def test_unknown_stage_prefers_pdf_text(client):
    with _temp_bill(
        "HB 9204",
        [
            (
                "Strange Thing",
                [
                    ("https://example.com/t4.html", "text/html", "html copy"),
                    ("https://example.com/t4.pdf", "application/pdf", "pdf copy"),
                ],
            ),
            (
                "Introduced",
                [("https://example.com/t4-int.pdf", "application/pdf", "int")],
            ),
        ],
    ) as bill_id:
        versions = _get(client, bill_id)
        expected_id = _doc_id("HB 9204", "https://example.com/t4.pdf")
    unknown = versions[0]
    assert unknown["archived_raw_text"] == "pdf copy"
    assert unknown["archived_document_id"] == expected_id
    assert all("raw_text" not in link for link in unknown["links"])


def test_bill_with_only_unknown_versions_still_gets_unknown_fields(client):
    """Review fix: the unknown-stage enrichment must not depend on a classifiable version
    existing. Link-level raw_text and diff stay absent."""
    with _temp_bill(
        "HB 9205",
        [
            (
                "Strange Thing",
                [("https://example.com/t5.pdf", "application/pdf", "text")],
            ),
            ("Odd Thing", [("https://example.com/t5b.pdf", "application/pdf", None)]),
        ],
    ) as bill_id:
        versions = _get(client, bill_id)
        expected_id = _doc_id("HB 9205", "https://example.com/t5.pdf")
    by_note = {v["note"]: v for v in versions}
    strange, odd = by_note["Strange Thing"], by_note["Odd Thing"]
    assert strange["version_stage"] == "unknown"
    assert strange["archived_document_id"] == expected_id
    assert strange["archived_raw_text"] == "text"
    assert "raw_text" not in strange["links"][0]
    assert "version_ordinal" not in strange
    assert odd["version_stage"] == "unknown"
    assert "archived_document_id" not in odd
    assert "archived_raw_text" not in odd


def test_list_endpoint_never_exposes_new_fields(client):
    for identifier in ("HB 9101", "HB 9105", "HB 9106"):
        results = client.get(
            f"/bills?jurisdiction=oh&session=2021&identifier={identifier}&include=versions"
        ).json()["results"]
        assert len(results) == 1
        for version in results[0]["versions"]:
            for field in NEW_FIELDS:
                assert field not in version


def test_detail_without_include_versions_has_no_versions_or_new_fields(client):
    body = client.get("/bills/oh/2021/HB 9105").json()
    assert "versions" not in body
    body = client.get("/bills/oh/2021/HB 9105?include=documents").json()
    assert "versions" not in body


def test_bill_without_archives_in_other_jurisdiction_is_unchanged(client):
    """Nebraska bills have versions but nothing archived: classifiable versions get a stage
    and ordinal, never an id or text."""
    ne_bill_id = client.get("/bills?jurisdiction=ne&session=2020").json()["results"][0][
        "id"
    ]
    for version in _get(client, ne_bill_id):
        assert "archived_document_id" not in version
        assert "archived_raw_text" not in version
        assert "diff_from_previous_version" not in version
