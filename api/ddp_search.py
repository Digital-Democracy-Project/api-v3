"""DDP enterprise-search endpoints over the ddp-openstates database
(PLAN-enterprise-search.md §6.2). Mounted at /ddp/search/*; ddp-api's /openstates/* catch-all proxy
forwards these with no ddp-api change.

Read-only except POST /refresh, which only ever rebuilds the derived ddp_bill_search table.
"""
import re
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from openstates.metadata import lookup
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import search_projection
from .auth import apikey_auth
from .db import engine, get_db

router = APIRouter(prefix="/ddp/search", tags=["ddp-search"])

MAX_QUERY_CHARS = 200
MAX_LIMIT = 100  # the broker asks each leg for a 100-candidate window (PLAN §6.1)
MAX_HYDRATE_IDS = 50  # one results page
# Measured 2026-09-29 on 75,805 real bills: title word_similarity at 0.5 returns "Medicaid
# Expansion" bills for the misspelling "medicade expansion" (0.73) in ~14 ms. Tune against the
# judged set (PLAN §9) before treating 0.5 as final.
WORD_SIMILARITY_THRESHOLD = "0.5"

_likely_bill_id = re.compile(r"[A-Za-z]{1,4}\s*-?\s*\d{1,5}")

_BILL_COLUMNS = """
    s.bill_id, s.jurisdiction_id, s.session_identifier, s.identifier, s.title, s.chamber,
    s.latest_action_date, s.latest_action_description
"""

# Optional exact-session scope, shared by every bill query. People are not session-scoped.
_SESSION_FILTER = (
    "AND (CAST(:session AS varchar) IS NULL OR s.session_identifier = :session)"
)

# Snippet = first abstract, 240 chars: OpenStates' own public description of the bill. Selected
# only for the rows that survive LIMIT, never for the whole candidate set.
_SNIPPET_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT left(a.abstract, 240) AS snippet
        FROM opencivicdata_billabstract a WHERE a.bill_id = hits.bill_id
        ORDER BY a.id LIMIT 1
    ) ab ON true
"""

EXACT_SQL = text(
    f"""
    WITH hits AS (
        SELECT {_BILL_COLUMNS}, 1.0::float AS score
        FROM ddp_bill_search s
        WHERE s.identifier_norm = upper(regexp_replace(:q, '[\\s-]', '', 'g'))
          AND s.jurisdiction_id = ANY(:jids) {_SESSION_FILTER}
        ORDER BY s.latest_action_date DESC NULLS LAST
        LIMIT :limit
    )
    SELECT hits.*, ab.snippet FROM hits {_SNIPPET_LATERAL}
"""
)

FTS_SQL = text(
    f"""
    WITH hits AS (
        SELECT {_BILL_COLUMNS}, ts_rank_cd(s.fts, query)::float AS score
        FROM ddp_bill_search s, websearch_to_tsquery('english', :q) query
        WHERE s.fts @@ query AND s.jurisdiction_id = ANY(:jids) {_SESSION_FILTER}
        ORDER BY score DESC, s.latest_action_date DESC NULLS LAST
        LIMIT :limit
    )
    SELECT hits.*, ab.snippet FROM hits {_SNIPPET_LATERAL}
"""
)

FUZZY_TITLE_SQL = text(
    f"""
    WITH hits AS (
        SELECT {_BILL_COLUMNS}, word_similarity(:q, s.title)::float AS score
        FROM ddp_bill_search s
        WHERE :q <% s.title AND s.jurisdiction_id = ANY(:jids) {_SESSION_FILTER}
        ORDER BY score DESC, s.latest_action_date DESC NULLS LAST
        LIMIT :limit
    )
    SELECT hits.*, ab.snippet FROM hits {_SNIPPET_LATERAL}
"""
)

HYDRATE_BILLS_SQL = text(
    f"""
    WITH hits AS (
        SELECT {_BILL_COLUMNS}, 0.0::float AS score
        FROM ddp_bill_search s
        WHERE s.bill_id = ANY(:ids) AND s.jurisdiction_id = ANY(:jids) {_SESSION_FILTER}
    )
    SELECT hits.*, ab.snippet FROM hits {_SNIPPET_LATERAL}
"""
)

HYDRATE_PEOPLE_SQL = text(
    """
    SELECT p.id, p.name, p.primary_party, p.current_jurisdiction_id,
           p."current_role" ->> 'title'              AS role_title,
           p."current_role" ->> 'org_classification' AS chamber,
           p."current_role" ->> 'district'           AS district,
           0.0::float AS score
    FROM opencivicdata_person p
    WHERE p.id = ANY(:ids) AND p.current_jurisdiction_id = ANY(:jids)
"""
)

COVERAGE_SQL = text(
    """
    SELECT sess.jurisdiction_id,
           count(*)                                         AS bills,
           count(s.bill_id)                                 AS projected,
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM ddp_bill_version_document d
               WHERE d.bill_id = b.id AND NOT d.is_error AND coalesce(d.raw_text, '') <> ''
           ))                                               AS with_text,
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM opencivicdata_billabstract a WHERE a.bill_id = b.id
           ))                                               AS with_abstract
    FROM opencivicdata_bill b
    JOIN opencivicdata_legislativesession sess ON sess.id = b.legislative_session_id
    LEFT JOIN ddp_bill_search s ON s.bill_id = b.id
    WHERE sess.jurisdiction_id = ANY(:jids)
    GROUP BY sess.jurisdiction_id
    ORDER BY sess.jurisdiction_id
"""
)

PEOPLE_COVERAGE_SQL = text(
    """
    SELECT current_jurisdiction_id, count(*) AS people FROM opencivicdata_person
    WHERE current_jurisdiction_id = ANY(:jids) GROUP BY current_jurisdiction_id
"""
)

SAMPLE_SQL = text(
    """
    SELECT bill_id FROM ddp_bill_search WHERE jurisdiction_id = ANY(:jids)
    ORDER BY random() LIMIT :n
"""
)

PREFIX_SQL = text(
    f"""
    WITH hits AS (
        SELECT {_BILL_COLUMNS}, 1.0::float AS score
        FROM ddp_bill_search s
        WHERE s.identifier_norm LIKE upper(regexp_replace(:q, '[\\s-]', '', 'g')) || '%'
          AND s.jurisdiction_id = ANY(:jids) {_SESSION_FILTER}
        ORDER BY s.latest_action_date DESC NULLS LAST, s.identifier_norm
        LIMIT :limit
    )
    SELECT hits.*, NULL::text AS snippet FROM hits
"""
)

# People need no projection: 4.6k people and 7.7k aliases scan in ~20 ms. Alias-aware: the best
# score over the person's own name and every opencivicdata_personname row.
PEOPLE_SQL = text(
    """
    WITH names AS (
        SELECT p.id AS person_id, p.name AS matched_name FROM opencivicdata_person p
        UNION ALL
        SELECT n.person_id, n.name FROM opencivicdata_personname n
    )
    SELECT p.id, p.name, p.primary_party, p.current_jurisdiction_id,
           p."current_role" ->> 'title'              AS role_title,
           p."current_role" ->> 'org_classification' AS chamber,
           p."current_role" ->> 'district'           AS district,
           max(word_similarity(:q, names.matched_name))::float AS score
    FROM names JOIN opencivicdata_person p ON p.id = names.person_id
    WHERE :q <% names.matched_name AND p.current_jurisdiction_id = ANY(:jids)
    GROUP BY p.id, p.name, p.primary_party, p.current_jurisdiction_id, p."current_role"
    ORDER BY score DESC, p.name
    LIMIT :limit
"""
)

# OPEN-326: a swapped pair of letters destroys most of a short surname's trigrams, so "Smtih" scores 0.33 against
# "Adam Smith" and falls under the 0.5 threshold above (measured 2026-10-05 on the 4,651 current legislators:
# a lowered threshold finds it only at 0.3, where ordinary words such as "budget" and "housing" start returning
# legislators, 5 of 33 non-name queries at 0.5 against 27 of 33 at 0.3). So the transposed spellings of a one-word
# query are also matched EXACTLY against surnames: every hit is a real surname that is the typed word with two
# adjacent letters swapped, which no loose similarity can produce (an ordinary word that happens to be one swap
# away from a surname would still match; none of 33 sampled non-name queries did). Aliases count, as in
# PEOPLE_SQL. The comparison is on lower() in the database, so a non-ASCII surname needs a UTF-8 database.
PEOPLE_TRANSPOSED_SQL = text(
    r"""
    WITH names AS (
        SELECT p.id AS person_id, p.name AS matched_name FROM opencivicdata_person p
        UNION ALL
        SELECT n.person_id, n.name FROM opencivicdata_personname n
    )
    SELECT p.id, p.name, p.primary_party, p.current_jurisdiction_id,
           p."current_role" ->> 'title'              AS role_title,
           p."current_role" ->> 'org_classification' AS chamber,
           p."current_role" ->> 'district'           AS district,
           CAST(:score AS float)                     AS score
    FROM names JOIN opencivicdata_person p ON p.id = names.person_id
    WHERE p.current_jurisdiction_id = ANY(:jids)
      AND substring(
            regexp_replace(lower(names.matched_name), ',?\s+(jr|sr|ii|iii|iv|md)\.?\s*$', '')
            from '[^\s]+$'
          ) = ANY(:variants)
    GROUP BY p.id, p.name, p.primary_party, p.current_jurisdiction_id, p."current_role"
    ORDER BY p.name
    LIMIT :limit
"""
)

_ONE_WORD = re.compile(r"[^\W\d_]{4,30}")  # letters only (accents allowed): one word, 4 to 30 letters


def _transposed_spellings(q: str) -> List[str]:
    """The lower-cased query with each pair of adjacent DIFFERENT letters swapped, one spelling per swap; empty
    unless the query is a single alphabetic word of 4 to 30 letters (a phrase, a number or a hyphenated name
    is left to the trigram match alone)."""
    word = q.strip().lower()
    if not _ONE_WORD.fullmatch(word):
        return []
    return sorted(
        {word[:i] + word[i + 1] + word[i] + word[i + 2 :] for i in range(len(word) - 1) if word[i] != word[i + 1]}
    )


def _abbr(jurisdiction_id: str) -> str:
    return lookup(jurisdiction_id=jurisdiction_id).abbr.upper()


def _jurisdiction_name(jurisdiction_id: str) -> str:
    return lookup(jurisdiction_id=jurisdiction_id).name


def _jurisdiction_ids(codes: List[str]) -> List[str]:
    if not codes:
        raise HTTPException(400, "at least one 'jurisdiction' is required")
    ids = []
    for code in codes:
        try:
            ids.append(lookup(abbr=code.lower()).jurisdiction_id)
        except KeyError:
            raise HTTPException(400, f"unknown jurisdiction '{code}'")
    return ids


def _bill_hit(row) -> dict:
    return {
        "entity_type": "bill",
        "id": row.bill_id,
        "identifier": row.identifier,
        "title": row.title,
        "jurisdiction": _abbr(row.jurisdiction_id),
        "jurisdiction_name": _jurisdiction_name(row.jurisdiction_id),
        "session": row.session_identifier,
        "chamber": row.chamber,
        "latest_action_date": row.latest_action_date,
        "latest_action_description": row.latest_action_description,
        "snippet": row.snippet,
        "score": row.score,
    }


def _person_hit(row) -> dict:
    return {
        "entity_type": "person",
        "id": row.id,
        "name": row.name,
        "party": row.primary_party,
        "jurisdiction": _abbr(row.current_jurisdiction_id),
        "jurisdiction_name": _jurisdiction_name(row.current_jurisdiction_id),
        "role_title": row.role_title,
        "chamber": row.chamber,
        "district": row.district,
        "score": row.score,
    }


def _people_hits(db: Session, params: dict) -> List[dict]:
    """People for the query: the trigram matches, then (OPEN-326) anyone whose surname is the typed word with two
    adjacent letters swapped. A transposition hit is scored at exactly the threshold, the least a hit can score
    (`<%` is inclusive, so a trigram hit can score the same), so it sits below any stronger direct match and
    `_rank` settles a tie. A person found both ways appears once, with the trigram hit. Each query is limited
    separately, so this can return up to twice `limit`: the callers sort the merged list and cut it to `limit`."""
    hits = [_person_hit(r) for r in db.execute(PEOPLE_SQL, params)]
    variants = _transposed_spellings(params["q"])
    if variants:
        seen = {h["id"] for h in hits}
        rows = db.execute(
            PEOPLE_TRANSPOSED_SQL,
            {**params, "variants": variants, "score": float(WORD_SIMILARITY_THRESHOLD)},
        )
        hits += [_person_hit(r) for r in rows if r.id not in seen]
    return hits


def _rank(hit: dict):
    # Highest score first; on a tie a person outranks a bill (a short typed prefix like "smi" is
    # far more often a name than a title). Deterministic so the judged set can pin behaviour.
    return (
        -hit["score"],
        0 if hit["entity_type"] == "person" else 1,
        hit.get("name") or hit["title"],
    )


def _validated_types(types: List[str]) -> set:
    wanted = set(types)
    if not wanted or not wanted <= {"bill", "person"}:
        raise HTTPException(400, "types must be any of: bill, person")
    return wanted


def _validated_query(q: str, min_len: int, max_len: int) -> str:
    q = q.strip()
    if not (min_len <= len(q) <= max_len):
        raise HTTPException(400, f"q must be {min_len}-{max_len} characters")
    return q


def _use_similarity_threshold(db: Session) -> None:
    # is_local=true: applies to this transaction only, never leaks to the pooled connection.
    db.execute(
        text("SELECT set_config('pg_trgm.word_similarity_threshold', :t, true)"),
        {"t": WORD_SIMILARITY_THRESHOLD},
    )


@router.get("")
def search(
    q: str = Query(..., description="Search text."),
    jurisdiction: List[str] = Query(
        [], description="Two-letter codes; at least one required."
    ),
    session: Optional[str] = Query(
        None, description="Exact session identifier; bills only."
    ),
    types: List[str] = Query(["bill", "person"], description="Any of: bill, person."),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    db: Session = Depends(get_db),
    auth: str = Depends(apikey_auth),
):
    """Three ranked lists, deliberately NOT fused here (scales differ; the broker fuses them):
    `exact` (bill-number match), `text` (full-text rank over title/abstract/current text) and
    `names` (bill titles and legislator names on one shared word_similarity scale, so the two
    kinds are directly comparable and pre-merged)."""
    started = time.monotonic()
    q = _validated_query(q, 1, MAX_QUERY_CHARS)
    jids = _jurisdiction_ids(jurisdiction)
    wanted = _validated_types(types)
    params = {"q": q, "jids": jids, "limit": limit, "session": session}
    _use_similarity_threshold(db)

    exact, text_hits, names = [], [], []
    if "bill" in wanted:
        if _likely_bill_id.fullmatch(q):
            exact = [_bill_hit(r) for r in db.execute(EXACT_SQL, params)]
        text_hits = [_bill_hit(r) for r in db.execute(FTS_SQL, params)]
    if len(q) >= 3:  # trigram matching is meaningless below three characters
        if "bill" in wanted:
            names += [_bill_hit(r) for r in db.execute(FUZZY_TITLE_SQL, params)]
        if "person" in wanted:
            names += _people_hits(db, params)
        names.sort(key=_rank)
    return {
        "q": q,
        "exact": exact,
        "text": text_hits,
        "names": names[:limit],
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
    }


@router.get("/suggest")
def suggest(
    q: str = Query(...),
    jurisdiction: List[str] = Query([]),
    limit: int = Query(8, ge=1, le=10),
    db: Session = Depends(get_db),
    auth: str = Depends(apikey_auth),
):
    """Type-ahead: bill-number prefixes first, then title and name matches on the shared
    word_similarity scale. No full-text branch (too slow and too loose for as-you-type)."""
    started = time.monotonic()
    q = _validated_query(q, 2, 100)
    jids = _jurisdiction_ids(jurisdiction)
    params = {"q": q, "jids": jids, "limit": limit, "session": None}
    _use_similarity_threshold(db)

    results: List[dict] = []
    if _likely_bill_id.fullmatch(q):
        results += [_bill_hit(r) for r in db.execute(PREFIX_SQL, params)]
    if len(q) >= 3:
        fuzzy = [_bill_hit(r) for r in db.execute(FUZZY_TITLE_SQL, params)]
        fuzzy += _people_hits(db, params)
        fuzzy.sort(key=_rank)
        seen = {h["id"] for h in results}
        results += [h for h in fuzzy if h["id"] not in seen]
    return {
        "q": q,
        "results": results[:limit],
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
    }


@router.get("/hydrate")
def hydrate(
    ids: List[str] = Query(..., alias="id", max_items=MAX_HYDRATE_IDS),
    jurisdiction: List[str] = Query([]),
    session: Optional[str] = Query(
        None, description="Exact session identifier; bills only."
    ),
    db: Session = Depends(get_db),
    auth: str = Depends(apikey_auth),
):
    """Details for ids another source found (a Pinecone hit, a broker row): 'ocd-bill/...' and
    'ocd-person/...' ids, mixed freely. Doubles as the scope check -- an id outside the requested
    jurisdictions (or session), or not in the projection, is simply absent from the response."""
    jids = _jurisdiction_ids(jurisdiction)
    bill_ids = [i for i in ids if i.startswith("ocd-bill/")]
    person_ids = [i for i in ids if i.startswith("ocd-person/")]
    hits = []
    if bill_ids:
        hits += [
            _bill_hit(r)
            for r in db.execute(
                HYDRATE_BILLS_SQL, {"ids": bill_ids, "jids": jids, "session": session}
            )
        ]
    if person_ids:
        hits += [
            _person_hit(r)
            for r in db.execute(HYDRATE_PEOPLE_SQL, {"ids": person_ids, "jids": jids})
        ]
    return {"results": hits}


@router.get("/coverage")
def coverage(
    jurisdiction: List[str] = Query([]),
    sample: int = Query(
        0, ge=0, le=1000, description="Also return this many random bill ids."
    ),
    db: Session = Depends(get_db),
    auth: str = Depends(apikey_auth),
):
    """Per-jurisdiction completeness for the Phase 0 report (PLAN §4.4, §4.5.7): bills, projected rows,
    bills with usable archived text, bills with an abstract, people. On an instance that does not
    replicate abstracts and people (the Mac subscriber, §4.5.6) the last three read as zero/low."""
    jids = _jurisdiction_ids(jurisdiction)
    rows = db.execute(COVERAGE_SQL, {"jids": jids}).fetchall()
    people = {
        r.current_jurisdiction_id: r.people
        for r in db.execute(PEOPLE_COVERAGE_SQL, {"jids": jids})
    }
    out = {
        "jurisdictions": [
            {
                "jurisdiction": _abbr(r.jurisdiction_id),
                "bills": r.bills,
                "projected": r.projected,
                "with_text": r.with_text,
                "with_abstract": r.with_abstract,
                "people": people.get(r.jurisdiction_id, 0),
            }
            for r in rows
        ]
    }
    if sample:
        out["sample_ids"] = [
            r.bill_id for r in db.execute(SAMPLE_SQL, {"jids": jids, "n": sample})
        ]
    return out


@router.post("/refresh")
def refresh(
    jurisdiction: Optional[str] = Query(
        None, description="Two-letter code; omit for all."
    ),
    limit: int = Query(200, ge=1, le=1000),
    auth: str = Depends(apikey_auth),
):
    """Bring ddp_bill_search up to date. Bounded per call; a caller loops while `more` is true."""
    jid = _jurisdiction_ids([jurisdiction])[0] if jurisdiction else None
    with engine.connect() as conn:
        # The all-jurisdictions form of refresh_batch reads ddp_bill_search before anything has ensured
        # it exists, so on a fresh instance the first POST must create it (OPEN-308 review carry-over).
        # Idempotent, so it is also safe to repeat on every call.
        search_projection.ensure_schema(conn)
        return search_projection.refresh_batch(conn, jurisdiction_id=jid, limit=limit)
