"""OPEN-641: each related bill in a bill's answer carries the legislature's own word for the link (`label`, from
OPEN-640's ddp_relation_label) and the linked bill as resolved in the database (`related_bill_id`). Both are left out
of the answer when unknown (the API's usual exclude-none), never an empty string. A database without the column
(before core migration data.0052) answers as before, with no `label`.

Bills are created per test by test_bill_version_fields._temp_bill and removed again.
"""

from contextlib import contextmanager

from sqlalchemy import text

from api.bill_version_flag import forget_check
from api.db.models import Bill, RelatedBill

from .conftest import TestingSessionLocal, engine
from .test_bill_version_fields import _temp_bill


@contextmanager
def _links(bill_id, links):
    """Add related-bill rows (identifier, relation_type, label, resolved) to the bill; removed again on exit."""
    db = TestingSessionLocal()
    resolved_id = db.query(Bill.id).filter(Bill.identifier == "HB 9106").scalar()
    rows = [
        RelatedBill(
            bill_id=bill_id,
            identifier=identifier,
            legislative_session="2021",
            relation_type=relation_type,
            ddp_relation_label=label,
            related_bill_id=resolved_id if resolved else None,
        )
        for identifier, relation_type, label, resolved in links
    ]
    db.add_all(rows)
    db.commit()
    try:
        yield resolved_id
    finally:
        for row in rows:
            db.delete(row)
        db.commit()
        db.close()


def _related(client, bill_id):
    response = client.get(f"/bills/{bill_id}?include=related_bills")
    assert response.status_code == 200
    return {r["identifier"]: r for r in response.json()["related_bills"]}


def test_label_and_resolved_bill_are_served(client):
    with _temp_bill("HB 9641", []) as bill_id:
        links = [
            ("SB 1278", "companion", "Identical bill", True),  # Arizona's "Identical to SB 1278"
            ("HB 2125", "related", None, False),  # a link with no word stored
            ("HB 2141", "related", "", False),  # an empty word is no word
        ]
        forget_check()
        with _links(bill_id, links) as resolved_id:
            related = _related(client, bill_id)
            assert related["SB 1278"]["label"] == "Identical bill"
            assert related["SB 1278"]["related_bill_id"] == resolved_id
            assert related["SB 1278"]["relation_type"] == "companion"
            assert "label" not in related["HB 2125"]
            assert "related_bill_id" not in related["HB 2125"]
            assert "label" not in related["HB 2141"]


def test_a_database_without_the_column_answers_as_before(client):
    with _temp_bill("HB 9642", []) as bill_id:
        with _links(bill_id, [("SB 1278", "companion", "Identical bill", True)]) as resolved_id:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE opencivicdata_relatedbill DROP COLUMN ddp_relation_label"))
            forget_check()
            try:
                related = _related(client, bill_id)
                assert "label" not in related["SB 1278"]
                assert related["SB 1278"]["related_bill_id"] == resolved_id
                assert related["SB 1278"]["relation_type"] == "companion"
            finally:
                with engine.begin() as conn:
                    conn.execute(
                        text("ALTER TABLE opencivicdata_relatedbill ADD COLUMN ddp_relation_label varchar(100)")
                    )
                forget_check()
