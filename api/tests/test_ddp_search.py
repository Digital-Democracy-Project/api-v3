"""/ddp/search routes (OPEN-309, PLAN-enterprise-search.md §6.2, §12, §4.5.4).

Uses its own two jurisdictions (Alaska and Wyoming, which conftest's shared fixtures do not touch)
and removes every row it creates, so it cannot change the counts other test modules assert on.
"""
import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from api import auth as auth_module
from api import ddp_search
from api import search_projection as sp
from api.db import get_db
from api.db.models import (
    Bill,
    BillAbstract,
    BillVersionDocument,
    Jurisdiction,
    LegislativeSession,
    Organization,
    Person,
    PersonName,
    Profile,
)
from .conftest import TestingSessionLocal, engine, get_test_db

AK = "ocd-jurisdiction/country:us/state:ak/government"
WY = "ocd-jurisdiction/country:us/state:wy/government"
SESSIONS = {
    "ak": "00000309-0000-0000-0000-00000000000a",
    "wy": "00000309-0000-0000-0000-00000000000b",
}
JIDS = {"ak": AK, "wy": WY}
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _bill(db, code, n, identifier, title, subject=None):
    b = Bill(
        id=f"ocd-bill/t309-{code}-{n}",
        identifier=identifier,
        title=title,
        legislative_session_id=SESSIONS[code],
        from_organization_id=f"org309-{code}-lower",
        subject=subject or [],
        classification=["bill"],
        extras={},
        created_at=T0,
        updated_at=T0,
        latest_action_date="2026-01-02",
        latest_action_description="Referred to committee",
    )
    db.add(b)
    db.commit()
    return b


def _person(db, code, n, name, aliases=()):
    db.add(
        Person(
            id=f"ocd-person/t309-{code}-{n}",
            name=name,
            party="Independent",
            jurisdiction_id=JIDS[code],
            current_role={
                "title": "Senator",
                "org_classification": "upper",
                "district": "7",
            },
            created_at=T0,
            updated_at=T0,
            extras={},
        )
    )
    db.commit()
    for alias in aliases:
        db.add(PersonName(person_id=f"ocd-person/t309-{code}-{n}", name=alias, note=""))
    db.commit()


@pytest.fixture
def world():
    db = TestingSessionLocal()
    for code, name in (("ak", "Alaska"), ("wy", "Wyoming")):
        j = Jurisdiction(
            id=JIDS[code],
            name=name,
            classification="state",
            division_id=f"ocd-division/country:us/state:{code}",
        )
        db.add(j)
        db.add(
            Organization(
                id=f"org309-{code}-lower",
                name=f"{name} House",
                classification="lower",
                jurisdiction=j,
            )
        )
        db.add(
            LegislativeSession(
                id=SESSIONS[code], jurisdiction=j, identifier="2026", name="2026"
            )
        )
    db.commit()

    b1 = _bill(db, "ak", 1, "HB 1", "An Act Relating to Medicaid Expansion")
    b12 = _bill(
        db, "ak", 12, "HB 12", "School Lunch Programs Act", subject=["Education"]
    )
    _bill(db, "ak", 30, "SB 30", "Smithson")
    _bill(db, "wy", 1, "HB 1", "Medicaid Expansion in Wyoming")
    db.add(
        BillAbstract(
            bill_id=b12.id,
            abstract="Free lunches for every student in the state.",
            note="",
        )
    )
    db.add(
        BillVersionDocument(
            bill_id=b1.id,
            version_note="Introduced",
            version_date="2026-01-01",
            source_url="https://x/b1",
            media_type="text/html",
            raw_text="Newts shall be protected in every wetland.",
            is_error=False,
            updated_at=T0,
        )
    )
    db.commit()
    _person(db, "ak", 1, "Smithson")
    _person(db, "ak", 2, "Nancy Whitfield", aliases=["Nan Whitfield"])
    _person(db, "wy", 1, "Roberta Yellowtail")

    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS ddp_bill_search"))
    yield db
    db.close()
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS ddp_bill_search"))
        c.execute(
            text(
                "DELETE FROM opencivicdata_personname WHERE person_id LIKE 'ocd-person/t309-%'"
            )
        )
        c.execute(
            text("DELETE FROM opencivicdata_person WHERE id LIKE 'ocd-person/t309-%'")
        )
        for table in ("ddp_bill_version_document", "opencivicdata_billabstract"):
            c.execute(text(f"DELETE FROM {table} WHERE bill_id LIKE 'ocd-bill/t309-%'"))
        c.execute(
            text("DELETE FROM opencivicdata_bill WHERE id LIKE 'ocd-bill/t309-%'")
        )
        c.execute(
            text("DELETE FROM opencivicdata_legislativesession WHERE id IN (:a, :b)"),
            {"a": SESSIONS["ak"], "b": SESSIONS["wy"]},
        )
        c.execute(
            text("DELETE FROM opencivicdata_organization WHERE id LIKE 'org309-%'")
        )
        c.execute(
            text("DELETE FROM opencivicdata_jurisdiction WHERE id IN (:a, :b)"),
            {"a": AK, "b": WY},
        )


@pytest.fixture
def built(world, client, monkeypatch):
    """The world with the projection built through the real POST /refresh route."""
    monkeypatch.setattr(ddp_search, "engine", engine)
    r = client.post("/ddp/search/refresh")
    assert r.status_code == 200 and not r.json()["more"]
    return world


@pytest.fixture
def api(client, monkeypatch):
    monkeypatch.setattr(ddp_search, "engine", engine)
    return client


def _ids(hits):
    return [h["id"] for h in hits]


# --- mounting -------------------------------------------------------------------------------------


def test_router_is_mounted_on_the_real_app():
    from api.main import app

    paths = {(m, r.path) for r in app.routes for m in getattr(r, "methods", ())}
    for path in ("", "/suggest", "/hydrate", "/coverage"):
        assert ("GET", "/ddp/search" + path) in paths
    assert ("POST", "/ddp/search/refresh") in paths


# --- refresh --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params", [{}, {"jurisdiction": "AK"}], ids=["all-jurisdictions", "scoped"]
)
def test_refresh_creates_the_table_on_a_fresh_instance(world, api, params):
    """OPEN-308 carry-over. The all-jurisdictions form of refresh_batch reads ddp_bill_search before
    anything has ensured it exists, so without the route's own ensure_schema call this is a 500."""
    with engine.connect() as c:
        assert c.execute(text("SELECT to_regclass('ddp_bill_search')")).scalar() is None
    r = api.post("/ddp/search/refresh", params=params)
    assert r.status_code == 200, r.text
    assert r.json()["refreshed"] >= (
        3 if params else 4
    )  # unscoped also builds the shared fixtures' bills
    with engine.connect() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM ddp_bill_search WHERE bill_id LIKE 'ocd-bill/t309-%'"
            )
        ).scalar()
    assert n == (3 if params else 4)


def test_refresh_second_call_is_a_no_op(built, api):
    body = api.post("/ddp/search/refresh").json()
    assert body["refreshed"] == 0 and body["more"] is False and body["busy"] is False


def test_refresh_limit_is_a_per_statement_batch_size_and_a_small_one_still_drains(
    world, api
):
    r = api.post(
        "/ddp/search/refresh", params={"jurisdiction": "AK", "limit": 1}
    ).json()
    assert r["refreshed"] == 3 and r["more"] is False


def test_refresh_reports_busy_while_the_same_jurisdiction_is_locked(world, api):
    api.post("/ddp/search/refresh", params={"jurisdiction": "AK"})  # creates the table
    holder = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        holder.execute(
            text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": sp._lock_key(AK)}
        )
        assert (
            api.post("/ddp/search/refresh", params={"jurisdiction": "AK"}).json()[
                "busy"
            ]
            is True
        )
        # a different jurisdiction is not blocked
        assert (
            api.post("/ddp/search/refresh", params={"jurisdiction": "WY"}).json()[
                "busy"
            ]
            is False
        )
    finally:
        holder.execute(
            text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": sp._lock_key(AK)}
        )
        holder.close()


@pytest.mark.parametrize(
    "params", [{"jurisdiction": "ZZ"}, {"limit": 0}, {"limit": 1001}]
)
def test_refresh_rejects_bad_parameters(api, params):
    assert 400 <= api.post("/ddp/search/refresh", params=params).status_code < 500


# --- search ---------------------------------------------------------------------------------------


def test_search_exact_scoped_and_ambiguous_across_jurisdictions(built, api):
    one = api.get(
        "/ddp/search", params={"q": "HB 12", "jurisdiction": ["AK", "WY"]}
    ).json()
    assert _ids(one["exact"]) == ["ocd-bill/t309-ak-12"]
    assert one["exact"][0]["snippet"] == "Free lunches for every student in the state."
    both = api.get(
        "/ddp/search", params={"q": "hb1", "jurisdiction": ["AK", "WY"]}
    ).json()
    assert sorted(h["jurisdiction"] for h in both["exact"]) == [
        "AK",
        "WY",
    ]  # no arbitrary single winner
    only = api.get("/ddp/search", params={"q": "HB 1", "jurisdiction": ["WY"]}).json()
    assert [h["jurisdiction"] for h in only["exact"]] == ["WY"]


# Real bill-number shapes the exact tier used to miss (OPEN-316), surveyed 2026-10-05 over every
# projected bill: FL special sessions end in a letter (HB 1C, HB 5403E), the US has 5-7 letter prefixes
# (HJRES, HCONRES), MI numbers some resolutions by letter alone (HJR A), and people type dots (H.R. 1).
_SHAPES = [
    # (stored identifier, queries that must find exactly it)
    ("HB 1C", ["HB 1C", "HB1C", "hb 1c", "HB-1C"]),
    ("HB 5403E", ["HB 5403E", "hb5403e"]),
    ("HCONRES 1", ["HCONRES 1", "hconres1"]),
    ("SJRES 9", ["SJRES 9", "SJRES9"]),
    ("HJR A", ["HJR A", "hjr a", "HJR-A"]),
    ("HJR AA", ["HJR AA"]),
    ("HR 7", ["HR 7", "H.R. 7", "h.r.7", "H. R. 7"]),
    ("HJRES 1", ["HJRES 1", "H.J. Res. 1", "H.J.Res.1"]),
]


@pytest.fixture
def shapes(built, api):
    """The built world plus one bill per surveyed shape, projected through the real refresh route."""
    for n, (identifier, _) in enumerate(_SHAPES, start=100):
        _bill(built, "ak", n, identifier, f"Shape fixture {n}")
    assert api.post("/ddp/search/refresh").status_code == 200
    return built


@pytest.mark.parametrize(
    "identifier,query",
    [(i, q) for i, queries in _SHAPES for q in queries],
)
def test_search_exact_finds_every_surveyed_bill_number_shape(shapes, api, identifier, query):
    hits = api.get("/ddp/search", params={"q": query, "jurisdiction": ["AK"]}).json()["exact"]
    assert [h["identifier"] for h in hits] == [identifier]


@pytest.mark.parametrize(
    "identifier,query",
    [(i, q) for i, queries in _SHAPES for q in queries],
)
def test_suggest_finds_every_surveyed_bill_number_shape_too(shapes, api, identifier, query):
    """suggest depends on the same gate and the same normalisation through its own PREFIX_SQL."""
    r = api.get("/ddp/search/suggest", params={"q": query, "jurisdiction": ["AK"]}).json()
    assert identifier in [h["identifier"] for h in r["results"]]


@pytest.mark.parametrize(
    "q",
    ["HB 1", "HB 12", "SB 2518", "HB 5403E", "HCONRES 135", "H.R. 1", "H. R. 1", "H.J. Res. 1", "S.J.Res. 9", "HJR A", "HB-1C", "hb1c"],
)
def test_the_bill_number_gate_accepts_real_shapes(q):
    assert ddp_search._likely_bill_id.fullmatch(q)


@pytest.mark.parametrize(
    "q",
    ["school lunch", "medicaid expansion", "qqqq zzzz wwww", "HB", "a", "H", "tax cut now", "hello world 12345678"],
)
def test_the_bill_number_gate_rejects_ordinary_queries(q):
    assert not ddp_search._likely_bill_id.fullmatch(q)


def test_the_bill_number_gate_is_fast_on_pathological_whitespace():
    """The two adjacent whitespace quantifiers must not turn a long run of spaces into a stall: a query is
    at most 200 characters (MAX_QUERY_CHARS), so the worst case here is the longest accepted query."""
    import time

    started = time.monotonic()
    assert not ddp_search._likely_bill_id.fullmatch("A" + " " * 198 + "1x3")
    assert not ddp_search._likely_bill_id.fullmatch("HB" + " " * 190 + "x1y")
    assert not ddp_search._likely_bill_id.fullmatch(" " * 200)
    assert time.monotonic() - started < 1.0


def test_search_full_text_finds_archived_document_text(built, api):
    r = api.get(
        "/ddp/search", params={"q": "wetland newts", "jurisdiction": ["AK"]}
    ).json()
    assert _ids(r["text"]) == ["ocd-bill/t309-ak-1"]
    assert r["text"][0]["score"] > 0


def test_search_tolerates_a_misspelled_title_and_orders_by_similarity(built, api):
    r = api.get(
        "/ddp/search", params={"q": "medicade expansion", "jurisdiction": ["AK", "WY"]}
    ).json()
    names = [h for h in r["names"] if h["entity_type"] == "bill"]
    assert {h["id"] for h in names} == {"ocd-bill/t309-ak-1", "ocd-bill/t309-wy-1"}
    scores = [h["score"] for h in r["names"]]
    assert scores == sorted(scores, reverse=True)


def test_search_finds_a_person_by_alias(built, api):
    r = api.get(
        "/ddp/search",
        params={"q": "Nan Whitfield", "jurisdiction": ["AK"], "types": ["person"]},
    ).json()
    assert _ids(r["names"]) == ["ocd-person/t309-ak-2"]
    assert r["exact"] == [] and r["text"] == []
    assert (
        r["names"][0]["jurisdiction"] == "AK"
        and r["names"][0]["role_title"] == "Senator"
    )


# --- OPEN-326: a swapped pair of letters in a surname ----------------------------------------------


def _people(api, q, jurisdictions=("AK",), path="/ddp/search"):
    params = {"q": q, "jurisdiction": list(jurisdictions)}
    if path == "/ddp/search":
        params["types"] = ["person"]
    body = api.get(path, params=params).json()
    return body["names"] if path == "/ddp/search" else body["results"]


def test_search_finds_a_surname_typed_with_two_letters_swapped(built, api):
    """"Whtifield" scores under the 0.5 word_similarity threshold against "Nancy Whitfield", so the trigram
    match alone returns nobody; the transposed spelling "whitfield" is matched exactly against surnames."""
    hits = _people(api, "Whtifield")
    assert _ids(hits) == ["ocd-person/t309-ak-2"]
    assert hits[0]["name"] == "Nancy Whitfield" and hits[0]["score"] == ddp_search.TRANSPOSED_SURNAME_SCORE


def test_a_transposition_hit_appears_once_when_the_trigram_match_also_finds_the_person(built, api):
    hits = _people(api, "Smithsno")
    assert _ids(hits) == ["ocd-person/t309-ak-1"]
    assert hits[0]["score"] >= ddp_search.TRANSPOSED_SURNAME_SCORE  # the higher of the two, not the trigram's


def test_suggest_finds_a_surname_typed_with_two_letters_swapped(built, api):
    hits = _people(api, "Whtifield", path="/ddp/search/suggest")
    assert [h["id"] for h in hits if h["entity_type"] == "person"] == ["ocd-person/t309-ak-2"]


def test_the_surname_is_taken_without_a_generational_suffix(built, api):
    _person(built, "ak", 9, "Maria Garcia, Jr.")
    assert _ids(_people(api, "Garica")) == ["ocd-person/t309-ak-9"]


def test_many_people_sharing_the_corrected_surname_are_capped_at_the_limit_in_name_order(built, api):
    for n, first in enumerate(("Ana", "Ben", "Cy", "Di")):
        _person(built, "ak", 20 + n, f"{first} Garcia")
    expected = ["ocd-person/t309-ak-20", "ocd-person/t309-ak-21"]  # equal scores: _rank orders by name
    r = api.get("/ddp/search", params={"q": "Garica", "jurisdiction": ["AK"], "types": ["person"], "limit": 2})
    assert _ids(r.json()["names"]) == expected
    r = api.get("/ddp/search/suggest", params={"q": "Garica", "jurisdiction": ["AK"], "limit": 2})
    assert _ids(r.json()["results"]) == expected


def test_a_transposed_surname_outranks_bills_that_only_share_trigrams_with_the_typo(built, api):
    """OPEN-326, the "Garica" case: bills whose titles merely resemble the typo score above the bare 0.5 a
    transposition hit used to carry, so suggest's cut of 8 showed bills and no Garcia."""
    _person(built, "ak", 40, "Ana Garcia")
    for n in range(10):
        _bill(built, "ak", 60 + n, f"HB {60 + n}", f"America Grows Act of {2026 + n}")  # word_similarity 0.57 to "garica"
    assert api.post("/ddp/search/refresh").status_code == 200
    r = api.get("/ddp/search/suggest", params={"q": "Garica", "jurisdiction": ["AK"], "limit": 8}).json()["results"]
    assert r[0]["id"] == "ocd-person/t309-ak-40"
    weak = [h for h in r if h["entity_type"] == "bill"]
    assert weak and all(0.5 <= h["score"] < ddp_search.TRANSPOSED_SURNAME_SCORE for h in weak)  # the bills were real rivals
    body = api.get("/ddp/search", params={"q": "Garica", "jurisdiction": ["AK"]}).json()
    assert body["names"][0]["id"] == "ocd-person/t309-ak-40"


def test_a_name_typed_correctly_still_beats_a_transposition_hit(built, api):
    """The word itself is stronger evidence than a swap of it: a person whose surname IS the typed word
    (score 1.0) ranks above one whose surname is only that word swapped."""
    _person(built, "ak", 41, "Ana Garica")
    _person(built, "ak", 42, "Ben Garcia")
    r = _people(api, "Garica")
    assert _ids(r)[:2] == ["ocd-person/t309-ak-41", "ocd-person/t309-ak-42"]
    assert r[0]["score"] > r[1]["score"] == ddp_search.TRANSPOSED_SURNAME_SCORE


def test_only_the_last_word_of_a_name_counts_as_the_surname(built, api):
    """"Garcia" as a first or middle name is not a Garcia: the surname is the last word, before any suffix."""
    _person(built, "ak", 43, "Garcia Lopez")
    _person(built, "ak", 44, "Rosa Garcia Lopez")
    assert _people(api, "Garica") == []
    assert _ids(_people(api, "Lpoez")) == ["ocd-person/t309-ak-43", "ocd-person/t309-ak-44"]  # the last word does match


def test_the_transposition_query_is_limited_in_sql(built):
    for n in range(4):
        _person(built, "ak", 50 + n, f"Person{n} Garcia")
    rows = built.execute(
        ddp_search.PEOPLE_TRANSPOSED_SQL,
        {"jids": [JIDS["ak"]], "pattern": ddp_search._surname_pattern(["garcia"]), "score": 0.9, "limit": 2},
    ).fetchall()
    assert len(rows) == 2  # the callers cut to the limit anyway; this bounds the work


def test_a_surname_is_found_through_an_alias(built, api):
    """The swap is in the middle of a short surname, which the trigram match cannot reach ("smtih" scores 0.33),
    and the surname is only in the alias, so only the transposition query's alias rows can find it."""
    _person(built, "ak", 30, "Wilhelmina Ortega", aliases=["Willie Smith"])
    assert _ids(_people(api, "Smtih")) == ["ocd-person/t309-ak-30"]


def test_an_accented_surname_typed_with_its_accent_is_found(built):
    """Called below the HTTP layer on purpose: starlette 0.21's TestClient mangles a non-ASCII query string
    (a real uvicorn decodes it correctly), so this pins the SQL: Python's lower() and the database's agree."""
    _person(built, "ak", 31, "Beatriz \u00c1lvarez")
    params = {"q": "\u00c1lvraez", "jids": [JIDS["ak"]], "limit": 8}
    assert [h["id"] for h in ddp_search._people_hits(built, params)] == ["ocd-person/t309-ak-31"]


def test_a_transposed_word_that_is_no_surname_returns_nobody(built, api):
    assert _people(api, "Budegt") == []


def test_transposition_matches_stay_inside_the_requested_jurisdictions(built, api):
    assert _people(api, "Yellwotail", ("AK",)) == []
    assert _ids(_people(api, "Yellwotail", ("WY",))) == ["ocd-person/t309-wy-1"]


def test_transposed_spellings_are_only_made_for_one_alphabetic_word():
    assert "smith" in ddp_search._transposed_spellings("Smtih")
    assert ddp_search._transposed_spellings("Smtih") == sorted(set(ddp_search._transposed_spellings("smtih")))
    assert "aabb" not in ddp_search._transposed_spellings("aabb")  # equal neighbours swap to the same word
    for not_a_word in ("abc", "Nancy Whtifield", "O'Brien", "Smith-Jones", "ab12", "1234", "x" * 31, ""):
        assert ddp_search._transposed_spellings(not_a_word) == [], not_a_word


def test_search_jurisdiction_session_and_type_scoping(built, api):
    r = api.get("/ddp/search", params={"q": "expansion", "jurisdiction": ["WY"]}).json()
    assert {h["jurisdiction"] for k in ("exact", "text", "names") for h in r[k]} == {
        "WY"
    }
    none = api.get(
        "/ddp/search",
        params={"q": "expansion", "jurisdiction": ["AK"], "session": "1999"},
    ).json()
    assert none["text"] == [] and none["names"] == []
    bills_only = api.get(
        "/ddp/search",
        params={"q": "Smithson", "jurisdiction": ["AK"], "types": ["bill"]},
    ).json()
    assert {h["entity_type"] for h in bills_only["names"]} == {"bill"}
    people_only = api.get(
        "/ddp/search",
        params={"q": "Smithson", "jurisdiction": ["AK"], "types": ["person"]},
    ).json()
    assert {h["entity_type"] for h in people_only["names"]} == {"person"}


def test_names_puts_a_person_ahead_of_a_bill_at_equal_score_deterministically(
    built, api
):
    for _ in range(3):
        r = api.get(
            "/ddp/search", params={"q": "Smithson", "jurisdiction": ["AK"]}
        ).json()
        assert [(h["entity_type"], h["score"]) for h in r["names"]][:2] == [
            ("person", 1.0),
            ("bill", 1.0),
        ]


def test_search_limit_is_capped_at_100(built, api):
    assert (
        api.get(
            "/ddp/search", params={"q": "act", "jurisdiction": ["AK"], "limit": 100}
        ).status_code
        == 200
    )
    assert (
        api.get(
            "/ddp/search", params={"q": "act", "jurisdiction": ["AK"], "limit": 101}
        ).status_code
        == 422
    )
    assert (
        api.get(
            "/ddp/search", params={"q": "act", "jurisdiction": ["AK"], "limit": 0}
        ).status_code
        == 422
    )


def test_search_limit_truncates_results(built, api):
    r = api.get(
        "/ddp/search",
        params={"q": "expansion", "jurisdiction": ["AK", "WY"], "limit": 1},
    ).json()
    assert len(r["names"]) == 1 and len(r["text"]) <= 1


@pytest.mark.parametrize(
    "params",
    [
        {"q": "x"},  # no jurisdiction
        {"q": "x", "jurisdiction": ["ZZ"]},  # unknown jurisdiction
        {"q": "x" * 201, "jurisdiction": ["AK"]},  # over-long q
        {"q": "   ", "jurisdiction": ["AK"]},  # blank q
        {"q": "x", "jurisdiction": ["AK"], "types": ["committee"]},  # unknown type
        {"jurisdiction": ["AK"]},  # q missing
    ],
)
def test_search_validation_is_4xx_never_5xx(api, params):
    assert 400 <= api.get("/ddp/search", params=params).status_code < 500


def test_search_short_query_skips_trigram_matching(built, api):
    r = api.get("/ddp/search", params={"q": "HB", "jurisdiction": ["AK"]})
    assert r.status_code == 200 and r.json()["names"] == []


def test_similarity_threshold_does_not_leak_to_the_pooled_connection(built, api):
    api.get("/ddp/search", params={"q": "medicade expansion", "jurisdiction": ["AK"]})
    db = TestingSessionLocal()
    try:
        assert (
            db.execute(text("SHOW pg_trgm.word_similarity_threshold")).scalar() == "0.6"
        )
    finally:
        db.close()


def _bill_with_look_alike_title(db):
    """A bill whose TITLE resembles a bill number that does not exist, the way real procedural titles
    ("Providing for consideration of the bill (H.R. 9999)") resemble numbers."""
    _bill(db, "ak", 31, "SB 31", "Relating to HB 9999999 appropriations")


def test_a_bill_number_that_does_not_exist_finds_nothing_not_look_alike_titles(built, api):
    """OPEN-316: "HB 99999999" is well-formed but matches no bill; it fell through to the fuzzy title
    match and returned bills whose titles share trigrams with it. A number is looked up as a number."""
    _bill_with_look_alike_title(built)
    assert api.post("/ddp/search/refresh").status_code == 200
    r = api.get("/ddp/search", params={"q": "HB 99999999", "jurisdiction": ["AK"]}).json()
    assert (r["exact"], r["text"], r["names"]) == ([], [], [])


def test_a_bill_number_that_exists_still_gets_title_matches_too(built, api):
    """Only a number that matched nothing skips the title match: a number that exists behaves as before."""
    _bill(built, "ak", 32, "SB 32", "Relating to HB 12 funding")
    assert api.post("/ddp/search/refresh").status_code == 200
    r = api.get("/ddp/search", params={"q": "HB 12", "jurisdiction": ["AK"]}).json()
    assert _ids(r["exact"]) == ["ocd-bill/t309-ak-12"]
    assert "ocd-bill/t309-ak-32" in _ids(r["names"])


def test_suggest_a_bill_number_prefix_with_no_match_suggests_no_look_alike_titles(built, api):
    _bill_with_look_alike_title(built)
    assert api.post("/ddp/search/refresh").status_code == 200
    r = api.get("/ddp/search/suggest", params={"q": "HB 99999", "jurisdiction": ["AK"]}).json()
    assert [h for h in r["results"] if h["entity_type"] == "bill"] == []


@pytest.mark.parametrize(
    "q",
    ["HB 99999999", "S 987654", "S1", "H 1", "H.R. 1", "H. R. 1", "H.J. Res. 1", "HB 1C", "HB-1", "hjres 12", "SB 2518E", "SPB 7042", "SD 50"],
)
def test_a_recognised_designator_then_digits_is_a_bill_number(q):
    assert ddp_search._is_missing_bill_number(q, [])
    assert not ddp_search._is_missing_bill_number(q, ["a hit"])  # found by number: title matching stays on


@pytest.mark.parametrize(
    "q",
    [
        "school lunch", "school lu", "medicade expansion", "qqqq zzzz wwww", "HB", "smith 3rd grade",
        "tax 2026 reform", "COVID 19", "COVID-19", "Title 42", "Section 230", "Article 5", "Prop 8", "U.S. 50",
        "Chapter 11 bankruptcy", "HJR A", "H2O", "H 2 O", "S3D", "H.2.O",
    ],
)
def test_anything_else_keeps_fuzzy_title_matching(q):
    """Numbered topics and half-typed titles are searches for a title, not a bill: only a recognised
    designator followed by digits counts as a number, so an unknown prefix fails safe."""
    assert not ddp_search._is_missing_bill_number(q, [])


@pytest.mark.parametrize("q", ["COVID 19", "Title 42", "Section 230", "Prop 8", "Article 5", "H2O"])
def test_numbered_topic_titles_are_still_found_by_search_and_suggest(built, api, q):
    """The reviewer's case: a bill titled for a numbered topic must come back for that query. None of these
    is a bill number, so none may lose its title match."""
    for n, title in enumerate(
        ("COVID 19 Emergency Relief", "Title 42 Border Authority", "Section 230 Reform", "Prop 8 Repeal", "Article 5 Convention", "H2O Quality Standards"),
        start=40,
    ):
        _bill(built, "ak", n, f"SB {n}", title)
    assert api.post("/ddp/search/refresh").status_code == 200
    wanted = {"COVID 19": 40, "Title 42": 41, "Section 230": 42, "Prop 8": 43, "Article 5": 44, "H2O": 45}[q]
    assert f"ocd-bill/t309-ak-{wanted}" in _ids(
        api.get("/ddp/search", params={"q": q, "jurisdiction": ["AK"]}).json()["names"]
    )
    assert f"ocd-bill/t309-ak-{wanted}" in _ids(
        api.get("/ddp/search/suggest", params={"q": q, "jurisdiction": ["AK"]}).json()["results"]
    )


# --- suggest --------------------------------------------------------------------------------------


def test_suggest_bill_number_prefix_first_then_names(built, api):
    r = api.get(
        "/ddp/search/suggest", params={"q": "HB 1", "jurisdiction": ["AK"]}
    ).json()
    assert [h["identifier"] for h in r["results"][:2]] == ["HB 1", "HB 12"]
    assert all(h["entity_type"] == "bill" for h in r["results"][:2])


def test_suggest_puts_the_exact_number_first_then_longer_numbers(built, api):
    """OPEN-316: "HB 1" listed HB 116, HB 147 ... and buried the bills numbered exactly HB 1, because the
    prefix lookup ordered by latest action date. Every number added here is newer than the exact ones, so a
    date-only order would put them first."""
    for n, identifier, code in ((200, "HB 10", "ak"), (201, "HB 100", "ak"), (202, "HB 123", "wy")):
        b = _bill(built, code, n, identifier, f"Newer bill {n}")
        b.latest_action_date = "2026-06-01"
        built.commit()
    assert api.post("/ddp/search/refresh").status_code == 200
    r = api.get(
        "/ddp/search/suggest", params={"q": "HB 1", "jurisdiction": ["AK", "WY"], "limit": 10}
    ).json()
    ids = [h["identifier"] for h in r["results"] if h["entity_type"] == "bill"]
    assert ids[:2] == ["HB 1", "HB 1"]  # the exact number in both jurisdictions, before anything longer
    longer = ids[2:]
    assert sorted(longer, key=len) == longer  # then shorter numbers before longer ones
    assert {"HB 10", "HB 12", "HB 100", "HB 123"} <= set(longer)


def test_suggest_limit_keeps_the_exact_numbers_and_drops_newer_longer_ones(built, api):
    """The ORDER BY inside the CTE decides which rows survive LIMIT. Two exact HB 1 (older) against four newer,
    longer numbers, with limit 2: a date-only inner order would keep the newer ones and drop the exact bills
    before any outer sort could rescue them."""
    for n, identifier in enumerate(("HB 10", "HB 11", "HB 100", "HB 101"), start=210):
        b = _bill(built, "ak", n, identifier, f"Newer bill {n}")
        b.latest_action_date = "2026-06-01"
        built.commit()
    assert api.post("/ddp/search/refresh").status_code == 200
    r = api.get(
        "/ddp/search/suggest", params={"q": "HB 1", "jurisdiction": ["AK", "WY"], "limit": 2}
    ).json()
    assert [h["identifier"] for h in r["results"]] == ["HB 1", "HB 1"]


def test_suggest_bill_number_ties_break_by_jurisdiction_whatever_order_they_are_asked_in(built, api):
    """AK and WY each have an HB 1 with the same date and length: the order is the documented tie-break
    (jurisdiction id), not whichever the planner scans first or the order the codes were given in."""
    for order in (["AK", "WY"], ["WY", "AK"]):
        r = api.get("/ddp/search/suggest", params={"q": "HB 1", "jurisdiction": order, "limit": 10}).json()
        assert [h["jurisdiction"] for h in r["results"][:2]] == ["AK", "WY"]


def test_suggest_bill_hits_keep_the_public_shape(built, api):
    """The CTE now carries identifier_norm and norm_len for ordering; neither may leak into the response."""
    hit = api.get("/ddp/search/suggest", params={"q": "HB 1", "jurisdiction": ["AK"]}).json()["results"][0]
    assert set(hit) == {
        "entity_type", "id", "identifier", "title", "jurisdiction", "jurisdiction_name", "session",
        "chamber", "latest_action_date", "latest_action_description", "snippet", "score",
    }


def test_suggest_prefers_a_name_and_does_not_repeat_ids(built, api):
    r = api.get(
        "/ddp/search/suggest", params={"q": "smiths", "jurisdiction": ["AK"]}
    ).json()
    assert r["results"][0]["entity_type"] == "person"
    ids = _ids(r["results"])
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "params",
    [
        {"q": "a", "jurisdiction": ["AK"]},  # under 2 chars
        {"q": "x" * 101, "jurisdiction": ["AK"]},  # over 100
        {"q": "ab"},  # no jurisdiction
        {"q": "ab", "jurisdiction": ["AK"], "limit": 11},  # over the type-ahead cap
    ],
)
def test_suggest_validation_is_4xx(api, params):
    assert 400 <= api.get("/ddp/search/suggest", params=params).status_code < 500


# --- hydrate --------------------------------------------------------------------------------------


def test_hydrate_returns_mixed_ids_and_drops_unknown_ones(built, api):
    ids = [
        "ocd-bill/t309-ak-12",
        "ocd-person/t309-ak-2",
        "ocd-bill/does-not-exist",
        "garbage",
    ]
    r = api.get(
        "/ddp/search/hydrate", params={"id": ids, "jurisdiction": ["AK"]}
    ).json()
    assert sorted(_ids(r["results"])) == ["ocd-bill/t309-ak-12", "ocd-person/t309-ak-2"]


def test_hydrate_id_outside_the_requested_jurisdictions_is_absent(built, api):
    ids = ["ocd-bill/t309-ak-12", "ocd-bill/t309-wy-1", "ocd-person/t309-ak-2"]
    r = api.get(
        "/ddp/search/hydrate", params={"id": ids, "jurisdiction": ["WY"]}
    ).json()
    assert _ids(r["results"]) == ["ocd-bill/t309-wy-1"]


def test_hydrate_session_scope_applies_to_bills_only(built, api):
    ids = ["ocd-bill/t309-ak-12", "ocd-person/t309-ak-2"]
    r = api.get(
        "/ddp/search/hydrate",
        params={"id": ids, "jurisdiction": ["AK"], "session": "1999"},
    ).json()
    assert _ids(r["results"]) == ["ocd-person/t309-ak-2"]


def test_hydrate_caps_at_50_ids(built, api):
    ids = [f"ocd-bill/none-{i}" for i in range(50)]
    assert (
        api.get(
            "/ddp/search/hydrate", params={"id": ids, "jurisdiction": ["AK"]}
        ).status_code
        == 200
    )
    ids.append("ocd-bill/none-50")
    assert (
        api.get(
            "/ddp/search/hydrate", params={"id": ids, "jurisdiction": ["AK"]}
        ).status_code
        == 422
    )


def test_hydrate_requires_ids_and_a_jurisdiction(api):
    assert (
        api.get("/ddp/search/hydrate", params={"jurisdiction": ["AK"]}).status_code
        == 422
    )
    assert (
        api.get("/ddp/search/hydrate", params={"id": ["ocd-bill/x"]}).status_code == 400
    )


# --- coverage -------------------------------------------------------------------------------------


def test_coverage_counts_before_and_after_a_build(world, api):
    api.post(
        "/ddp/search/refresh", params={"jurisdiction": "WY", "limit": 1}
    )  # creates the table, builds WY
    rows = {
        j["jurisdiction"]: j
        for j in api.get(
            "/ddp/search/coverage", params={"jurisdiction": ["AK", "WY"]}
        ).json()["jurisdictions"]
    }
    assert rows["AK"] == {
        "jurisdiction": "AK",
        "bills": 3,
        "projected": 0,
        "with_text": 1,
        "with_abstract": 1,
        "people": 2,
    }
    assert (
        rows["WY"]["bills"] == 1
        and rows["WY"]["projected"] == 1
        and rows["WY"]["people"] == 1
    )
    api.post("/ddp/search/refresh")
    rows = api.get(
        "/ddp/search/coverage", params={"jurisdiction": ["AK", "WY"]}
    ).json()["jurisdictions"]
    assert all(j["projected"] == j["bills"] for j in rows)


def test_coverage_sample_returns_projected_bill_ids_only(built, api):
    r = api.get(
        "/ddp/search/coverage", params={"jurisdiction": ["AK"], "sample": 2}
    ).json()
    assert set(r) == {"jurisdictions", "sample_ids"}
    assert len(r["sample_ids"]) == 2 and all(
        i.startswith("ocd-bill/t309-ak-") for i in r["sample_ids"]
    )
    assert (
        "sample_ids"
        not in api.get("/ddp/search/coverage", params={"jurisdiction": ["AK"]}).json()
    )


def test_coverage_validation(api):
    assert (
        api.get(
            "/ddp/search/coverage", params={"jurisdiction": ["AK"], "sample": 1001}
        ).status_code
        == 422
    )
    assert api.get("/ddp/search/coverage").status_code == 400


# --- authorisation, proxy contract (PLAN §4.5.4 item 5) ---------------------------------------
# ddp-api's /openstates/{path} catch-all decides read vs write scope from the HTTP method, then forwards
# to this app with the internal key. What api-v3 can guarantee, and what these tests pin: every route
# demands apikey_auth, only /refresh is a non-GET (so it is the only route ddp-api can map to write
# scope), and the responses carry ids and display fields only. The ddp-api half (401/403 for a
# read-scope key on POST) is a ddp-api test and a Phase 0 check on the real deployment.

ROUTES = [
    ("GET", "/ddp/search", {"q": "act", "jurisdiction": ["AK"]}),
    ("GET", "/ddp/search/suggest", {"q": "ab", "jurisdiction": ["AK"]}),
    ("GET", "/ddp/search/hydrate", {"id": ["ocd-bill/x"], "jurisdiction": ["AK"]}),
    ("GET", "/ddp/search/coverage", {"jurisdiction": ["AK"]}),
    ("POST", "/ddp/search/refresh", {"jurisdiction": "AK"}),
]


class _StubLimiter:
    def check_limit_and_increment_counters(self, key, tier):
        return None


@pytest.fixture
def real_auth_client(world, monkeypatch):
    """A tiny app with the router and the REAL apikey_auth (conftest overrides it on the main app)."""
    monkeypatch.setattr(
        auth_module, "limiter", _StubLimiter()
    )  # the real one needs Redis
    monkeypatch.setattr(ddp_search, "engine", engine)
    app = FastAPI()
    app.include_router(ddp_search.router)
    app.dependency_overrides[get_db] = get_test_db
    db = TestingSessionLocal()
    db.add(Profile(id="t309-profile", api_key="t309-key", api_tier="default"))
    db.commit()
    yield TestClient(app)
    db.query(Profile).filter(Profile.id == "t309-profile").delete()
    db.commit()
    db.close()


@pytest.mark.parametrize("method,path,params", ROUTES)
def test_every_route_rejects_a_request_without_a_key(
    real_auth_client, method, path, params
):
    r = real_auth_client.request(method, path, params=params)
    assert r.status_code == 403


@pytest.mark.parametrize("method,path,params", ROUTES)
def test_every_route_rejects_an_unknown_key(real_auth_client, method, path, params):
    r = real_auth_client.request(
        method, path, params=params, headers={"X-API-KEY": "nope"}
    )
    assert r.status_code == 401


@pytest.mark.parametrize("method,path,params", ROUTES)
def test_every_route_accepts_a_valid_key(real_auth_client, method, path, params):
    ensure = real_auth_client.post(
        "/ddp/search/refresh",
        params={"jurisdiction": "AK"},
        headers={"X-API-KEY": "t309-key"},
    )
    assert ensure.status_code == 200
    r = real_auth_client.request(
        method, path, params=params, headers={"X-API-KEY": "t309-key"}
    )
    assert r.status_code == 200, r.text


def test_refresh_does_no_work_for_an_unauthenticated_caller(real_auth_client):
    real_auth_client.post("/ddp/search/refresh", params={"jurisdiction": "AK"})
    with engine.connect() as c:
        assert c.execute(text("SELECT to_regclass('ddp_bill_search')")).scalar() is None


def test_only_refresh_is_a_write_route_and_all_routes_depend_on_apikey_auth():
    methods = {}
    for route in ddp_search.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert auth_module.apikey_auth in calls, route.path
        methods[route.path] = route.methods
    assert methods.pop("/ddp/search/refresh") == {"POST"}
    assert all(m == {"GET"} for m in methods.values())
    assert len(methods) == 4
