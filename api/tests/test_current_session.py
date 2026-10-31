import datetime
from collections import namedtuple

import pytest

from ..current_session import is_regular, session_flags

S = namedtuple("S", "identifier classification start_date end_date")

# Real session lists as read from the Mac's data service on 2026-10-30 (newest rows only).
# A fixed "today" is passed to every call so these tests do not rot.
REAL = {
    "us": [
        S("119", "primary", "2025-01-03", "2027-01-03"),
        S("118", "primary", "2023-01-03", "2025-01-03"),
        S("117", "primary", "2021-01-03", "2023-01-03"),
    ],
    "al": [
        S("2026rs", "primary", "2026-01-13", "2026-04-02"),
        S("2025rs", "primary", "2025-02-04", "2025-05-15"),
        S("2023s1", "special", "2023-03-08", "2023-03-14"),
        S("2023rs", "primary", "2023-03-07", "2023-06-08"),
    ],
    "az": [
        S("57th-2nd-regular", "primary", "2026-01-12", "2026-04-25"),
        S("57th-1st-regular", "primary", "2025-01-13", "2025-04-26"),
        S("56th-2nd-regular", "primary", "2024-01-08", "2024-04-20"),
    ],
    "fl": [
        S("2027", "primary", "2027-01-13", "2027-03-13"),
        S("2026F", "special", "2026-06-01", "2026-06-05"),
        S("2026E", "special", "2026-05-12", "2026-05-29"),
        S("2026", "primary", "2026-01-13", "2026-03-13"),
        S("2025", "primary", "2025-03-04", "2025-05-02"),
    ],
    "ma": [
        S("194th", "primary", "2025-01-01", "2026-07-31"),
        S("193rd", "primary", "2023-01-04", "2024-07-31"),
    ],
    "mi": [
        S("2025-2026", "primary", "2025-01-08", "2026-12-31"),
        S("2023-2024", "primary", "2023-01-11", "2024-12-22"),
    ],
    "nc": [
        S("2025E1", "special", "2025-11-17", "2025-12-01"),
        S("2025", "primary", "2025-01-11", "2026-07-01"),
        S("2023", "primary", "2023-01-11", "2024-07-01"),
    ],
    "ut": [
        S("2027", "primary", "2027-01-20", "2027-03-06"),
        S("2026", "primary", "2026-01-20", "2026-03-06"),
        S("2025S2", "special", "2025-12-09", "2025-12-12"),
        S("2025", "primary", "2025-01-21", "2025-03-17"),
    ],
    # Virginia leaves classification blank, special sessions included (2026S1)
    "va": [
        S("2027", "", "2027-01-13", "2027-02-27"),
        S("2026S1", "", "2026-04-23", "2026-05-01"),
        S("2026", "", "2026-01-14", "2026-03-14"),
        S("2025", "", "2025-01-08", "2025-02-22"),
    ],
    "wa": [
        S("2025-2026", "primary", "2025-01-13", "2026-03-06"),
        S("2023-2024", "primary", "2023-01-09", "2024-03-07"),
        S("2019-2020", "", "2019-01-14", "2020-03-12"),
    ],
}

TODAY = datetime.date(2026, 10, 30)


def flagged(state, today=TODAY):
    flags = session_flags(REAL[state], today)
    current = sorted(i for i, (c, _) in flags.items() if c)
    previous = sorted(i for i, (_, p) in flags.items() if p)
    return current, previous


@pytest.mark.parametrize(
    "state,current,previous",
    [
        ("us", ["119"], ["118"]),
        ("mi", ["2025-2026"], ["2023-2024"]),
        ("ut", ["2026"], ["2025"]),
        ("ma", ["194th"], ["193rd"]),
        ("nc", ["2025"], ["2023"]),
        ("al", ["2026rs"], ["2025rs"]),
        ("az", ["57th-2nd-regular"], ["57th-1st-regular"]),
        ("fl", ["2026"], ["2025"]),
        ("va", ["2026"], ["2025"]),
        ("wa", ["2025-2026"], ["2023-2024"]),
    ],
)
def test_expected_table_between_sessions(state, current, previous):
    # 2026-10-30: most legislatures are between sessions, so "current" is the newest regular
    # session that has started, and a session that has not started (UT/FL/VA 2027) never is.
    assert flagged(state) == (current, previous)


def test_session_that_has_not_started_is_never_current_or_previous():
    for state, sessions in REAL.items():
        flags = session_flags(sessions, TODAY)
        for s in sessions:
            if s.start_date > TODAY.isoformat():
                assert flags[s.identifier] == (False, False), (state, s.identifier)


def test_active_special_session_is_current_alongside_the_regular_one():
    # FL special 2026F is active 2026-06-02; regular 2026 ended 03-13, so it is the fallback.
    assert flagged("fl", datetime.date(2026, 6, 2)) == (["2026", "2026F"], ["2025"])
    # A special session is still active on its last day.
    assert flagged("fl", datetime.date(2026, 6, 5)) == (["2026", "2026F"], ["2025"])
    # Ended specials stop counting the next day.
    assert flagged("fl", datetime.date(2026, 6, 6)) == (["2026"], ["2025"])


def test_two_sessions_active_at_once_are_both_current():
    # Virginia's 2026S1 (blank classification, identifier suffix) runs inside the year.
    assert flagged("va", datetime.date(2026, 4, 25)) == (["2026", "2026S1"], ["2025"])
    # UT: regular 2026 active and a special overlapping it (synthetic overlap on real rows).
    sessions = REAL["ut"] + [S("2026S1", "special", "2026-02-01", "2026-02-05")]
    flags = session_flags(sessions, datetime.date(2026, 2, 2))
    assert flags["2026"] == (True, False) and flags["2026S1"] == (True, False)
    assert flags["2025"] == (False, True)


def test_active_regular_session_is_current_before_its_end_date():
    assert flagged("us", datetime.date(2026, 6, 1)) == (["119"], ["118"])
    assert flagged("ma", datetime.date(2026, 7, 31)) == (["194th"], ["193rd"])


def test_a_regular_session_becomes_current_on_its_start_date():
    assert flagged("ut", datetime.date(2027, 1, 19)) == (["2026"], ["2025"])
    assert flagged("ut", datetime.date(2027, 1, 20)) == (["2027"], ["2026"])


def test_blank_classification_special_is_not_regular():
    assert is_regular("2026", "") is True
    assert is_regular("2026S1", "") is False
    assert is_regular("2025E1", "") is False
    assert is_regular("2019-2020", "") is True
    # Virginia's older specials (blank classification, real identifiers)
    assert is_regular("2018specialI", "") is False
    assert is_regular("2011specialI", "") is False
    # a stated classification always wins over the identifier shape
    assert is_regular("2023s1", "special") is False
    assert is_regular("2025S1", "primary") is True


def test_blank_or_bad_dates_do_not_crash_and_never_count_as_started():
    sessions = [
        S("old", "primary", "2020-01-01", ""),
        S("nodate", "primary", "", ""),
        S("junk", "primary", "soon", "later"),
    ]
    flags = session_flags(sessions, TODAY)
    # a blank end date is open-ended, so "old" is active and current
    assert flags == {
        "old": (True, False),
        "nodate": (False, False),
        "junk": (False, False),
    }


def test_a_date_that_is_not_a_date_in_end_date_is_open_ended():
    # documented behaviour: no tracked jurisdiction has one today, so this pins the choice
    flags = session_flags([S("x", "primary", "2025-01-01", "TBD")], TODAY)
    assert flags == {"x": (True, False)}


def test_old_va_special_with_blank_classification_is_never_previous():
    sessions = REAL["va"] + [S("2020specialI", "", "2020-08-18", "2020-11-10")]
    assert session_flags(sessions, TODAY)["2020specialI"] == (False, False)


def test_no_started_session_gives_no_flags():
    flags = session_flags([S("2030", "primary", "2030-01-01", "2030-02-01")], TODAY)
    assert flags == {"2030": (False, False)}
    assert session_flags([], TODAY) == {}


def test_answer_does_not_depend_on_row_order():
    rows = REAL["va"]
    assert session_flags(rows, TODAY) == session_flags(list(reversed(rows)), TODAY)


def test_endpoint_publishes_flags_only_when_sessions_are_included(client):
    # fixture jurisdiction "Nebraska": 2020 (ended) and 2021 (no end date, so still active)
    detail = client.get(
        "/jurisdictions/ocd-jurisdiction/country:us/state:ne/government"
        "?include=legislative_sessions"
    ).json()
    flags = {
        s["identifier"]: (s["is_current"], s["is_previous"])
        for s in detail["legislative_sessions"]
    }
    assert flags == {"2021": (True, False), "2020": (False, True)}
    listing = client.get(
        "/jurisdictions?classification=state&include=legislative_sessions"
    )
    for j in listing.json()["results"]:
        for s in j["legislative_sessions"]:
            assert isinstance(s["is_current"], bool)
            assert isinstance(s["is_previous"], bool)
    plain = client.get("/jurisdictions/ocd-jurisdiction/country:us/state:ne/government")
    assert "legislative_sessions" not in plain.json()


def test_previous_is_never_also_current():
    # two regular sessions active at once (overlap): both are current, so neither is "previous"
    sessions = [
        S("A", "primary", "2025-01-01", "2027-12-31"),
        S("B", "primary", "2026-01-01", "2027-12-31"),
        S("C", "primary", "2023-01-01", "2024-12-31"),
    ]
    flags = session_flags(sessions, TODAY)
    assert flags["A"] == (True, False)
    assert flags["B"] == (True, False)
    assert flags["C"] == (False, True)
