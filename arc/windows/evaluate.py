"""Trigger evaluation. Direction-dependent, and the operators are NOT interchangeable.

    UP   fires when signal_twap >= locked_trigger
    DOWN fires when signal_twap <= locked_trigger

THE DEFECT THIS SHAPE PREVENTS (A12, criterion 5). A shared `>=` for both directions
does not bias the strategy — it deletes it. A DOWN window's trigger sits BELOW its
opening TWAP by exactly one buffer, so at the freeze instant `opening_twap >= trigger`
is ALREADY true. Every DOWN window would fire on the very first evaluation after
freezing, unconditionally, regardless of what BTC did. Half the strategy becomes "always
trade DOWN immediately", and nothing about it looks wrong: the window froze correctly,
the trigger is the configured distance away, the fire is logged normally. There is a
regression test whose entire job is to prove `>=`-only breaks DOWN.

EQUALITY FIRES, in both directions. `>=` and `<=`, never `>` and `<`. The trigger is the
threshold the buffer defines, so reaching it exactly is reaching it. U4 (whether the
VENUE settles on `>=` or `>`) is a separate, still-unverified question about outcome
determination and does not reach this comparison.

CONTINUOUS AND CHEAP (criteria 11, 17). `evaluate_market` is a plain synchronous
function over at most five windows: two Decimal comparisons each, no I/O, no lock, no
await. It is called from the feed path on every accepted observation, so anything
blocking here would stall the market loop and every other window with it. Persistence
of a FIRE is one small row write on an already-open WAL connection.

DETERMINISTIC (criterion 16). Same frozen values plus same signal TWAP gives the same
verdict, always. No clock is read to decide whether a trigger passed — `now` is recorded
as fired_at and never compared — and no float appears anywhere: every quantity is a
Decimal, so the comparison is exact and repeatable rather than dependent on binary
rounding (criterion 20).

INDEPENDENT (criteria 6, 18). Each window is evaluated in its own try block. One
market can simultaneously hold a FROZEN 10s DOWN window and a FROZEN 3s UP window, each
with its own trigger, and one raising cannot prevent the others from being evaluated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from arc.domain.enums import Direction, WindowState
from arc.domain.models import ExecutionWindow, MarketInstance
from arc.logging_setup import log_event
from arc.storage.store import Store
from arc.windows.lifecycle import (
    WindowTransitionError,
    assert_can_fire,
    is_terminal,
    transition,
)

__all__ = ["TriggerResult", "evaluate_market", "evaluate_window", "is_satisfied"]


@dataclass(frozen=True, slots=True)
class TriggerResult:
    """One window's verdict. Frozen: a verdict that could be edited is not a verdict."""

    offset_seconds: int
    fired: bool
    direction: Direction | None = None
    signal_twap: Decimal | None = None
    locked_trigger: Decimal | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def is_satisfied(window: ExecutionWindow, signal_twap: Decimal | None) -> bool:
    """The comparison itself. Pure, total, and side-effect free.

    Delegates to ExecutionWindow.is_triggered so the direction logic lives in exactly
    one place. Two implementations of this comparison is one more than the number that
    can be kept correct — and the failure mode of the wrong one is silent.
    """
    return window.is_triggered(signal_twap)


def evaluate_window(
    market: MarketInstance,
    window: ExecutionWindow,
    *,
    store: Store,
    now: float,
    logger: logging.Logger | None = None,
) -> TriggerResult:
    """Evaluate one frozen window. Fires at most once, ever.

    The fire is persisted BEFORE this returns, so a crash immediately after cannot lose
    the fact that the window fired — which would otherwise let it fire a second time
    after the restart and authorise a second intent from one window (criterion 12).
    """
    offset = window.offset_seconds

    # Terminal windows are skipped silently, not reported as errors. The evaluator runs
    # on every observation, so by late in a market most windows are terminal and this is
    # the overwhelmingly common path.
    if is_terminal(window.state):
        return TriggerResult(offset_seconds=offset, fired=False)

    if window.state is not WindowState.FROZEN:
        # PENDING: no trigger exists yet, so there is nothing to compare. Not an error —
        # the window simply has not activated.
        return TriggerResult(offset_seconds=offset, fired=False)

    signal_twap = market.signal_twap
    if not is_satisfied(window, signal_twap):
        return TriggerResult(
            offset_seconds=offset,
            fired=False,
            direction=window.direction,
            signal_twap=signal_twap,
            locked_trigger=window.locked_trigger,
        )

    # Satisfied. Re-check the fire preconditions rather than trusting the state read
    # above: this is the one path with a lasting consequence.
    try:
        assert_can_fire(window)
        transition(window, WindowState.FIRED, logger=logger)
    except WindowTransitionError as exc:
        return TriggerResult(offset_seconds=offset, fired=False, error=str(exc))

    window.fired_at = now
    store.save_window_state(market.slug, offset, WindowState.FIRED, fired_at=now)

    log_event(
        logging.INFO,
        "Window Fired",
        f"{market.slug}  {offset}s  {window.direction}  "
        f"twap {signal_twap}  trigger {window.locked_trigger}",
        logger=logger,
    )
    return TriggerResult(
        offset_seconds=offset,
        fired=True,
        direction=window.direction,
        signal_twap=signal_twap,
        locked_trigger=window.locked_trigger,
    )


def evaluate_market(
    market: MarketInstance,
    *,
    store: Store,
    now: float,
    logger: logging.Logger | None = None,
) -> tuple[TriggerResult, ...]:
    """Evaluate every window in priority order: 3, 5, 7, 10, 15 (criterion 7).

    Order is by ascending offset because the window nearest close has the largest
    determined fraction behind it and is the best-informed (A7/A12).

    Every window is evaluated even if an earlier one fired. Windows are independent:
    they hold different triggers and may hold opposite directions, and stopping at the
    first fire would mean a 3s UP window's fire suppressed the 10s DOWN window's
    entirely separate signal. Whether two fires should both become orders is a Risk
    Engine question (hazard H3), not this module's — the Window Engine only reports
    which triggers were satisfied.

    Each window is isolated: an unexpected failure is captured into that window's
    result so the remaining windows are still evaluated (criterion 18).
    """
    results: list[TriggerResult] = []
    for window in market.windows_by_priority():
        try:
            results.append(
                evaluate_window(market, window, store=store, now=now, logger=logger)
            )
        except Exception as exc:
            # Broad by design: isolating one window's failure from the other four is
            # the point (criterion 18). A narrower clause would let an unforeseen
            # exception type abandon every window after this one.
            log_event(
                logging.ERROR,
                "Window Evaluation Failed",
                f"{market.slug}  {window.offset_seconds}s  {type(exc).__name__}: {exc}",
                logger=logger,
            )
            results.append(
                TriggerResult(
                    offset_seconds=window.offset_seconds,
                    fired=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return tuple(results)
