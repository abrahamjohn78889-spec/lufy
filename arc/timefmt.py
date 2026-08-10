"""Dual time display: one instant, three renderings.

WHY THIS IS DERIVED AND NOT STORED. Every timestamp in ARC is a UTC epoch float,
and that is the only value any engine reads. IST and ET are produced here, at the
moment something is displayed, from that one number. Persisting three copies of an
instant would be three histories, and on the day a DST transition or a clock change
made them disagree the operator would be reading whichever copy their page loaded.
Deriving also keeps replay honest: a recorded run re-rendered a year later shows
the same wall clock it showed live, because the conversion is a pure function of
the epoch and the zone.

WHY THE ZONES ARE FIXED. The operator is in India; the venue's day — its open,
its close, its maintenance windows, the timestamps in its own UI — is New York.
Those two are the pair that has to be correlated during an incident, so they are
named constants rather than configuration. `ARC_TIMEZONE` remains what it always
was: the zone the log file's own lines are stamped in.

NOTHING HERE IS IN THE TRADING PATH. No window, buffer, freeze, PTB, TWAP or
countdown is computed from a value produced by this module. It formats; it does
not decide.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

__all__ = [
    "ET",
    "ET_LABEL",
    "IST",
    "IST_LABEL",
    "UTC_LABEL",
    "ZONES",
    "clocks",
    "format_in",
    "parse_at",
    "stamps",
]

# Asia/Kolkata, not a fixed +05:30 offset. India has no DST today, but a named zone
# is right for the same reason ET is: the offset is the zone's business, not ours.
IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

# Polymarket Exchange Time. America/New_York rather than a fixed -05:00, because
# the venue observes DST and a hardcoded offset would be silently an hour wrong for
# eight months of the year.
ET: Final[ZoneInfo] = ZoneInfo("America/New_York")

UTC_LABEL: Final[str] = "UTC"
IST_LABEL: Final[str] = "IST"
ET_LABEL: Final[str] = "ET"

_UTC: Final[ZoneInfo] = ZoneInfo("UTC")

_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


def format_in(ts: float | int | None, zone: ZoneInfo | None) -> str:
    """One instant in one zone, or an em dash when there is no instant.

    An absent timestamp renders as "—" rather than as the epoch. A fill time of
    1970-01-01 on an unfilled order is a value an operator has to stop and decode;
    a dash is read correctly at a glance.
    """
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, zone).strftime(_FORMAT)


def stamps(ts: float | int | None) -> dict[str, Any]:
    """The canonical epoch plus its two display renderings.

    `utc` is the number every engine already uses and the only one anything reads
    back. `utc_display`, `ist` and `et` are strings for a human. They are shipped
    together so a record cannot travel with a timestamp and arrive without its
    zones — a per-field conversion in the browser would be a second implementation
    of this function, and it would be the one that disagreed.
    """
    return {
        "utc": ts,
        "utc_display": format_in(ts, _UTC),
        "ist": format_in(ts, IST),
        "et": format_in(ts, ET),
    }


def clocks(now: float) -> dict[str, Any]:
    """The OPS Deck's live wall clocks: the same instant, three ways."""
    return stamps(now)


# The zones a filter may be expressed in, by the label the operator types.
ZONES: Final[dict[str, ZoneInfo]] = {"utc": _UTC, "ist": IST, "et": ET}


def parse_at(text: str, zone: str = "utc") -> float | None:
    """A filter bound written as a wall clock, converted to the canonical epoch.

    Accepts a bare epoch too, so an existing caller passing a number keeps working.
    Returns None for anything unparseable rather than raising: a mistyped filter
    should show unfiltered history, not a 500. The conversion happens once, here,
    and what reaches the store is the same UTC float it has always stored — a
    zone-aware filter must never rewrite the instant a record was written at.
    """
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    tz = ZONES.get(zone.strip().lower(), _UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.timestamp()
