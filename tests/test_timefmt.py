"""Dual time display. One instant, three renderings, and nothing in the trading path.

The whole feature is a pure function, so it is tested as one: a known epoch has
exactly one correct rendering in each zone, and the interesting cases are the two
DST boundaries the venue crosses and the absent timestamp an unfilled order carries.

The last class is the one that matters most. Dual time is presentation metadata;
if an engine ever reads a derived string, the feature has stopped being cosmetic.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from arc.timefmt import ET, IST, clocks, format_in, parse_at, stamps

# 2026-08-06 21:34:12 IST, chosen because it is the instant in the addendum's own
# worked example: IST 21:34:12 / ET 12:04:12, a 9h30m gap during EDT.
EXAMPLE: float = datetime(2026, 8, 6, 21, 34, 12, tzinfo=IST).timestamp()

# 2026-01-15 12:00:00 UTC — winter, so New York is on EST (-05:00).
WINTER: float = datetime(2026, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")).timestamp()

# 2026-07-15 12:00:00 UTC — summer, so New York is on EDT (-04:00).
SUMMER: float = datetime(2026, 7, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")).timestamp()


class TestTheAddendumsWorkedExample:
    def test_ist_is_what_the_operator_was_promised(self) -> None:
        assert stamps(EXAMPLE)["ist"] == "2026-08-06 21:34:12"

    def test_et_is_the_venues_own_clock(self) -> None:
        assert stamps(EXAMPLE)["et"] == "2026-08-06 12:04:12"

    def test_utc_is_carried_unchanged(self) -> None:
        # The canonical value is the float, not a rendering of it.
        assert stamps(EXAMPLE)["utc"] == EXAMPLE


class TestDaylightSaving:
    """ET is a named zone, not a fixed offset. A hardcoded -05:00 would be an hour
    wrong for eight months of the year, and every venue timestamp with it."""

    def test_new_york_is_five_behind_in_january(self) -> None:
        assert format_in(WINTER, ET) == "2026-01-15 07:00:00"

    def test_new_york_is_four_behind_in_july(self) -> None:
        assert format_in(SUMMER, ET) == "2026-07-15 08:00:00"

    def test_india_does_not_shift(self) -> None:
        assert format_in(WINTER, IST) == "2026-01-15 17:30:00"
        assert format_in(SUMMER, IST) == "2026-07-15 17:30:00"


class TestTheAbsentTimestamp:
    """An unfilled order has no fill time. 1970-01-01 is a value the operator has to
    stop and decode; a dash is read correctly at a glance."""

    def test_none_renders_as_a_dash(self) -> None:
        assert format_in(None, IST) == "—"

    def test_every_field_of_an_absent_stamp_is_a_dash(self) -> None:
        absent = stamps(None)
        assert absent["utc"] is None
        assert absent["utc_display"] == absent["ist"] == absent["et"] == "—"

    def test_zero_is_an_instant_not_an_absence(self) -> None:
        # Epoch zero is a legal reading and must not be confused with "no value".
        assert stamps(0.0)["utc_display"] == "1970-01-01 00:00:00"


class TestTheShape:
    def test_stamps_ships_exactly_four_keys(self) -> None:
        assert set(stamps(EXAMPLE)) == {"utc", "utc_display", "ist", "et"}

    def test_clocks_is_stamps(self) -> None:
        # One conversion utility, per the acceptance criteria. Not two that drift.
        assert clocks(EXAMPLE) == stamps(EXAMPLE)

    def test_the_format_is_sortable(self) -> None:
        for key in ("utc_display", "ist", "et"):
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", stamps(EXAMPLE)[key])


class TestFilterBounds:
    """`?since=`/`?until=` may be written in the zone the operator is reading in.
    What reaches the store is still the canonical epoch."""

    def test_a_bare_epoch_still_works(self) -> None:
        assert parse_at("1750000000") == 1750000000.0

    def test_a_wall_clock_is_read_in_the_named_zone(self) -> None:
        assert parse_at("2026-08-06 21:34:12", "ist") == EXAMPLE
        assert parse_at("2026-08-06 12:04:12", "et") == EXAMPLE

    def test_the_same_text_in_two_zones_is_two_instants(self) -> None:
        assert parse_at("2026-08-06 12:00:00", "ist") != parse_at("2026-08-06 12:00:00", "et")

    def test_an_explicit_offset_beats_the_zone_parameter(self) -> None:
        assert parse_at("2026-08-06T21:34:12+05:30", "et") == EXAMPLE

    def test_empty_means_unfiltered(self) -> None:
        assert parse_at("") is None
        assert parse_at("   ") is None

    def test_a_mistyped_bound_shows_everything_rather_than_five_hundred(self) -> None:
        assert parse_at("last tuesday") is None

    def test_an_unknown_zone_falls_back_to_utc(self) -> None:
        assert parse_at("2026-08-06 12:00:00", "mars") == parse_at("2026-08-06 12:00:00", "utc")


class TestNothingInTheTradingPathReadsIt:
    """The addendum's hard constraint: display and audit only. If a decision, risk,
    window, strategy or execution module imports the formatter, this fails."""

    def test_no_engine_imports_the_formatter(self) -> None:
        root = Path(__file__).resolve().parents[1] / "arc"
        offenders = [
            path.relative_to(root).as_posix()
            for package in ("decision", "risk", "windows", "strategy", "execution", "domain")
            for path in (root / package).rglob("*.py")
            if "timefmt" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []
