"""
The one rule for which legislative sessions are "current" and "previous" (OPEN-467).

Nothing else in the platform should decide this again: ddp-sync, the nightly scraper
script, the broker and the website read the `is_current` / `is_previous` flags that
/jurisdictions/{id}?include=legislative_sessions publishes from here.

Rule (decided by Ramon, 2026-10-31: an active special session counts as current):

* A session that has not started (start_date after today, or no usable start_date) is never
  current or previous.
* A session is *active* when it has started and has not ended (a blank end_date is open-ended).
* Current = every active session, plus, when no REGULAR session is active (between sessions),
  the newest regular session that has started. So a state between sessions still has a current
  session, and a special session that is active in the gap does not hide the regular one.
* Previous = the newest started regular session that began before the current regular session
  and is not itself current (if two regular sessions overlap and are both active, both are
  current and the one before them is previous, so a session is never both flags). There is none
  when no regular session is current.
* A blank end_date, or one that is not a date, is open-ended (no tracked jurisdiction has a
  non-date value today; the column is free text).

"Regular" is `classification == "primary"`. Virginia and Washington leave classification
blank; there a blank classification is regular unless the identifier ends in a special-session
suffix: VA's "2026S1" (a digit, then S or E, then digits) or its older "2018specialI".
"""
import datetime
import re
from typing import Dict, List, Optional, Sequence, Tuple

# VA 2026S1, NC 2025E1, UT 2025S2, and VA's older "2018specialI" (blank classification there)
_SPECIAL_SUFFIX = re.compile(r"\d[SsEe]\d+$|special", re.IGNORECASE)


def _parse_date(value: Optional[str]) -> Optional[datetime.date]:
    """ISO date, or None when blank or not a date (the column is free text)."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value[:10])
    except ValueError:
        return None


def is_regular(identifier: str, classification: Optional[str]) -> bool:
    if classification:
        return classification == "primary"
    return not _SPECIAL_SUFFIX.search(identifier or "")


def session_flags(
    sessions: Sequence, today: Optional[datetime.date] = None
) -> Dict[str, Tuple[bool, bool]]:
    """
    sessions: objects with identifier, classification, start_date, end_date (one jurisdiction).
    Returns {identifier: (is_current, is_previous)} for every session.
    """
    today = today or datetime.date.today()
    started: List[Tuple[datetime.date, Optional[datetime.date], str, bool]] = []
    for s in sessions:
        start = _parse_date(s.start_date)
        if start is not None and start <= today:
            started.append(
                (
                    start,
                    _parse_date(s.end_date),
                    s.identifier,
                    is_regular(s.identifier, s.classification),
                )
            )

    def active(entry):
        end = entry[1]
        return end is None or end >= today

    # newest start first; identifier breaks a tie so the answer does not depend on row order
    started.sort(key=lambda e: (e[0], e[2]), reverse=True)

    current = {e[2] for e in started if active(e)}
    regular = [e for e in started if e[3]]
    active_regular = [e for e in regular if active(e)]
    current_regular = (active_regular or regular[:1] or [None])[0]
    if current_regular:
        # an active regular session is already in `current`; this adds the fallback
        current.add(current_regular[2])

    previous = None
    if current_regular:
        earlier = [
            e for e in regular if e[0] < current_regular[0] and e[2] not in current
        ]
        previous = earlier[0][2] if earlier else None

    return {
        s.identifier: (s.identifier in current, s.identifier == previous)
        for s in sessions
    }
