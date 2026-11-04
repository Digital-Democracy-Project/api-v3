import pytest
from sqlalchemy import event
from api.db import get_db, models
from api.pagination import Pagination
from .conftest import engine


def test_pagination_basic(client):
    response = client.get("/jurisdictions")
    response = response.json()
    assert response["pagination"] == {
        "page": 1,
        "max_page": 1,
        "per_page": 52,
        "total_items": 3,
    }


def test_pagination_empty(client):
    response = client.get("/people?jurisdiction=wi")
    response = response.json()
    assert response["pagination"] == {
        "page": 1,
        "max_page": 1,
        "per_page": 10,
        "total_items": 0,
    }


def test_pagination_per_page(client):
    response = client.get("/jurisdictions?per_page=2")
    response = response.json()
    assert response["pagination"] == {
        "page": 1,
        "max_page": 2,
        "per_page": 2,
        "total_items": 3,
    }


def test_pagination_page2(client):
    response = client.get("/jurisdictions?per_page=2&page=2")
    response = response.json()
    assert response["results"][0]["name"] == "Ohio"
    assert response["pagination"] == {
        "page": 2,
        "max_page": 2,
        "per_page": 2,
        "total_items": 3,
    }


def test_pagination_invalid_per_page(client):
    response = client.get("/jurisdictions?per_page=0")
    assert response.status_code == 400
    response = response.json()
    assert "invalid per_page" in response["detail"]

    response = client.get("/jurisdictions?per_page=999")
    assert response.status_code == 400
    response = response.json()
    assert "invalid per_page" in response["detail"]


def test_pagination_invalid_page(client):
    response = client.get("/jurisdictions?per_page=2&page=0")
    assert response.status_code == 404
    response = response.json()
    assert "invalid page" in response["detail"]

    response = client.get("/jurisdictions?per_page=2&page=5")
    assert response.status_code == 404
    response = response.json()
    assert "invalid page" in response["detail"]


def test_pagination_no_order_by():
    db = list(get_db())[0]
    query = db.query(models.Jurisdiction)
    p = Pagination()
    with pytest.raises(Exception) as e:
        p.paginate(query)
    assert "ordering is required for pagination" in str(e)


@pytest.mark.parametrize(
    "path, total, page_order",
    [
        (
            "/bills?sort=latest_action_desc&per_page=2",
            None,
            "ORDER BY opencivicdata_bill.latest_action_date DESC NULLS LAST, opencivicdata_bill.id LIMIT",
        ),
        ("/jurisdictions", 3, "ORDER BY opencivicdata_jurisdiction.name LIMIT"),
    ],
)
def test_the_total_count_does_not_sort_but_the_page_still_does(
    client, path, total, page_order
):
    # OPEN-484: Query.count() keeps the ORDER BY inside its subquery, so Postgres sorted every row only to
    # count it. The count must not carry the order; the page query must (paging needs it).
    sent = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        sent.append(" ".join(statement.split()))

    event.listen(engine, "after_cursor_execute", grab)
    try:
        response = client.get(path)
    finally:
        event.remove(engine, "after_cursor_execute", grab)
    assert response.status_code == 200, response.text
    counts = [s for s in sent if s.startswith("SELECT count(*)")]
    assert len(counts) == 1 and "ORDER BY" not in counts[0], counts
    assert any(page_order in s for s in sent), sent  # the page is still ordered
    if total is not None:
        assert response.json()["pagination"]["total_items"] == total
