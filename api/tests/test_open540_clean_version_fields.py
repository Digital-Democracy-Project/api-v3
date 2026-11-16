"""OPEN-540 (PLAN-state-specific-handling §4.4, C5): the clean-data fields on bill versions,
added beside the existing ones: current_document_id, is_bill_version, is_full_text,
diff_baseline_id and version_date (version_stage already existed). Single-bill detail with
include=versions only. Bills are created per test by test_bill_version_fields._temp_bill.
"""

from sqlalchemy import text

from api.bill_version_flag import forget_check
from api.db.models import BillVersion, BillVersionDocument

from .conftest import TestingSessionLocal, engine
from .test_bill_version_fields import _get, _temp_bill

BODY = "AN ACT relating to the state budget. " * 5
INTRODUCED = "Introduced"
ENGROSSED = "Engrossed"
UNKNOWN_NOTE = "Fiscal Note"  # note_stage() places no "Fiscal Note": stage unknown


def _store(bill_id, note, **values):
    db = TestingSessionLocal()
    try:
        db.query(BillVersionDocument).filter(
            BillVersionDocument.bill_id == bill_id,
            BillVersionDocument.version_note == note,
        ).update({getattr(BillVersionDocument, k): v for k, v in values.items()})
        db.commit()
    finally:
        db.close()


def _doc(bill_id, note):
    db = TestingSessionLocal()
    try:
        return (
            db.query(BillVersionDocument.id)
            .filter(
                BillVersionDocument.bill_id == bill_id,
                BillVersionDocument.version_note == note,
            )
            .scalar()
        )
    finally:
        db.close()


def _by_note(versions):
    return {v["note"]: v for v in versions}


def test_fields_for_placed_unplaced_and_textless_versions(client):
    versions = [
        (INTRODUCED, [("https://example.com/i.pdf", "application/pdf", BODY + "i")]),
        (ENGROSSED, [("https://example.com/e.pdf", "application/pdf", BODY + "e")]),
        (UNKNOWN_NOTE, [("https://example.com/f.pdf", "application/pdf", BODY + "f")]),
        ("Enrolled", [("https://example.com/n.pdf", "application/pdf", None)]),
    ]
    with _temp_bill("HB 9540", versions) as bill_id:
        introduced_doc = _doc(bill_id, INTRODUCED)
        _store(bill_id, INTRODUCED, is_bill_version=True)
        _store(
            bill_id,
            ENGROSSED,
            is_bill_version=True,
            diff_from_previous_version="--- a\n+++ b\n",
            diff_baseline_id=introduced_doc,
        )
        v = _by_note(_get(client, bill_id))

        # current_document_id: the same archived row as archived_document_id, for every version.
        for note in (INTRODUCED, ENGROSSED, UNKNOWN_NOTE):
            assert v[note]["current_document_id"] == v[note]["archived_document_id"]
            assert v[note]["current_document_id"] == _doc(bill_id, note)
        assert v["Enrolled"].get("current_document_id") is None

        # is_full_text: a placed version with extracted text only.
        assert v[INTRODUCED]["is_full_text"] is True
        assert v[ENGROSSED]["is_full_text"] is True
        assert v[UNKNOWN_NOTE]["is_full_text"] is False
        assert v["Enrolled"]["is_full_text"] is False

        # is_bill_version and diff_baseline_id: read as stored, never computed here.
        assert v[INTRODUCED]["is_bill_version"] is True
        assert v[UNKNOWN_NOTE].get("is_bill_version") is None  # NULL: not decided
        assert v[ENGROSSED]["diff_baseline_id"] == introduced_doc
        assert v[INTRODUCED].get("diff_baseline_id") is None

        # version_date: these versions carry no date, so null, never guessed.
        assert all(x.get("version_date") is None for x in v.values())
        # The existing fields are unchanged.
        assert v[ENGROSSED]["version_stage"] == "chamber_passage"
        assert v[UNKNOWN_NOTE]["version_stage"] == "unknown"


def test_a_published_date_is_returned_as_given(client):
    versions = [
        (INTRODUCED, [("https://example.com/i.pdf", "application/pdf", BODY)]),
    ]
    with _temp_bill("HB 9541", versions) as bill_id:
        db = TestingSessionLocal()
        try:
            db.query(BillVersion).filter(BillVersion.bill_id == bill_id).update(
                {BillVersion.date: "2026-03-17"}
            )
            db.query(BillVersionDocument).filter(
                BillVersionDocument.bill_id == bill_id
            ).update({BillVersionDocument.version_date: "2026-03-17"})
            db.commit()
        finally:
            db.close()
        (introduced,) = _get(client, bill_id)
        assert introduced["version_date"] == "2026-03-17"
        assert introduced["date"] == "2026-03-17"


def test_a_flagged_paper_is_not_full_text(client):
    versions = [
        (INTRODUCED, [("https://example.com/i.pdf", "application/pdf", BODY + "i")]),
        (
            "Committee Amendment 1",
            [("https://example.com/a.pdf", "application/pdf", BODY + "a")],
        ),
    ]
    with _temp_bill("HB 9542", versions) as bill_id:
        _store(bill_id, "Committee Amendment 1", is_bill_version=False)
        v = _by_note(_get(client, bill_id))
        assert v["Committee Amendment 1"]["is_bill_version"] is False
        assert v["Committee Amendment 1"]["is_full_text"] is False
        assert v[INTRODUCED]["is_full_text"] is True


def test_a_database_without_the_baseline_column_answers_as_before(client):
    versions = [
        (INTRODUCED, [("https://example.com/i.pdf", "application/pdf", BODY + "i")]),
        (ENGROSSED, [("https://example.com/e.pdf", "application/pdf", BODY + "e")]),
    ]
    with _temp_bill("HB 9543", versions) as bill_id:
        _store(bill_id, ENGROSSED, is_bill_version=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE ddp_bill_version_document DROP COLUMN diff_baseline_id"
                )
            )
        forget_check()
        try:
            v = _by_note(_get(client, bill_id))
            assert v[ENGROSSED].get("diff_baseline_id") is None
            assert v[ENGROSSED]["is_bill_version"] is True
            assert v[ENGROSSED]["is_full_text"] is True
            assert [x["note"] for x in _get(client, bill_id)] == [INTRODUCED, ENGROSSED]
        finally:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE ddp_bill_version_document "
                        "ADD COLUMN diff_baseline_id integer"
                    )
                )
            forget_check()


def test_a_version_with_only_blank_text_is_not_full_text(client):
    """is_full_text needs extracted text: the document choice (_archived_row_for) skips a
    whitespace-only document (OPEN-365), so the version has no current document either."""
    versions = [
        (INTRODUCED, [("https://example.com/i.pdf", "application/pdf", BODY)]),
        (ENGROSSED, [("https://example.com/e.pdf", "application/pdf", "   \n  ")]),
    ]
    with _temp_bill("HB 9544", versions) as bill_id:
        v = _by_note(_get(client, bill_id))
        assert v[ENGROSSED]["version_stage"] == "chamber_passage"
        assert v[ENGROSSED]["is_full_text"] is False
        assert (
            "current_document_id" not in v[ENGROSSED]
        )  # absent means null (exclude_none)
        assert v[INTRODUCED]["is_full_text"] is True


def test_an_unplaced_version_the_archiver_calls_real_is_full_text(client):
    """Ramon, 2026-11-16: a real bill version whose name core cannot place (North Carolina's
    'A1: ACI-6-V-10', Arizona's 'Transmitted to Secretary of State') is full text when the
    archiver stored it as a bill version. It still never becomes latest. An undecided (NULL)
    unplaced version is not counted."""
    versions = [
        (INTRODUCED, [("https://example.com/i.pdf", "application/pdf", BODY + "i")]),
        (UNKNOWN_NOTE, [("https://example.com/f.pdf", "application/pdf", BODY + "f")]),
        ("Transmitted", [("https://example.com/t.pdf", "application/pdf", BODY + "t")]),
    ]
    with _temp_bill("HB 9545", versions) as bill_id:
        _store(bill_id, UNKNOWN_NOTE, is_bill_version=True)
        listed = _get(client, bill_id)
        v = _by_note(listed)
        assert v[UNKNOWN_NOTE]["version_stage"] == "unknown"
        assert v[UNKNOWN_NOTE]["is_bill_version"] is True
        assert v[UNKNOWN_NOTE]["is_full_text"] is True
        assert v["Transmitted"]["version_stage"] == "unknown"
        assert v["Transmitted"]["is_full_text"] is False  # NULL: not decided
        assert listed[-1]["note"] == INTRODUCED  # placement still decides latest


def test_an_errored_flagged_document_does_not_unplace_its_version(client):
    """OPEN-422's flagged set ignores documents that failed (is_error): a version whose only
    flagged document errored keeps its place, and its other format is its text."""
    versions = [
        (INTRODUCED, [("https://example.com/i.pdf", "application/pdf", BODY + "i")]),
        (
            ENGROSSED,
            [
                ("https://example.com/e.pdf", "application/pdf", BODY + "pdf"),
                ("https://example.com/e.html", "text/html", BODY + "html"),
            ],
        ),
    ]
    with _temp_bill("HB 9546", versions) as bill_id:
        db = TestingSessionLocal()
        try:
            db.query(BillVersionDocument).filter(
                BillVersionDocument.bill_id == bill_id,
                BillVersionDocument.source_url == "https://example.com/e.pdf",
            ).update(
                {
                    BillVersionDocument.is_bill_version: False,
                    BillVersionDocument.is_error: True,
                }
            )
            db.commit()
        finally:
            db.close()
        listed = _get(client, bill_id)
        engrossed = _by_note(listed)[ENGROSSED]
        assert engrossed["version_stage"] == "chamber_passage"
        assert engrossed["is_full_text"] is True
        assert listed[-1]["note"] == ENGROSSED


def test_list_endpoint_carries_no_clean_fields(client):
    response = client.get("/bills?jurisdiction=oh&session=2021&include=versions")
    assert response.status_code == 200
    for bill in response.json()["results"]:
        for version in bill.get("versions", []):
            assert "current_document_id" not in version
            assert "is_full_text" not in version
