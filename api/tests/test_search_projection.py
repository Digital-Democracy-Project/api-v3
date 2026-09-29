"""ddp_bill_search projection (OPEN-308, PLAN-enterprise-search.md §4.5.3 / §12 "Refresh").

Uses its own jurisdiction (Alaska, which conftest's shared fixtures do not touch) and removes every
row it creates, so it cannot change the counts other test modules assert on.
"""
import datetime as dt

import pytest
from sqlalchemy import text

from api import search_projection as sp
from api.db.models import (
    Bill,
    BillAbstract,
    BillVersionDocument,
    Jurisdiction,
    LegislativeSession,
    Organization,
)
from .conftest import TestingSessionLocal, engine

AK = "ocd-jurisdiction/country:us/state:ak/government"
OTHER = "ocd-jurisdiction/country:us/state:zz/government"  # a second jurisdiction for lock tests
SESSIONS = {
    "ak": "00000308-0000-0000-0000-00000000000a",
    "zz": "00000308-0000-0000-0000-00000000000b",
}
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _utc(**kw):
    return T0 + dt.timedelta(**kw)


@pytest.fixture
def world():
    """Two jurisdictions, one session and chamber each; torn down (children first) afterwards."""
    db = TestingSessionLocal()
    for jid, name, code in ((AK, "Alaska", "ak"), (OTHER, "Zed", "zz")):
        j = Jurisdiction(
            id=jid,
            name=name,
            classification="state",
            division_id=f"ocd-division/country:us/state:{code}",
        )
        db.add(j)
        db.add(
            Organization(
                id=f"org-{code}-lower",
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
    yield db
    db.close()
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS ddp_bill_search"))
        for table in ("ddp_bill_version_document", "opencivicdata_billabstract"):
            c.execute(text(f"DELETE FROM {table} WHERE bill_id LIKE 'ocd-bill/t308-%'"))
        c.execute(
            text("DELETE FROM opencivicdata_bill WHERE id LIKE 'ocd-bill/t308-%'")
        )
        c.execute(
            text("DELETE FROM opencivicdata_legislativesession WHERE id IN (:a, :b)"),
            {"a": SESSIONS["ak"], "b": SESSIONS["zz"]},
        )
        c.execute(
            text(
                "DELETE FROM opencivicdata_organization WHERE id IN ('org-ak-lower', 'org-zz-lower')"
            )
        )
        c.execute(
            text("DELETE FROM opencivicdata_jurisdiction WHERE id IN (:a, :b)"),
            {"a": AK, "b": OTHER},
        )


def add_bill(
    db,
    n,
    *,
    code="ak",
    title="An Act Relating to Newts",
    identifier=None,
    subject=None,
    updated=None,
):
    b = Bill(
        id=f"ocd-bill/t308-{code}-{n}",
        identifier=identifier or f"HB {n}",
        title=title,
        legislative_session_id=SESSIONS[code],
        from_organization_id=f"org-{code}-lower",
        subject=subject or [],
        classification=["bill"],
        extras={},
        created_at=T0,
        updated_at=updated or T0,
        latest_action_date="2026-01-02",
        latest_action_description="Referred to committee",
    )
    db.add(b)
    db.commit()
    return b


def add_doc(
    db,
    bill,
    text_,
    *,
    note="Introduced",
    media="text/html",
    updated=None,
    is_error=False,
):
    d = BillVersionDocument(
        bill_id=bill.id,
        version_note=note,
        version_date="2026-01-01",
        source_url=f"https://x/{bill.id}/{note}/{media}",
        media_type=media,
        raw_text=text_,
        is_error=is_error,
        updated_at=updated or T0,
    )
    db.add(d)
    db.commit()
    return d


def refresh(jid=AK, **kw):
    with engine.connect() as conn:
        sp.ensure_schema(conn)
        return sp.refresh_batch(conn, jurisdiction_id=jid, **kw)


def rows(jid=AK):
    with engine.connect() as c:
        return c.execute(
            text(
                "SELECT bill_id, identifier_norm, chamber, title, source_updated_at FROM ddp_bill_search "
                "WHERE jurisdiction_id = :j ORDER BY bill_id"
            ),
            {"j": jid},
        ).fetchall()


def search_ids(query, config="english"):
    with engine.connect() as c:
        return [
            r[0]
            for r in c.execute(
                text(
                    f"SELECT bill_id FROM ddp_bill_search WHERE fts @@ plainto_tsquery('{config}', :q) ORDER BY bill_id"
                ),
                {"q": query},
            )
        ]


# --- schema ---------------------------------------------------------------------------------------


def test_ensure_schema_is_idempotent_and_creates_indexes(world):
    with engine.connect() as c:
        sp.ensure_schema(c)
        sp.ensure_schema(c)
        idx = {
            r[0]
            for r in c.execute(
                text(
                    "SELECT indexname FROM pg_indexes WHERE tablename='ddp_bill_search'"
                )
            )
        }
    assert sp.EXPECTED_INDEXES <= idx


def test_ensure_schema_rejects_missing_column_then_passes_once_restored(world):
    with engine.connect() as c:
        sp.ensure_schema(c)
    with engine.begin() as c:
        c.execute(
            text("ALTER TABLE ddp_bill_search DROP COLUMN latest_action_description")
        )
    with engine.connect() as c:
        # CREATE TABLE IF NOT EXISTS accepts the old table; the column check must not.
        with pytest.raises(RuntimeError, match="latest_action_description"):
            sp.ensure_schema(c)
    with engine.begin() as c:
        c.execute(
            text(
                "ALTER TABLE ddp_bill_search ADD COLUMN latest_action_description text"
            )
        )
    with engine.connect() as c:
        sp.ensure_schema(c)


def test_ensure_schema_rejects_wrong_fts_type(world):
    with engine.connect() as c:
        sp.ensure_schema(c)
    with engine.begin() as c:
        c.execute(text("DROP INDEX ddp_bill_search_fts_idx"))
        c.execute(text("ALTER TABLE ddp_bill_search DROP COLUMN fts"))
        c.execute(
            text(
                "ALTER TABLE ddp_bill_search ADD COLUMN fts text[] NOT NULL DEFAULT '{}'"
            )
        )  # an array can still take a GIN index
    with engine.connect() as c:
        with pytest.raises(RuntimeError, match="fts type"):
            sp.ensure_schema(c)


# --- refresh: what gets rebuilt ---------------------------------------------------------------------


def test_first_refresh_builds_rows_and_second_touches_nothing(world):
    add_bill(world, 1, identifier="HB 1-A")
    add_bill(world, 2)
    r1 = refresh()
    assert (r1["refreshed"], r1["busy"], r1["more"]) == (2, False, False)
    before = rows()
    assert [r.identifier_norm for r in before] == [
        "HB1A",
        "HB2",
    ]  # spaces and hyphens removed, upper-cased
    assert all(r.chamber == "lower" for r in before)
    with engine.connect() as c:
        stamp = c.execute(
            text("SELECT max(refreshed_at) FROM ddp_bill_search")
        ).scalar()
    r2 = refresh()
    assert r2["refreshed"] == 0
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT max(refreshed_at) FROM ddp_bill_search")).scalar()
            == stamp
        )
        assert sp.stale_count(c, AK) == 0


def test_changed_bill_updated_at_refreshes_exactly_that_bill(world):
    b1, _ = add_bill(world, 1), add_bill(world, 2)
    refresh()
    b1.title = "An Act Relating to Salamanders"
    b1.updated_at = _utc(days=1)
    world.commit()
    assert refresh()["refreshed"] == 1
    assert search_ids("salamanders") == [b1.id]
    assert refresh()["refreshed"] == 0


def test_newer_or_new_document_refreshes_its_bill_and_adds_text(world):
    b1, _ = add_bill(world, 1), add_bill(world, 2)
    refresh()
    assert search_ids("scorpion") == []
    add_doc(
        world,
        b1,
        "AN ACT designating the scorpion as the state arachnid.",
        updated=_utc(days=1),
    )
    assert refresh()["refreshed"] == 1
    assert search_ids("scorpion") == [b1.id]


def test_document_flipped_to_error_refreshes_and_drops_its_text(world):
    b1 = add_bill(world, 1)
    d = add_doc(world, b1, "Text about scorpions.")
    assert refresh()["with_text"] == 1
    d.is_error = True
    d.updated_at = _utc(
        days=1
    )  # refresh-extraction writes updated_at on every document it corrects
    world.commit()
    assert refresh() == {
        "refreshed": 1,
        "with_text": 0,
        "orphans_removed": 0,
        "more": False,
        "busy": False,
    }
    assert search_ids("scorpions") == []


def test_full_marks_everything_stale_and_rebuilds_each_row_once(world):
    add_bill(world, 1)
    add_bill(world, 2)
    add_bill(world, 1, code="zz")
    refresh()
    refresh(OTHER)
    with engine.connect() as c:
        assert (
            sp.invalidate_all(c, AK) == 2
        )  # scoped: the other jurisdiction is not marked
        assert sp.stale_count(c, AK) == 2 and sp.stale_count(c, OTHER) == 0
    assert refresh()["refreshed"] == 2
    assert refresh()["refreshed"] == 0


def test_weights_and_sources_of_the_fts_column(world):
    b = add_bill(world, 1, title="Medicaid Expansion Act", subject=["Health"])
    world.add(BillAbstract(bill_id=b.id, abstract="Covers hospital financing", note=""))
    world.commit()
    add_doc(world, b, "The department shall administer telehealth waivers.")
    refresh()
    with engine.connect() as c:
        fts = c.execute(text("SELECT fts::text FROM ddp_bill_search")).scalar()
    assert "'medicaid':1A" in fts  # title
    assert (
        "'hospit':" in fts and "B" in fts.split("'hospit':")[1].split(" ")[0]
    )  # abstract
    assert (
        "'health':" in fts and "B" in fts.split("'health':")[1].split(" ")[0]
    )  # subject
    assert "C" in fts.split("'telehealth':")[1].split(" ")[0]  # archived text


def test_text_component_is_capped(world, monkeypatch):
    monkeypatch.setattr(sp, "TEXT_CAP_CHARS", 20)
    b = add_bill(world, 1)
    add_doc(world, b, "alpha bravo charlie delta echo foxtrot golf")
    refresh()
    assert search_ids("alpha") == [b.id]
    assert search_ids("golf") == []


# --- refresh: bounding, locking, orphans -------------------------------------------------------------


def test_tiny_statement_budget_still_drains(world, monkeypatch):
    monkeypatch.setattr(sp, "MAX_CHARS_PER_STATEMENT", 10)
    for n in range(1, 5):
        add_doc(world, add_bill(world, n), "x" * 100 + f" word{n}")
    total, calls = 0, 0
    while True:
        r = refresh(
            limit=3, time_budget_s=0
        )  # budget spent after the first statement, every call
        total += r["refreshed"]
        calls += 1
        assert calls < 20
        if not r["more"]:
            break
    assert total == 4 and calls >= 2
    assert refresh()["refreshed"] == 0


def test_limit_bounds_a_call_and_more_signals_continuation(world):
    for n in range(1, 6):
        add_bill(world, n)
    r = refresh(limit=2, time_budget_s=0)
    assert (r["refreshed"], r["more"]) == (2, True)
    assert r["orphans_removed"] == 0  # the sweep only runs once drained


def test_second_refresh_of_same_jurisdiction_is_busy_and_other_jurisdiction_proceeds(
    world,
):
    add_bill(world, 1)
    add_bill(world, 1, code="zz")
    with engine.connect() as held:
        with held.begin():
            assert held.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                {"k": sp._lock_key(AK)},
            ).scalar()
        try:
            assert refresh(AK) == {
                "refreshed": 0,
                "with_text": 0,
                "orphans_removed": 0,
                "more": False,
                "busy": True,
            }
            assert rows(AK) == []  # did no work
            assert (
                refresh(OTHER)["refreshed"] == 1
            )  # a different jurisdiction holds a different lock
        finally:
            with held.begin():
                held.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:k))"),
                    {"k": sp._lock_key(AK)},
                )
    assert refresh(AK)["refreshed"] == 1  # lock released by the failed/finished holder


def test_lock_is_released_after_a_failed_refresh(world, monkeypatch):
    add_bill(world, 1)
    monkeypatch.setattr(sp, "UPSERT_SQL", text("SELECT 1/0"))
    with pytest.raises(Exception):
        refresh()
    monkeypatch.undo()
    assert refresh()["refreshed"] == 1


def test_unscoped_refresh_skips_a_held_jurisdiction_and_does_the_rest(world):
    add_bill(world, 1)
    add_bill(world, 1, code="zz")
    with engine.connect() as held:
        with held.begin():
            assert held.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                {"k": sp._lock_key(AK)},
            ).scalar()
        try:
            r = refresh(None)
            assert r["busy"] is True
            assert len(rows(OTHER)) == 1 and rows(AK) == []
            assert refresh(AK)["busy"] is True
        finally:
            with held.begin():
                held.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:k))"),
                    {"k": sp._lock_key(AK)},
                )
    r = refresh(None)
    assert r["busy"] is False and len(rows(AK)) == 1


def test_orphan_left_by_replica_delete_is_removed_by_a_scoped_refresh_and_not_before(
    world,
):
    b1, b2 = add_bill(world, 1), add_bill(world, 2)
    refresh()
    other = add_bill(world, 1, code="zz")
    refresh(OTHER)
    # Logical-replication apply runs with session_replication_role = replica, which suppresses the
    # ON DELETE CASCADE, so the projection row survives its bill.
    with engine.begin() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        c.execute(text("DELETE FROM opencivicdata_bill WHERE id = :i"), {"i": b1.id})
        c.execute(text("DELETE FROM opencivicdata_bill WHERE id = :i"), {"i": other.id})
    assert {r.bill_id for r in rows(AK)} == {
        b1.id,
        b2.id,
    }  # still there: nothing swept it yet
    r = refresh(AK)
    assert r["orphans_removed"] == 1
    assert {r.bill_id for r in rows(AK)} == {b2.id}
    assert (
        len(rows(OTHER)) == 1
    )  # another jurisdiction's orphan is not this refresh's business
    assert refresh(OTHER)["orphans_removed"] == 1


def test_ordinary_delete_cascades(world):
    b = add_bill(world, 1)
    refresh()
    world.delete(b)
    world.commit()
    assert rows() == []


# --- pick_current_docs ----------------------------------------------------------------------------------


def _doc(i, bill, note, media="text/html", date=""):
    return (i, bill, note, date, media, 10)


def test_pick_current_docs_later_stage_wins_regardless_of_row_order():
    docs = [
        _doc(1, "b", "Enrolled"),
        _doc(2, "b", "Introduced"),
        _doc(3, "b", "Engrossed"),
    ]
    assert sp.pick_current_docs(docs) == {"b": 1}
    assert sp.pick_current_docs(list(reversed(docs))) == {"b": 1}


def test_pick_current_docs_prefers_pdf_within_one_version():
    docs = [
        _doc(1, "b", "Introduced", "application/pdf"),
        _doc(2, "b", "Introduced", "text/html"),
    ]
    assert sp.pick_current_docs(docs) == {"b": 1}
    assert sp.pick_current_docs(docs[::-1]) == {"b": 1}


def test_pick_current_docs_ignores_unclassifiable_notes():
    assert sp.STAGE_UNKNOWN == sp.note_stage("zzz unrecognised")[0]
    docs = [
        _doc(1, "only-unknown", "zzz unrecognised"),
        _doc(2, "mixed", "zzz unrecognised"),
        _doc(3, "mixed", "Introduced"),
    ]
    assert sp.pick_current_docs(docs) == {
        "mixed": 3
    }  # a bill with only unknown notes gets no text


def test_bill_with_only_unclassifiable_versions_is_indexed_without_text(world):
    b = add_bill(world, 1)
    add_doc(world, b, "mysterious wombat text", note="zzz unrecognised")
    r = refresh()
    assert (r["refreshed"], r["with_text"]) == (1, 0)
    assert search_ids("wombat") == []


def test_within_text_budget_always_takes_at_least_one(monkeypatch):
    monkeypatch.setattr(sp, "MAX_CHARS_PER_STATEMENT", 1_500_000)
    chars = {
        1: 3_000_000,
        2: 3_000_000,
        3: 10,
    }  # each document counts as at most TEXT_CAP_CHARS (1 M)
    picks = {"a": 1, "b": 2, "c": 3}
    assert sp._within_text_budget(["a", "b", "c"], picks, chars) == ["a"]
    assert sp._within_text_budget(["c", "a", "b"], picks, chars) == ["c", "a"]


# --- CLI ------------------------------------------------------------------------------------------------


def test_cli_ensure_refresh_dry_run_and_full(world, monkeypatch, capsys):
    import api.db

    monkeypatch.setattr(
        api.db, "engine", engine
    )  # the CLI resolves the engine at call time
    add_bill(world, 1)
    add_bill(world, 2)
    assert sp._cli(["ensure"]) == 0
    assert "ddp_bill_search present" in capsys.readouterr().out

    assert sp._cli(["refresh", "--jurisdiction", "AK", "--dry-run"]) == 0
    assert "would_refresh=2" in capsys.readouterr().out
    assert rows() == []  # dry run wrote nothing

    assert sp._cli(["refresh", "--jurisdiction", "ak"]) == 0
    out = capsys.readouterr().out
    assert "refreshed=2" in out and "mode=incremental" in out

    assert sp._cli(["refresh", "--jurisdiction", "ak"]) == 0
    assert "refreshed=0" in capsys.readouterr().out  # nothing changed: nothing touched

    assert sp._cli(["refresh", "--jurisdiction", "ak", "--full"]) == 0
    assert "refreshed=2" in capsys.readouterr().out


def test_cli_reports_busy_with_nonzero_exit(world, monkeypatch, capsys):
    import api.db

    monkeypatch.setattr(api.db, "engine", engine)
    add_bill(world, 1)
    with engine.connect() as held:
        with held.begin():
            held.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                {"k": sp._lock_key(AK)},
            )
        try:
            assert sp._cli(["refresh", "--jurisdiction", "ak"]) == 1
        finally:
            with held.begin():
                held.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:k))"),
                    {"k": sp._lock_key(AK)},
                )
    assert "another refresh holds the lock" in capsys.readouterr().out
