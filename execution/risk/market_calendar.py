"""NYSE session calendar wrapper around `exchange_calendars` (XNYS).

Single entry point for "is today a real trading day" + (Phase 4+) early
closes. Using the library avoids the class of bug where a hard-coded
table goes stale the first time a new-year maintenance edit is missed.
"""

from __future__ import annotations

from datetime import date

import exchange_calendars as xcals


_XNYS = xcals.get_calendar("XNYS")


def is_nyse_holiday(d: date) -> bool:
    """True if d is a weekday the NYSE is closed (holiday)."""
    if d.weekday() >= 5:
        return False
    return not _XNYS.is_session(d)


def is_session_day(d: date) -> bool:
    """True if d is a regular NYSE trading session."""
    return bool(_XNYS.is_session(d))
