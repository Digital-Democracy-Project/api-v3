"""ddp_bill_search -- the enterprise-search bill projection (PLAN-enterprise-search.md §4.5).

A DDP-owned, derived table: one row per bill, rebuildable at any time from opencivicdata_bill,
opencivicdata_billabstract and ddp_bill_version_document. It exists because api-v3's own free-text
path (`/bills?q=`, backed by opencivicdata_searchablebill) is empty on DDP's databases and cannot
rank, tolerate typos or complete a prefix. Nothing here writes to an upstream table.

Created with plain SQL (CREATE ... IF NOT EXISTS) rather than an openstates-core Django migration:
migrations added to that fork risk number collisions with upstream's own (see ddp-open-states
PRIMITIVES.md, "Why not a database table"), and api-v3 already owns DDP-only side tables by the
same route (start-os-api.sh's bulk_dataexport ensure step).

Freshness invariant (PLAN-enterprise-search.md 4.5.3): a row is stale when its bill's or any of its
archived documents' `updated_at` is newer than the row's `source_updated_at`. Abstracts, subjects and
the originating chamber are only ever changed by openstates-core's importer, which also saves the
bill (advancing `Bill.updated_at`); any other writer must touch `opencivicdata_bill.updated_at`, or be
followed by `python -m api.search_projection refresh --full --jurisdiction <x>`.
"""
import time
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .version_ordering import STAGE_UNKNOWN, note_stage, version_sort_key

# Measured 2026-09-29: the largest archived document is 7.0M characters and still parses
# (253 KB of tsvector, far under Postgres' 1 MB limit). The cap bounds build time, not
# correctness: to_tsvector runs at roughly 4 MB/s on the Mac Studio.
TEXT_CAP_CHARS = 1_000_000

# One SQL statement never processes more than this many characters of bill text (~1 s at the
# measured 4 MB/s), however many bills are stale: 200 bills of federal omnibus text would otherwise
# be one statement of a minute or more, and the time budget below is only checked between statements.
MAX_CHARS_PER_STATEMENT = 4_000_000
# Hard ceiling per transaction: a 20 s soft budget plus one statement stays under the 60 s client timeout.
STATEMENT_TIMEOUT_MS = 30_000


def _lock_key(jurisdiction_id: str) -> str:
    """Advisory-lock key serialising refreshes of the SAME jurisdiction (a second caller gets `busy`
    instead of duplicating the work). Different jurisdictions hold different locks, so concurrent
    archive hooks never skip each other; an all-jurisdictions refresh takes each jurisdiction's lock
    in turn (see refresh_batch), so there is no separate global lock."""
    return "ddp_bill_search_refresh:" + jurisdiction_id


# The columns the query code relies on. Schema changes are appended to DDL_STATEMENTS as idempotent
# `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` lines, so an older table converges; ensure_schema then
# refuses to continue if a table still lacks one of these (e.g. a prototype table from an earlier build).
EXPECTED_COLUMNS = {
    "bill_id",
    "jurisdiction_id",
    "session_identifier",
    "identifier",
    "identifier_norm",
    "title",
    "chamber",
    "latest_action_date",
    "latest_action_description",
    "fts",
    "source_updated_at",
    "refreshed_at",
}

EXPECTED_INDEXES = {
    "ddp_bill_search_fts_idx",
    "ddp_bill_search_title_idx",
    "ddp_bill_search_ident_idx",
    "ddp_bill_search_juris_idx",
}

DDL_STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    """
    CREATE TABLE IF NOT EXISTS ddp_bill_search (
        bill_id             varchar PRIMARY KEY REFERENCES opencivicdata_bill(id) ON DELETE CASCADE,
        jurisdiction_id     varchar     NOT NULL,
        session_identifier  varchar     NOT NULL,
        identifier          varchar     NOT NULL,
        identifier_norm     varchar     NOT NULL,
        title               text        NOT NULL,
        chamber             varchar,
        latest_action_date  varchar,
        latest_action_description text,
        fts                 tsvector    NOT NULL,
        source_updated_at   timestamptz NOT NULL,
        refreshed_at        timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ddp_bill_search_fts_idx   ON ddp_bill_search USING gin (fts)",
    "CREATE INDEX IF NOT EXISTS ddp_bill_search_title_idx ON ddp_bill_search USING gin (title gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS ddp_bill_search_ident_idx ON ddp_bill_search (identifier_norm varchar_pattern_ops)",
    "CREATE INDEX IF NOT EXISTS ddp_bill_search_juris_idx ON ddp_bill_search (jurisdiction_id)",
]

# A bill needs (re)building when it has no row, or its own row or any archived document is newer
# than the newest input the projection last saw (source_updated_at).
_STALE_FROM_WHERE = """
    FROM opencivicdata_bill b
    JOIN opencivicdata_legislativesession sess ON sess.id = b.legislative_session_id
    LEFT JOIN ddp_bill_search s ON s.bill_id = b.id
    WHERE (CAST(:jurisdiction_id AS varchar) IS NULL OR sess.jurisdiction_id = :jurisdiction_id)
      AND (
            s.bill_id IS NULL
         OR b.updated_at > s.source_updated_at
         OR EXISTS (SELECT 1 FROM ddp_bill_version_document d
                    WHERE d.bill_id = b.id AND d.updated_at > s.source_updated_at)
      )
"""
STALE_SQL = text("SELECT b.id " + _STALE_FROM_WHERE + " ORDER BY b.id LIMIT :limit")
STALE_COUNT_SQL = text("SELECT count(*) " + _STALE_FROM_WHERE)

DOCS_SQL = text(
    """
    SELECT id, bill_id, version_note, version_date, media_type, char_length(raw_text) AS n_chars
    FROM ddp_bill_version_document
    WHERE bill_id = ANY(:bill_ids) AND NOT is_error AND coalesce(raw_text, '') <> ''
"""
)

# One set-based upsert per batch. `doc_id` is the stage-chosen archived document for the bill
# (NULL when there is no usable archived text, leaving the C-weight component empty).
UPSERT_SQL = text(
    """
    INSERT INTO ddp_bill_search AS t
        (bill_id, jurisdiction_id, session_identifier, identifier, identifier_norm, title,
         chamber, latest_action_date, latest_action_description, fts, source_updated_at, refreshed_at)
    SELECT b.id, sess.jurisdiction_id, sess.identifier, b.identifier,
           upper(regexp_replace(b.identifier, '[\\s-]', '', 'g')),
           coalesce(b.title, ''),
           org.classification, b.latest_action_date, b.latest_action_description,
           setweight(to_tsvector('english', coalesce(b.title, '')), 'A')
             || setweight(to_tsvector('english',
                    coalesce((SELECT string_agg(a.abstract, ' ') FROM opencivicdata_billabstract a
                              WHERE a.bill_id = b.id), '')
                    || ' ' || coalesce(array_to_string(b.subject, ' '), '')), 'B')
             || setweight(to_tsvector('english',
                    coalesce((SELECT left(d.raw_text, :cap) FROM ddp_bill_version_document d
                              WHERE d.id = p.doc_id), '')), 'C'),
           greatest(b.updated_at,
                    coalesce((SELECT max(d2.updated_at) FROM ddp_bill_version_document d2
                              WHERE d2.bill_id = b.id), b.updated_at)),
           now()
    FROM unnest(CAST(:bill_ids AS varchar[]), CAST(:doc_ids AS int[])) AS p(bill_id, doc_id)
    JOIN opencivicdata_bill b ON b.id = p.bill_id
    JOIN opencivicdata_legislativesession sess ON sess.id = b.legislative_session_id
    LEFT JOIN opencivicdata_organization org ON org.id = b.from_organization_id
    ON CONFLICT (bill_id) DO UPDATE SET
        jurisdiction_id = EXCLUDED.jurisdiction_id,
        session_identifier = EXCLUDED.session_identifier,
        identifier = EXCLUDED.identifier, identifier_norm = EXCLUDED.identifier_norm,
        title = EXCLUDED.title, chamber = EXCLUDED.chamber,
        latest_action_date = EXCLUDED.latest_action_date,
        latest_action_description = EXCLUDED.latest_action_description, fts = EXCLUDED.fts,
        source_updated_at = EXCLUDED.source_updated_at,
        refreshed_at = EXCLUDED.refreshed_at
"""
)

# The FK's ON DELETE CASCADE does not fire for rows deleted by logical-replication apply
# (session_replication_role = replica), so orphans are swept explicitly, not trusted away.
ORPHAN_SQL = text(
    """
    DELETE FROM ddp_bill_search s
    WHERE (CAST(:jurisdiction_id AS varchar) IS NULL OR s.jurisdiction_id = :jurisdiction_id)
      AND NOT EXISTS (SELECT 1 FROM opencivicdata_bill b WHERE b.id = s.bill_id)
"""
)


def ensure_schema(conn: Connection) -> None:
    """Idempotent; safe to call on every boot and before every refresh. Raises if the table exists
    but lacks a column the query code relies on (`CREATE TABLE IF NOT EXISTS` alone would accept it)."""
    with conn.begin():
        for statement in DDL_STATEMENTS:
            conn.execute(text(statement))
        present = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'ddp_bill_search' AND table_schema = current_schema()"
                )
            )
        }
    missing = EXPECTED_COLUMNS - present
    if not missing:
        with conn.begin():
            fts_type = conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'ddp_bill_search' AND table_schema = current_schema() "
                    "AND column_name = 'fts'"
                )
            ).scalar()
            indexes = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = 'ddp_bill_search' AND schemaname = current_schema()"
                    )
                )
            }
        if fts_type != "tsvector" or not EXPECTED_INDEXES <= indexes:
            raise RuntimeError(
                f"ddp_bill_search has fts type {fts_type!r} and indexes {sorted(indexes)}; "
                f"expected tsvector and {sorted(EXPECTED_INDEXES)}"
            )
    if missing:
        raise RuntimeError(
            f"ddp_bill_search is missing columns {sorted(missing)}; add an ALTER to DDL_STATEMENTS"
        )


def pick_current_docs(rows) -> Dict[str, int]:
    """bill_id -> id of the current version's archived document.

    Same rule as BillPagination.postprocess_includes: drop STAGE_UNKNOWN notes, order by
    version_sort_key (never DB row order or BillVersion.date, blank for most jurisdictions), take
    the last; within one version prefer the PDF, matching archive_bill_versions()'s diff lineage.
    """
    by_bill: Dict[str, list] = {}
    for doc_id, bill_id, note, date, media_type, _n_chars in rows:
        if note_stage(note or "")[0] == STAGE_UNKNOWN:
            continue
        by_bill.setdefault(bill_id, []).append(
            (doc_id, note or "", date or "", media_type)
        )
    picks: Dict[str, int] = {}
    for bill_id, docs in by_bill.items():
        docs.sort(
            key=lambda d: (
                version_sort_key(d[1], d[2]),
                d[3] == "application/pdf",
                d[0],
            )
        )
        picks[bill_id] = docs[-1][0]
    return picks


def invalidate_all(conn: Connection, jurisdiction_id: Optional[str] = None) -> int:
    """--full rebuild: mark rows stale instead of looping over a 'full' predicate, so an
    interrupted rebuild resumes where it stopped and re-running it afterwards costs nothing."""
    with conn.begin():
        return conn.execute(
            text(
                """
                UPDATE ddp_bill_search SET source_updated_at = 'epoch'
                WHERE CAST(:j AS varchar) IS NULL OR jurisdiction_id = :j
            """
            ),
            {"j": jurisdiction_id},
        ).rowcount


def _within_text_budget(ids, picks, chars_by_doc) -> list:
    """The prefix of `ids` whose chosen documents total at most MAX_CHARS_PER_STATEMENT of text
    (always at least one bill, so a single huge document still makes progress)."""
    chosen, total = [], 0
    for bill_id in ids:
        n = min(chars_by_doc.get(picks.get(bill_id), 0), TEXT_CAP_CHARS)
        if chosen and total + n > MAX_CHARS_PER_STATEMENT:
            break
        chosen.append(bill_id)
        total += n
    return chosen


def _refresh_one(
    conn: Connection,
    jurisdiction_id: str,
    limit: int = 200,
    time_budget_s: float = 20.0,
) -> dict:
    """Refresh stale bills, at most `limit` per statement (and MAX_CHARS_PER_STATEMENT of text per
    statement), stopping between statements once `time_budget_s` is spent. Returns {"refreshed",
    "with_text", "orphans_removed", "more", "busy"}; a caller loops while `more`.

    Bounded on purpose: this runs inside a web request (POST /ddp/search/refresh). Each statement is
    capped by text volume, so the budget -- checked between statements -- really does bound the call
    to roughly the budget plus one short statement. The stale predicate is the cursor, so the next call
    simply continues. `conn` must be a dedicated connection (engine.connect()), because the
    session-level advisory lock that serialises same-jurisdiction refreshes lives and dies with it.
    """
    started = time.monotonic()
    key = _lock_key(jurisdiction_id)
    with conn.begin():
        locked = conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key}
        ).scalar()
    if not locked:
        return {
            "refreshed": 0,
            "with_text": 0,
            "orphans_removed": 0,
            "more": False,
            "busy": True,
        }
    refreshed = with_text = orphans = 0
    more = False
    try:
        ensure_schema(conn)
        while True:
            with conn.begin():
                # One snapshot for choosing documents and for the upsert's own reads (including the
                # source_updated_at watermark it stores): a document written between the two statements
                # keeps a newer updated_at than the watermark, so the next pass rebuilds that bill
                # instead of the projection recording a state it never indexed. Must be the first statement.
                conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                conn.execute(
                    text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                )
                ids = [
                    r[0]
                    for r in conn.execute(
                        STALE_SQL, {"jurisdiction_id": jurisdiction_id, "limit": limit}
                    ).fetchall()
                ]
                if not ids:
                    break
                doc_rows = conn.execute(DOCS_SQL, {"bill_ids": ids}).fetchall()
                picks = pick_current_docs(doc_rows)
                chars_by_doc = {r[0]: r[5] for r in doc_rows}
                taken = _within_text_budget(ids, picks, chars_by_doc)
                doc_ids = [picks.get(i) for i in taken]
                conn.execute(
                    UPSERT_SQL,
                    {"bill_ids": taken, "doc_ids": doc_ids, "cap": TEXT_CAP_CHARS},
                )
            refreshed += len(taken)
            with_text += sum(1 for d in doc_ids if d is not None)
            if len(taken) == len(ids) and len(ids) < limit:
                break  # drained: everything stale fit in this statement and there was less than a full batch
            if time.monotonic() - started >= time_budget_s:
                more = (
                    True  # budget spent with work possibly left: the caller continues
                )
                break
        if not more:
            with conn.begin():
                orphans = conn.execute(
                    ORPHAN_SQL, {"jurisdiction_id": jurisdiction_id}
                ).rowcount
        return {
            "refreshed": refreshed,
            "with_text": with_text,
            "orphans_removed": orphans,
            "more": more,
            "busy": False,
        }
    finally:
        with conn.begin():
            conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key})


JURISDICTIONS_SQL = text(
    """
    SELECT jurisdiction_id FROM opencivicdata_legislativesession
    UNION SELECT jurisdiction_id FROM ddp_bill_search
"""
)


def refresh_batch(
    conn: Connection,
    jurisdiction_id: Optional[str] = None,
    limit: int = 200,
    time_budget_s: float = 20.0,
) -> dict:
    """Refresh one jurisdiction, or (jurisdiction_id=None) every jurisdiction in turn.

    There is deliberately no separate "global" lock: an all-jurisdictions refresh is just the scoped
    refresh run for each jurisdiction, taking that jurisdiction's own lock, so it can never process a
    row concurrently with a scoped refresh. A jurisdiction whose lock is held is skipped and reported
    (`busy: true`, so the caller retries). The scheduled path (ddp-sync) always passes a jurisdiction;
    the unscoped form is for operators."""
    if jurisdiction_id is not None:
        return _refresh_one(conn, jurisdiction_id, limit, time_budget_s)
    started = time.monotonic()
    with conn.begin():
        jids = sorted(r[0] for r in conn.execute(JURISDICTIONS_SQL))
    total = {
        "refreshed": 0,
        "with_text": 0,
        "orphans_removed": 0,
        "more": False,
        "busy": False,
    }
    for i, jid in enumerate(jids):
        remaining = time_budget_s - (time.monotonic() - started)
        if remaining <= 0:
            total["more"] = True  # jurisdictions left unvisited: the caller continues
            break
        part = _refresh_one(conn, jid, limit, remaining)
        for k in ("refreshed", "with_text", "orphans_removed"):
            total[k] += part[k]
        total["more"] = total["more"] or part["more"]
        total["busy"] = total["busy"] or part["busy"]
    return total


def stale_count(conn: Connection, jurisdiction_id: Optional[str] = None) -> int:
    """How many bills the next refresh would rebuild (the --dry-run number)."""
    with conn.begin():
        return conn.execute(
            STALE_COUNT_SQL, {"jurisdiction_id": jurisdiction_id}
        ).scalar()


def _cli(argv=None) -> int:
    """python -m api.search_projection ensure|refresh [--jurisdiction fl] [--full] [--dry-run]

    Operator entry point (Mac boot script, RDS host runbook, manual repair). The scheduled path
    is POST /ddp/search/refresh, driven by ddp-sync."""
    import argparse

    from openstates.metadata import lookup

    from .db import engine

    ap = argparse.ArgumentParser(prog="python -m api.search_projection")
    ap.add_argument("command", choices=["ensure", "refresh"])
    ap.add_argument("--jurisdiction")
    ap.add_argument(
        "--full", action="store_true", help="mark every row stale, then refresh"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print how many bills would be rebuilt"
    )
    args = ap.parse_args(argv)
    jid = (
        lookup(abbr=args.jurisdiction.lower()).jurisdiction_id
        if args.jurisdiction
        else None
    )
    started = time.monotonic()
    with engine.connect() as conn:
        ensure_schema(conn)
        if args.command == "ensure":
            print("=== BILL SEARCH ENSURE: ddp_bill_search present ===")
            return 0
        if args.dry_run:
            print(
                f"=== BILL SEARCH REFRESH (dry-run): {args.jurisdiction or 'all'} | "
                f"would_refresh={stale_count(conn, jid)} ==="
            )
            return 0
        if args.full:
            invalidate_all(conn, jid)
        totals = {"refreshed": 0, "with_text": 0, "orphans_removed": 0}
        while True:
            batch = refresh_batch(
                conn, jurisdiction_id=jid, limit=500, time_budget_s=1e9
            )
            if batch["busy"]:
                print("=== BILL SEARCH REFRESH: another refresh holds the lock ===")
                return 1
            for k in totals:
                totals[k] += batch[k]
            if not batch["more"]:
                break
        print(
            f"=== BILL SEARCH REFRESH: {args.jurisdiction or 'all'} | "
            f"mode={'full' if args.full else 'incremental'} | refreshed={totals['refreshed']} | "
            f"with_text={totals['with_text']} | orphans_removed={totals['orphans_removed']} | "
            f"seconds={time.monotonic() - started:.1f} ==="
        )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_cli())
