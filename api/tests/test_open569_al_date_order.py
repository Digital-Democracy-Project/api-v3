"""OPEN-569: Alabama versions are ordered by date (openstates-core's rule, imported since
OPEN-538). The notes and dates are HB 315's (2026RS) as the OPEN-569 Alabama scraper dates them."""

from unittest import mock

from api import bills
from openstates.utils.version_ordering import version_sort_key

HB315 = [
    ("Introduced", "2026-01-22"),
    ("Tillman 1st Substitute Offered", "2026-03-17"),
    ("Engrossed", "2026-03-17"),
    ("Jefferson County Legislation 1st Substitute", "2026-04-02"),
    ("Coleman 1st Amendment Offered", "2026-04-07"),
    ("Enrolled", "2026-04-08"),
]


def _key(note, date, state):
    return version_sort_key(note, date, (), "house", state)


def test_alabama_orders_hb315_by_date():
    assert sorted(reversed(HB315), key=lambda v: _key(*v, "Alabama")) == HB315


def test_other_states_keep_the_stage_first_key():
    order = [n for n, _ in sorted(HB315, key=lambda v: _key(*v, "Ohio"))]
    assert order.index("Jefferson County Legislation 1st Substitute") < order.index(
        "Engrossed"
    )
    for note, date in HB315:
        assert _key(note, date, "Ohio") == version_sort_key(note, date, (), "house")


def test_bill_detail_passes_the_jurisdiction_name(client):
    with mock.patch.object(
        bills, "version_sort_key", wraps=bills.version_sort_key
    ) as key:
        response = client.get("/bills/oh/2021/HB 9105?include=versions")
    assert response.status_code == 200
    assert key.call_args_list
    assert {c.args[4] for c in key.call_args_list} == {"Ohio"}
