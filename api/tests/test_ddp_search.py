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


# --- suggest --------------------------------------------------------------------------------------


def test_suggest_bill_number_prefix_first_then_names(built, api):
    r = api.get(
        "/ddp/search/suggest", params={"q": "HB 1", "jurisdiction": ["AK"]}
    ).json()
    assert [h["identifier"] for h in r["results"][:2]] == ["HB 12", "HB 1"] or {
        h["identifier"] for h in r["results"][:2]
    } == {"HB 1", "HB 12"}
    assert all(h["entity_type"] == "bill" for h in r["results"][:2])


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


# --- authorisation (PLAN §4.5.4 item 5) -----------------------------------------------------------
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
