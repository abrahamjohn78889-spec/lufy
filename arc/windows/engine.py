"""The Window Engine driver: one pass over one market's windows.

`WindowEngine.pass_over(market, now)` does exactly three things, in order:

    1  open every window whose activation instant has passed   (activation.py)
    2  freeze each newly opened window, atomically             (freeze.py)
    3  evaluate every frozen window against the signal TWAP    (evaluate.py)

and `expire_all` sweeps the rest when the market closes (lifecycle.py).

Called from the market loop on every pass and from the feed path on every accepted
observation. Both call sites are correct and neither is a schedule: the whole design is
that calling more often only reduces activation latency, and calling less often only
increases it, with no path by which a window is lost (criteria 1-2).

Synchronous throughout. Nothing here awaits, sleeps, locks, or creates a task, so a pass
cannot block the market loop, another window, or feed processing (criterion 11), and
there is no timer or background task to leak (criterion 19).

This engine determines ONLY whether a frozen trigger has been satisfied. It produces no
ExecutionIntent, sizes nothing, reads no order book and touches no wallet. What a fired
window becomes is a question for engines that do not exist yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from arc.config import TradingConfig
from arc.domain.models import ExecutionWindow, MarketInstance
from arc.storage.store import Store
from arc.windows.activation import due_windows
from arc.windows.evaluate import TriggerResult, evaluate_market
from arc.windows.freeze import freeze_due_window, restore_window
from arc.windows.lifecycle import expire_window

__all__ = ["WindowEngine", "WindowPass"]


@dataclass(frozen=True, slots=True)
class WindowPass:
    """What one pass did. Empty tuples mean nothing of that kind happened."""

    frozen: tuple[int, ...] = ()
    fired: tuple[int, ...] = ()
    results: tuple[TriggerResult, ...] = field(default=())

    @property
    def acted(self) -> bool:
        return bool(self.frozen or self.fired)


class WindowEngine:
    """Drives window activation, freezing and evaluation for whichever market it is given.

    Holds NO per-market state. Every mutable value lives on the MarketInstance passed in
    (A11), so one engine correctly serves both markets that are alive across a close
    boundary — and there is no cache here that could carry one market's trigger into the
    next. The counters are process-level totals for diagnostics, not per-market state.
    """

    __slots__ = ("_logger", "_store", "_trading", "windows_expired", "windows_fired",
                 "windows_frozen")

    def __init__(
        self,
        store: Store,
        trading: TradingConfig,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._trading = trading
        self._logger = logger
        self.windows_frozen = 0
        self.windows_fired = 0
        self.windows_expired = 0

    def pass_over(self, market: MarketInstance, now: float) -> WindowPass:
        """One level-triggered pass: activate, freeze, evaluate.

        Freezing happens before evaluation in the SAME pass, so a window that comes due
        on this pass is also evaluated on this pass. It cannot fire from that first
        evaluation unless the TWAP has already crossed its brand-new trigger, which for a
        buffer above zero it cannot have — the trigger is one whole buffer away from the
        TWAP that just produced it. That is exactly why the buffer is required to be
        positive at freeze time.
        """
        frozen: list[int] = []
        for window in due_windows(market, now):
            if freeze_due_window(
                market,
                window,
                trading=self._trading,
                store=self._store,
                now=now,
                logger=self._logger,
            ):
                frozen.append(window.offset_seconds)
                self.windows_frozen += 1

        results = evaluate_market(market, store=self._store, now=now, logger=self._logger)
        fired = tuple(r.offset_seconds for r in results if r.fired)
        self.windows_fired += len(fired)

        return WindowPass(frozen=tuple(frozen), fired=fired, results=results)

    def expire_all(self, market: MarketInstance) -> tuple[int, ...]:
        """End every non-terminal window. Called once when the market closes.

        A window that never crossed reaches EXPIRED, which is a NORMAL terminal state
        and is never logged as an error (criterion 8): the buffer said the move was not
        big enough, so there was no trade. Nothing here is left PENDING, so no window
        survives its own market and no orphan sits in a hopeful state forever.
        """
        expired: list[int] = []
        for window in market.windows_by_priority():
            if expire_window(window, logger=self._logger):
                self._store.save_window_state(market.slug, window.offset_seconds, window.state)
                expired.append(window.offset_seconds)
                self.windows_expired += 1
        return tuple(expired)

    def restore(self, market: MarketInstance) -> tuple[int, ...]:
        """Reload every persisted frozen window onto a freshly built market.

        Values come back VERBATIM, including direction and locked_trigger. Recomputation
        is impossible from here: restore_window reads the row and passes it through, and
        the signal TWAP is never consulted (A4, criterion 9).
        """
        restored: list[int] = []
        for window in market.windows_by_priority():
            if restore_window(market, window.offset_seconds, store=self._store):
                restored.append(window.offset_seconds)
        return tuple(restored)

    def pending(self, market: MarketInstance) -> tuple[ExecutionWindow, ...]:
        """Windows not yet terminal. For the dashboard's Active Window panel."""
        from arc.windows.lifecycle import is_terminal

        return tuple(w for w in market.windows_by_priority() if not is_terminal(w.state))
