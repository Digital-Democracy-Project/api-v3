"""OPEN-715: the sitemap bill listing (/ddp/sitemap/bills). Every bill of a jurisdiction, keyset-paged
like /ddp/embedding/ledger, with its newest vote date read in one grouped query per page. The vote
dates are compared with the Ohio fixtures' own vote events (fixtures.py), not with the route's code.
"""
from .conftest import query_logger
from .test_ddp_embedding_ledger import _bill_count

SITEMAP = "/ddp/sitemap/bills"
FIELDS = {
    "ocd_bill_id",
    "session",
    "identifier",
    "title",
    "latest_action_date",
    "latest_vote_date",
    "has_votes",
}


def _pages(client, jurisdiction="oh", limit=1000):
    """Every page of a jurisdiction's listing, in order."""
    pages, after = [], None
    while True:
        params = {"jurisdiction": jurisdiction, "limit": limit}
        if after:
            params["after"] = after
        response = client.get(SITEMAP, params=params)
        assert response.status_code == 200
        page = response.json()
        pages.append(page)
        after = page["next_after"]
        if not after:
            return pages
        assert len(pages) <= _bill_count(jurisdiction), "the cursor does not advance"


def _all(client, **kw):
    return [b for p in _pages(client, **kw) for b in p["results"]]


def test_every_bill_of_the_jurisdiction_is_listed_once_in_bill_id_order(client):
    bills = _all(client)
    ids = [b["ocd_bill_id"] for b in bills]
    assert len(ids) == _bill_count() == len(set(ids))
    assert ids == sorted(ids)
    assert all(set(b) == FIELDS for b in bills)
    assert all(not i.startswith("ocd-bill/") for i in ids)


def test_a_row_carries_the_bills_own_session_number_title_and_action_date(client):
    hb1 = {b["ocd_bill_id"]: b for b in _all(client)}["1234"]
    assert hb1["session"] == "2021"
    assert hb1["identifier"] == "HB 1"
    assert hb1["title"] == "Alphabetization of OHIO Act"
    assert hb1["latest_action_date"] == "2021-01-01"


def test_the_vote_date_is_the_newest_vote_event_and_has_votes_follows_it(client):
    bills = {b["ocd_bill_id"]: b for b in _all(client)}
    # HB 1: votes on 2021-01-01 and 2021-02-01
    assert bills["1234"]["latest_vote_date"] == "2021-02-01"
    assert bills["1234"]["has_votes"] is True
    # votes-0004: 2026-03-01 and 2026-04-01; votes-0002: one vote, 2025-02-01
    assert bills["votes-0004"]["latest_vote_date"] == "2026-04-01"
    assert bills["votes-0002"]["latest_vote_date"] == "2025-02-01"
    # votes-0003: no vote at all
    assert bills["votes-0003"]["latest_vote_date"] is None
    assert bills["votes-0003"]["has_votes"] is False


def test_paging_one_bill_at_a_time_gives_the_same_list_as_one_page(client):
    one_page = _pages(client, limit=1000)
    assert len(one_page) == 1 and one_page[0]["next_after"] is None
    paged = _pages(client, limit=1)
    assert len(paged) == _bill_count()
    assert [b for p in paged for b in p["results"]] == one_page[0]["results"]
    # the cursor is the full id of the page's last bill
    assert paged[0]["next_after"] == "ocd-bill/" + paged[0]["results"][0]["ocd_bill_id"]


def test_the_last_page_has_a_null_cursor_when_the_bill_count_is_a_multiple_of_the_limit(
    client,
):
    total = _bill_count()
    # the smallest limit above 1 that divides the bill count (the count itself if it is prime)
    limit = next(n for n in range(2, total + 1) if total % n == 0)
    pages = _pages(client, limit=limit)
    assert len(pages) == total // limit  # no empty extra request at the end
    assert all(len(p["results"]) == limit for p in pages)
    exact = client.get(SITEMAP, params={"jurisdiction": "oh", "limit": total}).json()
    assert exact["next_after"] is None and len(exact["results"]) == total


def test_a_page_costs_two_queries_whatever_its_size(client):
    client.get(SITEMAP, params={"jurisdiction": "oh", "limit": 1})
    small = query_logger.count
    client.get(SITEMAP, params={"jurisdiction": "oh", "limit": 1000})
    big = query_logger.count
    assert small == big == 2  # the bills, then one grouped vote query


def test_an_unknown_jurisdiction_and_a_bad_limit_are_refused(client):
    assert client.get(SITEMAP, params={"jurisdiction": "zz"}).status_code == 400
    assert client.get(SITEMAP).status_code == 422
    assert (
        client.get(SITEMAP, params={"jurisdiction": "oh", "limit": 0}).status_code
        == 422
    )
    assert (
        client.get(SITEMAP, params={"jurisdiction": "oh", "limit": 1001}).status_code
        == 422
    )
    assert (
        client.get(SITEMAP, params={"jurisdiction": "oh", "limit": 1000}).status_code
        == 200
    )


def test_a_jurisdiction_with_no_bills_answers_an_empty_last_page(client):
    page = client.get(SITEMAP, params={"jurisdiction": "ak"}).json()
    assert page == {"results": [], "next_after": None}


def test_the_sitemap_route_requires_the_api_key():
    from fastapi.testclient import TestClient

    from api.auth import apikey_auth
    from api.main import app

    override = app.dependency_overrides.pop(apikey_auth)
    try:
        status = TestClient(app).get(SITEMAP, params={"jurisdiction": "oh"}).status_code
    finally:
        app.dependency_overrides[apikey_auth] = override
    assert status == 403
