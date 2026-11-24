"""Which bill labels (`opencivicdata_bill.classification`) are real bill types (OPEN-519, NEXT-182).

Ramon (2026-11-10, "only show the bill type"): the website lists and counts an item as a bill only when its label is a
bill type. Massachusetts files reports, communications, orders and study orders under bill numbers; OPEN-517 and
OPEN-518 label them as what they are, and Louisiana files two House study requests. They stay in the data and open by
direct link, but a caller that asks for `bill_types_only` (`GET /bills`, `/ddp/search`, `/ddp/search/suggest`,
`/ddp/search/hydrate`) does not get them. A caller that does not ask gets exactly what it got before.

This is the ONE list. Do not copy it into the broker or the website: they pass `bill_types_only`.

Rule: an item is a bill type when any of its labels is in BILL_TYPES, or it has no label at all (fail open, so a
scraper that leaves the label blank never hides a bill). NOT_BILL_TYPES is the labels deliberately left out. A label
on neither list is still left out by the rule, so `python -m api.bill_types check` (and its test) fails when the data
holds one, and someone decides which list it belongs on before it is hidden by accident.

OPEN-861 (Ramon, 2026-11-24: hide the old draft record, store it, never delete it): `bill_types_only` also leaves out a
bill draft's own record once the draft is introduced as a bill. Montana's pre-filing scrape stores each draft
("LC nnnn") as a bill; the introduced bill links to it, and the importer never deletes the draft's record. The link
comes from the data: a state's core rules name the relation type its introduced bills use for their draft
(`INTRODUCED_DRAFT_RELATION` in openstates-core `ddp_states/<state>.py`), and a record that a bill of its own
jurisdiction links to with that type is left out. No state is named here; a core without the value hides nothing.
"""
import sys
from typing import List

from openstates.ddp_states import state_values
from sqlalchemy import exists, func, or_, text

BILL_TYPES = (
    "bill",
    "proposed bill",  # Massachusetts files real measures as proposed bills too (339 of its 2,896, 2026-11-10)
    "resolution",
    "joint resolution",
    "concurrent resolution",
    "memorial",
    "joint memorial",
    "concurrent memorial",
    "constitutional amendment",
)

NOT_BILL_TYPES = (
    "report",  # OPEN-517/518: Massachusetts agency reports, plans, budgets, filings
    "communication",  # OPEN-517/518: letters and messages, the Governor's included
    "order",  # OPEN-517/518: chamber and committee orders, precepts, rules orders
    "study request",  # OPEN-517/518 study orders; Louisiana's HSR 1 and HSR 2 (would not change the law if passed)
    "amendment",  # OPEN-509: Massachusetts amendment papers, listed under their real bill instead
)

# For raw SQL over opencivicdata_bill aliased `b`. `::text[]` because the real column is varchar[] and the test
# schema's is text[]; `&&` needs both sides the same type. Bind :bill_types to list(BILL_TYPES).
SQL_IS_BILL_TYPE = (
    "(b.classification::text[] && CAST(:bill_types AS text[])"
    " OR coalesce(cardinality(b.classification), 0) = 0)"
)

UNKNOWN_LABELS_SQL = text(
    """
    SELECT DISTINCT label FROM opencivicdata_bill b, unnest(b.classification) AS label
    WHERE NOT (label = ANY(CAST(:known AS text[])))
    ORDER BY label
"""
)


def is_bill_type_clause(classification_column):
    """The rule for a SQLAlchemy query on `models.Bill.classification`."""
    return or_(
        classification_column.overlap(list(BILL_TYPES)),
        func.coalesce(func.cardinality(classification_column), 0) == 0,
    )


def introduced_draft_links() -> List[str]:
    """OPEN-861: "<jurisdiction id>#<relation type>" for each state whose core rules name the relation by which an
    introduced bill links to its draft's record."""
    return [
        f"ocd-jurisdiction/country:us/state:{state}/government#{relation}"
        for state, relation in sorted(state_values("INTRODUCED_DRAFT_RELATION").items())
    ]


INTRODUCED_DRAFT_LINKS = introduced_draft_links()

# For raw SQL over opencivicdata_bill aliased `b`: not a draft's record that a bill has since been introduced from. The
# link is resolved by the importer only within the bill's own jurisdiction and session. Bind :draft_links to
# INTRODUCED_DRAFT_LINKS.
SQL_IS_NOT_INTRODUCED_DRAFT = (
    "NOT EXISTS (SELECT 1 FROM opencivicdata_relatedbill r"
    " JOIN opencivicdata_legislativesession ls ON ls.id = b.legislative_session_id"
    " WHERE r.related_bill_id = b.id"
    " AND ls.jurisdiction_id || '#' || r.relation_type = ANY(CAST(:draft_links AS text[])))"
)


def is_not_introduced_draft_clause(models):
    """The OPEN-861 rule for a SQLAlchemy query on `models.Bill` joined to `models.LegislativeSession`."""
    return ~exists().where(
        models.RelatedBill.related_bill_id == models.Bill.id,
        (
            models.LegislativeSession.jurisdiction_id
            + "#"
            + models.RelatedBill.relation_type
        ).in_(INTRODUCED_DRAFT_LINKS),
    )


def unknown_labels(db) -> List[str]:
    """Labels in the data that are on neither list. `db` is a Session or a Connection."""
    known = list(BILL_TYPES) + list(NOT_BILL_TYPES)
    return [row[0] for row in db.execute(UNKNOWN_LABELS_SQL, {"known": known})]


def main(argv=None) -> int:
    """`python -m api.bill_types check`: exit 1 and name every label on neither list (read-only)."""
    args = sys.argv[1:] if argv is None else argv
    if args != ["check"]:
        print("usage: python -m api.bill_types check", file=sys.stderr)
        return 2
    from .db import engine

    with engine.connect() as conn:
        unknown = unknown_labels(conn)
    if unknown:
        print(
            "labels on neither list in api/bill_types.py (decide which list each belongs on): "
            + ", ".join(repr(label) for label in unknown)
        )
        return 1
    print("every bill label is on a list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
