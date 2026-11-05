"""OPEN-493: GET /bills allows 50 a page when no include= is asked for, and keeps 20 with one."""
import pytest

from .test_bills_newest_action import _add_bill, _delete_bill

BASE = "/bills?sort=latest_action_desc"


@pytest.fixture
def fifty_two_bills():
    """52 bills with action dates in 2099, so they are the newest 52 of the list, in the order they are returned."""
    ids = [_add_bill(f"PGSZ {i}", f"2099-12-31T23:59:{59 - i:02d}") for i in range(52)]
    yield ids
    for bill_id in ids:
        _delete_bill(bill_id)


def _page(client, per_page, extra="", page=1):
    return client.get(f"{BASE}&per_page={per_page}&page={page}{extra}")


def test_fifty_is_one_page_of_the_same_list(client, fifty_two_bills):
    body = _page(client, 50).json()
    twenty = _page(client, 20).json()
    # the dates the fixture set decide the order, not the code under test
    assert [b["id"] for b in body["results"]] == fifty_two_bills[:50]
    assert [b["id"] for b in body["results"][:20]] == [
        b["id"] for b in twenty["results"]
    ]
    total = twenty["pagination"]["total_items"]
    assert body["pagination"] == {
        "per_page": 50,
        "page": 1,
        "total_items": total,
        "max_page": -(-total // 50),
    }
    # page 2 of 50 continues the list: no repeats, the remaining fixture bills first
    second = [b["id"] for b in _page(client, 50, page=2).json()["results"]]
    assert second[:2] == fifty_two_bills[50:52]
    assert not set(second) & set(fifty_two_bills[:50])


@pytest.mark.parametrize("per_page", [1, 7, 20])
def test_twenty_and_under_answer_as_before(client, fifty_two_bills, per_page):
    r = _page(client, per_page)
    assert r.status_code == 200
    assert [b["id"] for b in r.json()["results"]] == fifty_two_bills[:per_page]
    assert r.json()["pagination"]["per_page"] == per_page


def test_more_than_fifty_is_refused_showing_the_range(client, fifty_two_bills):
    r = _page(client, 51)
    assert r.status_code == 400
    assert "invalid per_page, must be in [1, 50]" in r.json()["detail"]
    assert _page(client, 0).status_code == 400


def test_an_include_keeps_the_limit_at_twenty(client, fifty_two_bills):
    r = _page(client, 50, "&include=sponsorships")
    assert r.status_code == 400
    assert "invalid per_page, must be in [1, 20]" in r.json()["detail"]
    ok = _page(client, 20, "&include=sponsorships")
    assert ok.status_code == 200
    assert len(ok.json()["results"]) == 20
    # a repeated include= is still an include; an unknown one is refused, it never earns the bigger page
    repeated = _page(client, 50, "&include=sponsorships&include=actions")
    assert repeated.status_code == 400
    assert "must be in [1, 20]" in repeated.json()["detail"]
    assert _page(client, 50, "&include=nonsense").status_code == 422


def test_the_default_page_size_is_unchanged(client, fifty_two_bills):
    body = client.get(BASE).json()
    assert body["pagination"]["per_page"] == 10
    assert len(body["results"]) == 10


def test_other_lists_keep_their_own_limits(client):
    committees = client.get("/committees?jurisdiction=ne&per_page=21")
    assert committees.status_code == 400
    assert "must be in [1, 20]" in committees.json()["detail"]
    assert client.get("/jurisdictions?per_page=52").status_code == 200
    assert client.get("/jurisdictions?per_page=53").status_code == 400
