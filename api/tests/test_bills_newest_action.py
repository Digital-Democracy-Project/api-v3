"""OPEN-469: GET /bills?sort=latest_action_desc, for one jurisdiction and for all of them."""
import datetime
import uuid

from sqlalchemy import event, select

from .conftest import TestingSessionLocal, engine
from ..db.models import Bill, LegislativeSession


def _dates(response):
    assert response.status_code == 200, response.text
    return [b["latest_action_date"] for b in response.json()["results"]]


def _add_bill(identifier, latest_action_date, bill_id=None):
    """A bill in an existing session; returns its id (the caller deletes it)."""
    db = TestingSessionLocal()
    try:
        session = db.execute(select(LegislativeSession)).scalars().first()
        org = db.execute(
            select(Bill.from_organization_id).where(
                Bill.legislative_session_id == session.id
            )
        ).scalar()
        bill = Bill(
            id=bill_id or "ocd-bill/" + str(uuid.uuid4()),
            identifier=identifier,
            title="Test bill",
            legislative_session_id=session.id,
            from_organization_id=org,
            extras={},
            classification=["bill"],
            subject=[],
            created_at=datetime.datetime.utcnow(),
            updated_at=datetime.datetime.utcnow(),
            latest_action_date=latest_action_date,
        )
        db.add(bill)
        db.commit()
        return bill.id
    finally:
        db.close()


def _delete_bill(bill_id):
    db = TestingSessionLocal()
    try:
        db.delete(db.get(Bill, bill_id))
        db.commit()
    finally:
        db.close()


def test_one_jurisdiction_newest_first(client):
    dates = _dates(client.get("/bills?jurisdiction=ne&sort=latest_action_desc"))
    assert len(dates) == 7
    assert dates == sorted(dates, reverse=True)


def test_all_jurisdictions_newest_first_and_includes_both_states(client):
    r = client.get("/bills?sort=latest_action_desc&per_page=20")
    dates = _dates(r)
    assert dates == sorted(dates, reverse=True)
    # every bill of every fixture jurisdiction is in the count, not just one state's
    ne = client.get("/bills?jurisdiction=ne").json()["pagination"]["total_items"]
    oh = client.get("/bills?jurisdiction=oh").json()["pagination"]["total_items"]
    assert r.json()["pagination"]["total_items"] >= ne + oh


def test_a_bill_with_no_action_date_sorts_last_not_first(client):
    bill_id = _add_bill("UNDATED 1", None)
    try:
        per_page = 5
        first = client.get(f"/bills?sort=latest_action_desc&per_page={per_page}")
        last_page = first.json()["pagination"]["max_page"]
        assert (
            last_page > 1
        )  # so "last" means the end of the whole list, not of one page
        # the undated bill is not on the first page ...
        assert bill_id not in [b["id"] for b in first.json()["results"]]
        # ... it is the very last bill of the list
        last = client.get(
            f"/bills?sort=latest_action_desc&per_page={per_page}&page={last_page}"
        )
        dates = _dates(last)
        assert not dates[-1] and all(dates[:-1])  # the API shows a missing date as ""
        assert last.json()["results"][-1]["id"] == bill_id
    finally:
        _delete_bill(bill_id)


def test_paging_through_the_list_never_repeats_or_skips_a_bill(client):
    first = client.get("/bills?sort=latest_action_desc&per_page=3&page=1").json()
    total = first["pagination"]["total_items"]
    ids = [b["id"] for b in first["results"]]
    for page in range(2, first["pagination"]["max_page"] + 1):
        ids += [
            b["id"]
            for b in client.get(
                f"/bills?sort=latest_action_desc&per_page=3&page={page}"
            ).json()["results"]
        ]
    assert len(ids) == total and len(set(ids)) == total


def test_newest_action_order_is_the_date_nulls_last_then_the_bill_id(client):
    # The tie-break cannot be seen in a small fixture (ties come back in scan order, which looks
    # fixed), so read the SQL the endpoint actually sent.
    sent = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        sent.append(" ".join(statement.split()))

    event.listen(engine, "after_cursor_execute", grab)
    try:
        assert client.get("/bills?sort=latest_action_desc").status_code == 200
    finally:
        event.remove(engine, "after_cursor_execute", grab)
    wanted = (
        "ORDER BY opencivicdata_bill.latest_action_date DESC NULLS LAST, "
        "opencivicdata_bill.id LIMIT"
    )
    assert any(wanted in statement for statement in sent), sent


def test_default_order_still_needs_a_jurisdiction_or_a_query(client):
    # unchanged: no sort, or any other sort, with neither still answers 400
    assert client.get("/bills").status_code == 400
    assert client.get("/bills?sort=updated_desc").status_code == 400
    assert client.get("/bills?sort=latest_action_asc").status_code == 400


def test_a_session_still_needs_a_jurisdiction_with_the_new_order(client):
    r = client.get("/bills?sort=latest_action_desc&session=2020")
    assert r.status_code == 400
    assert "session requires a jurisdiction" in r.json()["detail"]
    assert (
        client.get(
            "/bills?jurisdiction=ne&session=2020&sort=latest_action_desc"
        ).status_code
        == 200
    )


def test_other_orders_are_unchanged_for_one_jurisdiction(client):
    for sort in (
        "updated_desc",
        "updated_asc",
        "first_action_desc",
        "latest_action_asc",
    ):
        assert client.get(f"/bills?jurisdiction=ne&sort={sort}").status_code == 200
