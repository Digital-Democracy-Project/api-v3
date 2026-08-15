"""Bill-version chronological ordering (OPEN-92) — a deliberate, explicitly-
synced copy of openstates-core's canonical implementation
(`openstates/utils/version_ordering.py`, OPEN-91:
https://github.com/Digital-Democracy-Project/openstates-core), not a
reinvention of it.

Why a copy and not a real dependency: api-v3 installs the `openstates`
package straight from PyPI (`pyproject.toml`'s `openstates = "^6.7.0"`,
built via `deploy/Dockerfile.ddp`'s plain `poetry install`) rather than from
the DDP openstates-core fork this logic actually lives in, so
`openstates.utils.version_ordering` is not importable here today. Re-pinning
that dependency to the fork (so this file can be deleted in favor of a real
import) is tracked separately as a known follow-up — see OPEN-92's own
ticket description — since it's a materially bigger, higher-risk change
(a production dependency re-pin needs `poetry lock` to regenerate, which
requires tooling this fix did not have available) than the ordering bug
this file exists to fix.

**Keep this byte-for-byte identical to openstates-core's copy.** If you're
changing the stage table/regex patterns here, you're almost certainly
fixing a bug that belongs in openstates-core first — port it there, then
copy it back here, not the other way around.
"""

from __future__ import annotations

import re
import typing

STAGE_INTRODUCED = 0
STAGE_AMENDMENT = 1
STAGE_CHAMBER_PASSAGE = 2
STAGE_FINAL_PASSAGE = 3
STAGE_ENACTED = 4
STAGE_UNKNOWN = 99  # excluded from diff lineage entirely -- see version_sort_key()

_ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}

_DATE_RE = re.compile(r"\A\d{4}(-\d{2}(-\d{2})?)?\Z")


def extract_ordinal(note: str) -> float:
    """
    Best-effort numeric ordinal embedded in a version_note, used to rank same-stage numbered
    variants against each other (MI's "Substitute (S-2)", UT's "Substitute #3", WA's "Second
    Substitute", FL's "c2"/"e2"). 0.0 if no ordinal is found -- the unnumbered/first-of-its-
    kind case (FL's "c1", WA's plain "Substitute Bill" with no ordinal word).

    MI's "(S-1)"/(H-2)" parenthesized number is checked first and takes priority over a
    trailing "- N" suffix on the same note (a second file for that *same* substitute stage,
    e.g. "Substitute (S-1) - 2" -- a minor tiebreak, not a different amendment stage; folded
    in as a small fraction so it sorts immediately after "Substitute (S-1)" rather than being
    conflated with "Substitute (S-2)").
    """
    lowered = note.lower()

    paren = re.search(r"\([sh]-(\d+)\)", lowered)
    if paren:
        base = float(paren.group(1))
        tail = re.search(r"\)\s*-\s*(\d+)\s*\Z", lowered)
        return base + (int(tail.group(1)) / 100.0 if tail else 0.0)

    for word, value in _ORDINAL_WORDS.items():
        if word in lowered:
            return float(value)

    m = (
        re.search(r"#\s*(\d+)\b", note)
        or re.search(r"\b[a-z](\d+)\b", lowered)
        or re.search(r"(\d+)\s*\Z", note)
    )
    if m:
        return float(m.group(1))
    return 0.0


def note_stage(note: str) -> tuple:
    """
    Classify a version_note into (stage, ordinal) using the content-based stage table built
    from the OPEN-34 audit (see openstates-core's version_ordering.py module docstring for the
    full audit this encodes). Never looks at DB order or position -- purely a function of the
    note text itself, so it's stable no matter what order versions are walked in or what row
    order Postgres happens to return.
    """
    lowered = note.lower()

    if re.search(
        r"public act|public law|\bchapter|passed legislature|concurred", lowered
    ):
        return (STAGE_ENACTED, extract_ordinal(note))

    if "veto" in lowered:
        return (STAGE_FINAL_PASSAGE, 3.0)
    if "reenroll" in lowered:
        return (STAGE_FINAL_PASSAGE, 2.0)
    if "governor" in lowered:
        return (STAGE_FINAL_PASSAGE, 1.0)
    if "enroll" in lowered or re.search(r"\ber\b", lowered):
        return (STAGE_FINAL_PASSAGE, 0.0)

    if re.match(r"(senate|house)\s*-", lowered):
        return (STAGE_CHAMBER_PASSAGE, 0.5)

    if re.search(r"\be\d+\b", lowered) and not re.search(
        r"substitute|committee", lowered
    ):
        return (STAGE_CHAMBER_PASSAGE, extract_ordinal(note))

    if "engross" in lowered and not re.search(r"substitute|committee", lowered):
        return (STAGE_CHAMBER_PASSAGE, extract_ordinal(note))

    if re.search(r"conference|\breport|\breferr|placed on calendar|as passed", lowered):
        return (STAGE_CHAMBER_PASSAGE, extract_ordinal(note) + 0.25)

    if re.search(r"substitute|amend|comparison|\bc\d+\b", lowered):
        if "engross" in lowered:
            return (STAGE_AMENDMENT, extract_ordinal(note) + 0.5)
        return (STAGE_AMENDMENT, extract_ordinal(note))

    if re.search(r"introduced|\bfiled\b|\bpb\b|original|^bill$", lowered):
        return (STAGE_INTRODUCED, extract_ordinal(note))

    if lowered == "bill text":
        return (STAGE_INTRODUCED, 0.0)

    return (STAGE_UNKNOWN, 0.0)


def version_sort_key(note: str, date: typing.Optional[str]) -> tuple:
    """
    Rank a single version (by its note + date) for chronological ordering, without ever
    trusting the order it was returned from the DB in. See openstates-core's
    version_ordering.py module docstring for the audit this encodes.

    Returns (stage, date-or-empty, ordinal). The macro stage always comes from the note (see
    note_stage()) -- a real, parseable date is used only as a same-stage tiebreaker, not as
    an override of the note-based stage.

    A note matching none of the known patterns returns stage STAGE_UNKNOWN -- the caller
    excludes those versions from "latest"/"previous" selection entirely rather than guessing
    a position for them.
    """
    stage, ordinal = note_stage(note)
    has_date = bool(date) and bool(_DATE_RE.match(date))
    return (stage, date if has_date else "", ordinal)
