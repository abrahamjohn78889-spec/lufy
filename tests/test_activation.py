"""Window activation: level-triggered, millisecond-exact, and never a timer.

The defect every test here guards against is a LOST window. A window that opens late
trades on a slightly older TWAP; a window that never opens produces no signal at all and
nothing anywhere reports it. So the tests below check three separate things:

    1  the instant is exactly close_ts - offset, to the millisecond
    2  a pass that arrives LATE still opens the window (level, not edge)
    3  no scheduling API is used anywhere in arc/windows/, checked by AST walk

(3) is structural rather than behavioural on purpose. A timer-based implementation can
pass every behavioural test on an idle event loop and fail only under the load that
matters. The absence of the API is the only check that holds under load.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import CLOSE_TS, OFFSETS, WINDOW_TS

from arc.domain.enums import MarketPhase, WindowState
from arc.domain.models import MarketInstance, Observation
from arc.domain.timing import activation_ts
from arc.windows.activation import (
    due_windows,
    is_activatable,
    next_activation_ts,
    window_is_due,
)


def _market(*, ptb: str = "64000.00", ticks: int = 1) -> MarketInstance:
    market = MarketInstance.create(WINDOW_TS, OFFSETS)
    market.phase = MarketPhase.ACTIVE
    market.freeze_ptb(ptb)
    for _ in range(ticks):
        market.add_observation(Observation(ts=float(WINDOW_TS), price=Decimal("64000.00")))
    return market


class TestActivationInstantIsExact:
    """Criterion 1: every window opens exactly at close_ts - offset."""

    @pytest.mark.parametrize("offset", OFFSETS)
    def test_activation_ts_is_close_minus_offset(self, offset: int) -> None:
        assert activation_ts(CLOSE_TS, offset) == CLOSE_TS - offset

    @pytest.mark.parametrize("offset", OFFSETS)
    def test_not_due_one_millisecond_early(self, offset: int) -> None:
        market = _market()
        window = market.window(offset)
        assert not window_is_due(window, CLOSE_TS, activation_ts(CLOSE_TS, offset) - 0.001)

    @pytest.mark.parametrize("offset", OFFSETS)
    def test_due_exactly_at_the_instant(self, offset: int) -> None:
        """The comparison is >=, so the instant itself opens the window."""
        market = _market()
        window = market.window(offset)
        assert window_is_due(window, CLOSE_TS, float(activation_ts(CLOSE_TS, offset)))

    @pytest.mark.parametrize("offset", OFFSETS)
    def test_due_one_millisecond_late(self, offset: int) -> None:
        market = _market()
        window = market.window(offset)
        assert window_is_due(window, CLOSE_TS, activation_ts(CLOSE_TS, offset) + 0.001)

    def test_windows_come_due_in_descending_offset_order_over_time(self) -> None:
        """15s becomes due first in wall-clock terms; 3s last. Both directions matter.

        The list is ordered by PRIORITY (3, 5, 7, 10, 15) but the 15s window is the one
        that opens earliest in time. Conflating the two orderings would open windows in
        the wrong sequence near close.
        """
        market = _market()
        seen: list[int] = []
        for offset in sorted(OFFSETS, reverse=True):
            now = float(activation_ts(CLOSE_TS, offset))
            for window in due_windows(market, now):
                if window.offset_seconds not in seen:
                    seen.append(window.offset_seconds)
            # Mark them frozen-equivalent by hand so the next step reports only new ones.
            for window in due_windows(market, now):
                window.state = WindowState.FROZEN
        assert seen == [15, 10, 7, 5, 3]


class TestPriorityOrder:
    """Criterion 7: evaluation and activation order is 3 -> 5 -> 7 -> 10 -> 15."""

    def test_due_windows_are_returned_in_priority_order(self) -> None:
        market = _market()
        # Past every activation instant: all five are due at once.
        due = due_windows(market, float(CLOSE_TS - 1))
        assert [w.offset_seconds for w in due] == [3, 5, 7, 10, 15]


class TestBusyLoopCannotSkipWindows:
    """Criterion 2: a pass 200 ms late still opens the window."""

    def test_a_pass_200ms_late_still_opens_the_window(self) -> None:
        market = _market()
        window = market.window(10)
        late = activation_ts(CLOSE_TS, 10) + 0.200
        assert window_is_due(window, CLOSE_TS, late)

    def test_a_pass_that_skips_the_instant_entirely_still_opens_it(self) -> None:
        """No pass ever lands on the instant. A level check does not care.

        This is the exact scenario a timer loses: the loop was blocked straight through
        close_ts - 15 and resumed afterwards. A scheduled callback for that instant has
        already been missed and will never fire again.
        """
        market = _market()
        before = activation_ts(CLOSE_TS, 15) - 0.5
        after = activation_ts(CLOSE_TS, 15) + 3.0
        window = market.window(15)
        assert not window_is_due(window, CLOSE_TS, before)
        assert window_is_due(window, CLOSE_TS, after)

    def test_all_five_open_on_one_very_late_pass(self) -> None:
        """A process that resumes one second before close opens every window at once.

        Better than losing four of them: each still freezes at the TWAP it can see, and
        the trigger it locks is honest about the instant it was locked at.
        """
        market = _market()
        due = due_windows(market, float(CLOSE_TS - 1))
        assert len(due) == len(OFFSETS)


class TestWindowNeverUnOpens:
    """Criterion 12/14: activation is one-way."""

    def test_a_frozen_window_is_never_due_again(self) -> None:
        market = _market()
        window = market.window(10)
        window.state = WindowState.FROZEN
        assert not window_is_due(window, CLOSE_TS, float(CLOSE_TS - 1))

    def test_a_fired_window_is_never_due_again(self) -> None:
        market = _market()
        window = market.window(10)
        window.state = WindowState.FIRED
        assert not window_is_due(window, CLOSE_TS, float(CLOSE_TS - 1))

    def test_an_expired_window_is_never_due_again(self) -> None:
        market = _market()
        window = market.window(10)
        window.state = WindowState.EXPIRED
        assert not window_is_due(window, CLOSE_TS, float(CLOSE_TS - 1))

    def test_time_moving_backwards_does_not_reopen_a_window(self) -> None:
        """NTP steps the clock back. The window must stay open.

        chrony can step the clock backwards at any moment. An implementation that
        derived open-ness from `now` alone would un-open a window the process has
        already frozen and acted on.
        """
        market = _market()
        window = market.window(10)
        assert window_is_due(window, CLOSE_TS, float(activation_ts(CLOSE_TS, 10)))
        window.state = WindowState.FROZEN
        # Clock steps back before the instant. State, not time, decides.
        assert not window_is_due(window, CLOSE_TS, float(WINDOW_TS))
        assert window.state is WindowState.FROZEN


class TestActivationRespectsMarketPhase:
    def test_only_active_markets_activate_windows(self) -> None:
        market = _market()
        assert is_activatable(market)
        for phase in (
            MarketPhase.DISCOVERED,
            MarketPhase.CANCELLING,
            MarketPhase.SETTLING,
            MarketPhase.SETTLED,
            MarketPhase.DEAD,
        ):
            market.phase = phase
            assert not is_activatable(market)
            assert due_windows(market, float(CLOSE_TS - 1)) == ()

    def test_a_dead_market_never_opens_a_window(self) -> None:
        """A market with no official PTB is never traded, so nothing may freeze on it."""
        market = MarketInstance.create(WINDOW_TS, OFFSETS)
        market.phase = MarketPhase.DEAD
        assert due_windows(market, float(CLOSE_TS - 1)) == ()


class TestNextActivationTs:
    """Display helper. Never used to schedule anything."""

    def test_reports_the_earliest_pending_instant(self) -> None:
        market = _market()
        assert next_activation_ts(market) == CLOSE_TS - 15

    def test_advances_as_windows_open(self) -> None:
        market = _market()
        market.window(15).state = WindowState.FROZEN
        assert next_activation_ts(market) == CLOSE_TS - 10

    def test_is_none_when_nothing_is_pending(self) -> None:
        market = _market()
        for window in market.windows_by_priority():
            window.state = WindowState.EXPIRED
        assert next_activation_ts(market) is None


class TestNoTimerApisInTheWindowEngine:
    """Criterion 2, structurally: no scheduling API appears in arc/windows/.

    An AST walk rather than a grep (A11's rule for the same class of check). A grep for
    "call_later" matches a comment, misses `getattr(loop, name)`, and cannot tell an
    attribute access apart from a string. The walk looks at the parsed program.

    Note the deliberate exclusions: `asyncio.sleep` in a test harness is fine, but
    nothing in arc/windows/ may sleep, schedule, or spawn — a pass must return.
    """

    FORBIDDEN_ATTRS = frozenset(
        {
            "call_later",
            "call_at",
            "call_soon",
            "sleep",
            "create_task",
            "ensure_future",
            "run_coroutine_threadsafe",
            "wait_for",
            "timeout",
            "Timer",
            "TimerHandle",
        }
    )
    FORBIDDEN_MODULES = frozenset({"asyncio", "threading", "sched", "signal"})

    def _window_sources(self, source_root: Path) -> list[Path]:
        files = sorted((source_root / "arc" / "windows").glob("*.py"))
        assert files, "arc/windows/ has no modules; this test would pass vacuously"
        return files

    def test_no_scheduling_module_is_imported(self, source_root: Path) -> None:
        offenders: list[str] = []
        for path in self._window_sources(source_root):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in self.FORBIDDEN_MODULES:
                            offenders.append(f"{path.name}:{node.lineno}  import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    root = (node.module or "").split(".")[0]
                    if root in self.FORBIDDEN_MODULES:
                        offenders.append(f"{path.name}:{node.lineno}  from {node.module}")
        assert not offenders, "scheduling imports in arc/windows/:\n  " + "\n  ".join(offenders)

    def test_no_scheduling_call_is_made(self, source_root: Path) -> None:
        offenders: list[str] = []
        for path in self._window_sources(source_root):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                name = ""
                if isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.Name):
                    name = node.id
                if name in self.FORBIDDEN_ATTRS:
                    offenders.append(f"{path.name}:{node.lineno}  {name}")
        assert not offenders, "scheduling calls in arc/windows/:\n  " + "\n  ".join(offenders)

    def test_nothing_in_the_window_engine_is_a_coroutine(self, source_root: Path) -> None:
        """A synchronous pass cannot be blocked, cancelled or left pending (criterion 11)."""
        offenders: list[str] = []
        for path in self._window_sources(source_root):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    offenders.append(f"{path.name}:{node.lineno}  async def {node.name}")
                elif isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                    offenders.append(f"{path.name}:{node.lineno}  {type(node).__name__}")
        assert not offenders, "async constructs in arc/windows/:\n  " + "\n  ".join(offenders)

    def test_no_float_literal_arithmetic_on_prices(self, source_root: Path) -> None:
        """Criterion 20: no float() call anywhere in the window engine.

        Decimal only. A float that reaches a trigger comparison makes the comparison
        depend on binary rounding, so the same frozen values and the same TWAP could
        produce different verdicts on different values that print identically.
        """
        offenders: list[str] = []
        for path in self._window_sources(source_root):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "float"
                ):
                    offenders.append(f"{path.name}:{node.lineno}  float(")
        assert not offenders, "float() in arc/windows/:\n  " + "\n  ".join(offenders)
