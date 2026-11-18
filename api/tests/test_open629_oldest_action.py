"""OPEN-629: GET /bills?sort=latest_action_asc: oldest action first, undated last, ties by id,
for one jurisdiction and for all of them."""
from sqlalchemy import event

from .conftest import engine
from .test_bills_newest_action import _add_bill, _dates, _delete_bill


def _ids(client, url):
    r = client.get(url)
    assert r.status_code == 200, r.text
    return [b["id"] for b in r.json()["results"]]


def test_all_jurisdictions_oldest_first(client):
    r = client.get("/bills?sort=latest_action_asc&per_page=20")
    dates = [d for d in _dates(r) if d]
    assert dates == sorted(dates)
    ne = client.get("/bills?jurisdiction=ne").json()["pagination"]["total_items"]
    oh = client.get("/bills?jurisdiction=oh").json()["pagination"]["total_items"]
    assert r.json()["pagination"]["total_items"] >= ne + oh


def test_one_jurisdiction_oldest_first(client):
    dates = _dates(client.get("/bills?jurisdiction=ne&sort=latest_action_asc"))
    assert dates == sorted(dates)


def test_oldest_first_is_the_newest_first_order_reversed_for_dated_bills(client):
    newest = _ids(client, "/bills?sort=latest_action_desc&per_page=50")
    oldest = _ids(client, "/bills?sort=latest_action_asc&per_page=50")
    # same bills (the fixture is smaller than one page) ...
    assert sorted(newest) == sorted(oldest)
    # ... and the dated ones by date the other way round (ties may differ: both break them by id)
    by_id = {
        b["id"]: b["latest_action_date"]
        for b in client.get("/bills?sort=latest_action_desc&per_page=50").json()[
            "results"
        ]
    }
    assert [by_id[i] for i in oldest if by_id[i]] == sorted(
        by_id[i] for i in newest if by_id[i]
    )


def test_a_bill_with_no_action_date_sorts_last_in_the_oldest_first_order_too(client):
    bill_id = _add_bill("UNDATED 629", None)
    try:
        per_page = 5
        first = client.get(f"/bills?sort=latest_action_asc&per_page={per_page}")
        last_page = first.json()["pagination"]["max_page"]
        assert last_page > 1
        assert bill_id not in [b["id"] for b in first.json()["results"]]
        last = client.get(
            f"/bills?sort=latest_action_asc&per_page={per_page}&page={last_page}"
        )
        assert last.json()["results"][-1]["id"] == bill_id
    finally:
        _delete_bill(bill_id)


def test_bills_sharing_a_day_come_back_by_id_and_paging_never_repeats_or_skips(client):
    # three bills on the same day, inserted out of id order: the tie-break must put them in id order on every page
    day = "1999-01-01"
    ids = [
        _add_bill(
            f"TIE {n}", day, bill_id=f"ocd-bill/00000000-0000-0000-0000-00000000062{n}"
        )
        for n in (3, 1, 2)
    ]
    try:
        first = client.get("/bills?sort=latest_action_asc&per_page=2&page=1").json()
        assert [b["id"] for b in first["results"]] == sorted(ids)[:2]
        total = first["pagination"]["total_items"]
        seen = [b["id"] for b in first["results"]]
        for page in range(2, first["pagination"]["max_page"] + 1):
            seen += _ids(
                client, f"/bills?sort=latest_action_asc&per_page=2&page={page}"
            )
        assert len(seen) == total and len(set(seen)) == total
        assert seen[2] == sorted(ids)[2]
    finally:
        for bill_id in ids:
            _delete_bill(bill_id)


def test_oldest_action_order_is_the_date_nulls_last_then_the_bill_id(client):
    sent = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        sent.append(" ".join(statement.split()))

    event.listen(engine, "after_cursor_execute", grab)
    try:
        assert client.get("/bills?sort=latest_action_asc").status_code == 200
    finally:
        event.remove(engine, "after_cursor_execute", grab)
    # ascending is the default, so the SQL has no ASC
    wanted = "ORDER BY opencivicdata_bill.latest_action_date NULLS LAST, opencivicdata_bill.id LIMIT"
    assert any(wanted in statement for statement in sent), sent


def test_a_session_still_needs_a_jurisdiction_with_the_oldest_order(client):
    r = client.get("/bills?sort=latest_action_asc&session=2020")
    assert r.status_code == 400
    assert "session requires a jurisdiction" in r.json()["detail"]
