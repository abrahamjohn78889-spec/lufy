"""The countdown: floored, never negative, and one clock behind both timers.

Every failure here is one the operator would read as fact. A ceiling shows 05:00
for two different seconds and disagrees with the Polymarket page beside it. A
negative timer reads as a market that is somehow still open after close. And two
timers computed from two sources drift, which is the one thing the two-timer
requirement explicitly forbids.

The browser half is checked by reading `app.js`: there is no JS runtime here, so
the invariants that matter — one skew, one countdown call, both timers written
from it — are asserted structurally.
"""

from __future__ import annotations

import re
from pathlib import Path

from arc.domain.timing import MARKET_DURATION_SECONDS, format_countdown

_WEB = Path(__file__).resolve().parent.parent / "arc" / "web"
_APP_JS = (_WEB / "app.js").read_text(encoding="utf-8")
_INDEX = (_WEB / "index.html").read_text(encoding="utf-8")

_CLOSE = 1_754_400_300


class TestFloored:
    def test_the_whole_299th_second_reads_04_59(self) -> None:
        """Polymarket floors. A round would show 05:00 twice and then skip 04:59."""
        assert format_countdown(_CLOSE - 299.999, _CLOSE) == "04:59"
        assert format_countdown(_CLOSE - 299.5, _CLOSE) == "04:59"
        assert format_countdown(_CLOSE - 299.001, _CLOSE) == "04:59"

    def test_a_full_market_reads_05_00_only_at_the_boundary(self) -> None:
        assert format_countdown(_CLOSE - MARKET_DURATION_SECONDS, _CLOSE) == "05:00"
        assert format_countdown(_CLOSE - 300.001, _CLOSE) == "05:00"

    def test_every_second_of_a_market_is_mm_ss(self) -> None:
        pattern = re.compile(r"^[0-9]{2}:[0-9]{2}$")
        for whole in range(MARKET_DURATION_SECONDS + 1):
            assert pattern.match(format_countdown(_CLOSE - whole, _CLOSE))

    def test_each_second_is_shown_exactly_once(self) -> None:
        """A duplicated or skipped label is a timer that visibly disagrees."""
        seen = [format_countdown(_CLOSE - w - 0.5, _CLOSE) for w in range(MARKET_DURATION_SECONDS)]
        assert len(set(seen)) == len(seen)


class TestNeverNegative:
    def test_at_close_it_is_zero(self) -> None:
        assert format_countdown(float(_CLOSE), _CLOSE) == "00:00"

    def test_after_close_it_stays_zero_through_settlement(self) -> None:
        """A market closes, then settles. A negative timer there reads as still open."""
        for past in (0.001, 1.0, 30.0, 300.0, 86_400.0):
            assert format_countdown(_CLOSE + past, _CLOSE) == "00:00"

    def test_no_minus_sign_is_ever_produced(self) -> None:
        assert "-" not in format_countdown(_CLOSE + 5000.0, _CLOSE)


class TestTheBrowserHalfCannotDrift:
    def test_both_timers_are_written_from_one_countdown_call(self) -> None:
        """Two calls would be two clocks, and two clocks drift."""
        body = _APP_JS.split("function tickTimers()", 1)[1].split("\n}", 1)[0]
        assert body.count("countdown(") == 1
        assert "#timer1" in body and "#timer2" in body

    def test_there_is_exactly_one_timer_of_each_id(self) -> None:
        assert _INDEX.count('id="timer1"') == 1
        assert _INDEX.count('id="timer2"') == 1

    def test_timer_two_lives_in_the_signal_tank(self) -> None:
        """The spec places timer 1 on the OPS Deck and timer 2 inside Signal Tank."""
        ops = _INDEX.split('id="ws-ops"', 1)[1].split('id="ws-tank"', 1)[0]
        tank = _INDEX.split('id="ws-tank"', 1)[1].split('id="ws-ledger"', 1)[0]
        assert 'id="timer1"' in ops and 'id="timer2"' not in ops
        assert 'id="timer2"' in tank and 'id="timer1"' not in tank

    def test_the_skew_is_measured_from_the_server_frame(self) -> None:
        """Without this the countdown runs on the browser's own clock, which drifts."""
        assert "clockSkew = state.ts - Date.now() / 1000" in _APP_JS

    def test_the_js_countdown_floors_and_clamps(self) -> None:
        body = _APP_JS.split("function countdown(", 1)[1].split("\n}", 1)[0]
        assert "Math.max(0," in body
        assert "Math.floor" in body
