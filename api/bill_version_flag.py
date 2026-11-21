"""OPEN-422 / OPEN-492: the archiver's saved answer to "is this document a real bill version?".

openstates-core decides once, per document, from its per-state rules, and stores the answer in
ddp_bill_version_document.is_bill_version (True: a real version; False: a procedural paper such
as a committee amendment list; NULL: not decided yet). api-v3 holds no list of its own: it only
reads the answer, and treats False like a version whose stage is unknown (STAGE_UNKNOWN): still
returned and labelled, never "latest" or "previous", no comparison or link text attached.

The column is added by a core migration. A database that does not have it yet (the Mac's
logical replica, until the column is added there too) must keep answering exactly as before, so
callers ask flag_column_present() first and read the column only when it is there.

OPEN-540: diff_baseline_id (OPEN-539, the archived document a stored comparison was made against)
arrives the same way, so baseline_column_present() asks the same question about it. OPEN-641: so does
opencivicdata_relatedbill.ddp_relation_label (OPEN-640), asked by relation_label_column_present().
"""

import time

from sqlalchemy import text

_CHECK_SQL = text(
    "SELECT 1 FROM information_schema.columns "
    "WHERE table_name = :table "
    "AND column_name = :column AND table_schema = current_schema()"
)

# Re-checked every few minutes, so a column added while api-v3 runs is picked up without a restart.
_RECHECK_SECONDS = 300
_cache = {}


def _column_present(bind, column: str, table: str = "ddp_bill_version_document") -> bool:
    now = time.monotonic()
    checked_at, present = _cache.get((table, column), (None, False))
    if checked_at is None or now - checked_at > _RECHECK_SECONDS:
        present = bind.execute(_CHECK_SQL, {"table": table, "column": column}).first() is not None
        _cache[(table, column)] = (now, present)
    return present


def flag_column_present(bind) -> bool:
    """True when the database has is_bill_version. `bind` is a Session or a Connection."""
    return _column_present(bind, "is_bill_version")


def baseline_column_present(bind) -> bool:
    """True when the database has diff_baseline_id (OPEN-539). `bind` is a Session or a Connection."""
    return _column_present(bind, "diff_baseline_id")


def relation_label_column_present(bind) -> bool:
    """True when the database has opencivicdata_relatedbill.ddp_relation_label (OPEN-640, core migration data.0052):
    the legislature's own word for a bill link (OPEN-641). `bind` is a Session or a Connection."""
    return _column_present(bind, "ddp_relation_label", table="opencivicdata_relatedbill")


def forget_check() -> None:
    """Make the next *_column_present() ask the database again (tests)."""
    _cache.clear()
