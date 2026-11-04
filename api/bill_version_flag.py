"""OPEN-422 / OPEN-492: the archiver's saved answer to "is this document a real bill version?".

openstates-core decides once, per document, from its per-state rules, and stores the answer in
ddp_bill_version_document.is_bill_version (True: a real version; False: a procedural paper such
as a committee amendment list; NULL: not decided yet). api-v3 holds no list of its own: it only
reads the answer, and treats False like a version whose stage is unknown (STAGE_UNKNOWN): still
returned and labelled, never "latest" or "previous", no comparison or link text attached.

The column is added by a core migration. A database that does not have it yet (the Mac's
logical replica, until the column is added there too) must keep answering exactly as before, so
callers ask flag_column_present() first and read the column only when it is there.
"""

import time

from sqlalchemy import text

_CHECK_SQL = text(
    "SELECT 1 FROM information_schema.columns "
    "WHERE table_name = 'ddp_bill_version_document' "
    "AND column_name = 'is_bill_version' AND table_schema = current_schema()"
)

# Re-checked every few minutes, so a column added while api-v3 runs is picked up without a restart.
_RECHECK_SECONDS = 300
_cache = {"checked_at": None, "present": False}


def flag_column_present(bind) -> bool:
    """True when the database has is_bill_version. `bind` is a Session or a Connection."""
    now = time.monotonic()
    checked_at = _cache["checked_at"]
    if checked_at is None or now - checked_at > _RECHECK_SECONDS:
        _cache["present"] = bind.execute(_CHECK_SQL).first() is not None
        _cache["checked_at"] = now
    return _cache["present"]


def forget_check() -> None:
    """Make the next flag_column_present() ask the database again (tests)."""
    _cache["checked_at"] = None
