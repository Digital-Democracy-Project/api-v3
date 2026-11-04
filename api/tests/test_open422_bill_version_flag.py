"""OPEN-422 / OPEN-492: api-v3 reads the archiver's saved answer "is this document a real bill
version?" (ddp_bill_version_document.is_bill_version) and treats False like an unknown-stage
version: returned and labelled, never "latest" or "previous", no comparison or link text.
NULL (not decided yet) and a database without the column behave exactly as before.

Bills are created per test by test_bill_version_fields._temp_bill and removed again.
"""

from sqlalchemy import text

from api.bill_version_flag import forget_check
from api.db.models import BillVersionDocument

from .conftest import TestingSessionLocal, engine
from .test_bill_version_fields import _get, _temp_bill

INTRODUCED = "Introduced"
AMENDMENT_LIST = "General Government and Capital Outlay Subcommittee Amendment"
SUBSTITUTE = "Courts of Justice Substitute"
BODY = "AN ACT to amend the Code of Virginia relating to the budget. " * 5


def _versions(*notes):
    return [
        (note, [(f"https://example.com/{i}.pdf", "application/pdf", BODY + note)])
        for i, note in enumerate(notes)
    ]


def _flag(bill_id, note, value, media_type=None):
    db = TestingSessionLocal()
    try:
        query = db.query(BillVersionDocument).filter(
            BillVersionDocument.bill_id == bill_id,
            BillVersionDocument.version_note == note,
        )
        if media_type:
            query = query.filter(BillVersionDocument.media_type == media_type)
        query.update({BillVersionDocument.is_bill_version: value})
        db.commit()
    finally:
        db.close()


def test_a_flagged_amendment_list_is_never_latest_and_keeps_no_link_text(client):
    with _temp_bill("HB 9422", _versions(INTRODUCED, AMENDMENT_LIST)) as bill_id:
        _flag(bill_id, AMENDMENT_LIST, False)
        versions = _get(client, bill_id)
        # Unplaced versions come first, the lineage last: versions[-1] is the latest.
        assert [v["note"] for v in versions] == [AMENDMENT_LIST, INTRODUCED]
        amendment, introduced = versions
        assert amendment["version_stage"] == "unknown"
        assert "version_ordinal" not in amendment
        assert amendment["archived_raw_text"].endswith(AMENDMENT_LIST)
        assert amendment["archived_document_id"] is not None
        assert all("raw_text" not in link for link in amendment["links"])
        assert "diff_from_previous_version" not in amendment
        assert introduced["version_stage"] == "introduced"
        assert introduced["version_ordinal"] == 0
        assert introduced["links"][0]["raw_text"].endswith(INTRODUCED)


def test_not_decided_yet_behaves_as_before(client):
    with _temp_bill("HB 9423", _versions(INTRODUCED, AMENDMENT_LIST)) as bill_id:
        versions = _get(client, bill_id)
        assert [v["note"] for v in versions] == [INTRODUCED, AMENDMENT_LIST]
        assert [v["version_stage"] for v in versions] == ["introduced", "amendment"]


def test_flagged_true_stays_eligible(client):
    with _temp_bill("HB 9424", _versions(INTRODUCED, SUBSTITUTE)) as bill_id:
        _flag(bill_id, SUBSTITUTE, True)
        versions = _get(client, bill_id)
        assert [v["note"] for v in versions] == [INTRODUCED, SUBSTITUTE]
        assert versions[-1]["version_stage"] == "amendment"
        assert versions[-1]["links"][0]["raw_text"].endswith(SUBSTITUTE)


def test_only_the_flagged_note_drops_out(client):
    notes = (INTRODUCED, AMENDMENT_LIST, SUBSTITUTE)
    with _temp_bill("HB 9425", _versions(*notes)) as bill_id:
        _flag(bill_id, AMENDMENT_LIST, False)
        versions = _get(client, bill_id)
        assert versions[0]["note"] == AMENDMENT_LIST
        assert {v["note"] for v in versions[1:]} == {INTRODUCED, SUBSTITUTE}
        assert versions[-1]["note"] == SUBSTITUTE


def test_a_database_without_the_column_answers_as_before(client):
    with _temp_bill("HB 9426", _versions(INTRODUCED, AMENDMENT_LIST)) as bill_id:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE ddp_bill_version_document DROP COLUMN is_bill_version"
                )
            )
        forget_check()
        try:
            versions = _get(client, bill_id)
            assert [v["note"] for v in versions] == [INTRODUCED, AMENDMENT_LIST]
            assert versions[-1]["links"][0]["raw_text"].endswith(AMENDMENT_LIST)
        finally:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE ddp_bill_version_document "
                        "ADD COLUMN is_bill_version boolean"
                    )
                )
            forget_check()


def test_one_flagged_format_makes_the_whole_version_not_a_version(client):
    """A version's saved formats normally carry one answer (it comes from the note); while a
    recompute is part-way they can differ, and any False wins, the same rule the search uses."""
    versions = [
        (INTRODUCED, [("https://example.com/i.pdf", "application/pdf", BODY)]),
        (
            AMENDMENT_LIST,
            [
                ("https://example.com/a.pdf", "application/pdf", BODY + "pdf"),
                ("https://example.com/a.html", "text/html", BODY + "html"),
            ],
        ),
    ]
    with _temp_bill("HB 9427", versions) as bill_id:
        _flag(bill_id, AMENDMENT_LIST, True, "application/pdf")
        _flag(bill_id, AMENDMENT_LIST, False, "text/html")
        result = _get(client, bill_id)
        assert [v["note"] for v in result] == [AMENDMENT_LIST, INTRODUCED]
        assert result[0]["version_stage"] == "unknown"
