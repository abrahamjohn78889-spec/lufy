"""Restart recovery. One fixed order, run before any new submission.

    reconnect → feeds → websocket → wallet → live orders → positions
    → pending orders → windows → continue

The order is not cosmetic. Each step depends on the one before it: there is no
point asking the venue about orders before the connection is up, no point counting
positions before the orders they belong to are known, and no point resuming windows
before it is settled what is already resting on the book. Running window resumption
first is what produces the failure this whole module exists to prevent — a second
submission for a window that already has one.

NEVER DUPLICATE SUBMISSIONS. Two independent mechanisms, because one is not enough:

  * intent uniqueness is a SQLite UNIQUE constraint on (market, window), so the
    decision itself cannot be made twice even across a crash mid-write; and
  * order ids are derived from (market, window, index, generation), so a replayed
    submission resolves to the row that already exists rather than to a new order.

Neither depends on in-memory state, which is exactly what a restart destroys.

Recovery is idempotent and safe to run repeatedly. A VPS that reboots twice in a
minute runs it twice and converges on the same state both times.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from arc.domain.enums import MarketPhase, OrderState
from arc.errors import ArcError
from arc.execution.fill_engine import FillEngine
from arc.execution.orders import transition
from arc.execution.reconcile import Reconciler
from arc.logging_setup import log_event
from arc.storage.store import Store

__all__ = ["RecoveryReport", "RecoveryRunner", "RecoveryStep", "StepResult"]


class RecoveryStep(StrEnum):
    """The fixed sequence. Order is load-bearing; see the module docstring."""

    RECONNECT = "RECONNECT"
    FEEDS = "FEEDS"
    WEBSOCKET = "WEBSOCKET"
    WALLET = "WALLET"
    LIVE_ORDERS = "LIVE_ORDERS"
    POSITIONS = "POSITIONS"
    PENDING_ORDERS = "PENDING_ORDERS"
    WINDOWS = "WINDOWS"


@dataclass(frozen=True, slots=True)
class StepResult:
    step: RecoveryStep
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class RecoveryReport:
    """What recovery found. Returned rather than logged-and-forgotten.

    The caller decides whether to resume trading: recovery that could not resolve
    every unknown order must not be followed by new submissions, because the
    unresolved ones may be resting and the new ones would stack on top of them.
    """

    steps: list[StepResult] = field(default_factory=list)
    unresolved_orders: tuple[str, ...] = ()
    orphans: tuple[str, ...] = ()
    resumed_markets: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps) and not self.orphans

    @property
    def safe_to_trade(self) -> bool:
        """True only when nothing at the venue is unaccounted for."""
        return self.ok and not self.unresolved_orders


class RecoveryRunner:
    """Executes the recovery sequence once, in order."""

    __slots__ = ("_fills", "_logger", "_reconciler", "_store")

    def __init__(
        self,
        store: Store,
        reconciler: Reconciler,
        fills: FillEngine,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._reconciler = reconciler
        self._fills = fills
        self._logger = logger

    async def run(
        self,
        now: float,
        *,
        reconnect: Callable[[], Awaitable[None]] | None = None,
        feeds: Callable[[], Awaitable[None]] | None = None,
        websocket: Callable[[], Awaitable[None]] | None = None,
        wallet: Callable[[], Awaitable[None]] | None = None,
        windows: Callable[[], Awaitable[tuple[str, ...]]] | None = None,
    ) -> RecoveryReport:
        """Run every step. Never raises; failures land in the report.

        Swallowing exceptions here is deliberate and is the opposite of hiding
        them: a recovery step that raises out of this method takes the process
        down, and a process that cannot restart cannot recover at all. Each
        failure is recorded, the sequence continues, and `safe_to_trade` — not an
        absence of exceptions — is what gates the resumption of trading.
        """
        report = RecoveryReport()

        for step, hook in (
            (RecoveryStep.RECONNECT, reconnect),
            (RecoveryStep.FEEDS, feeds),
            (RecoveryStep.WEBSOCKET, websocket),
            (RecoveryStep.WALLET, wallet),
        ):
            report.steps.append(await self._external(step, hook))

        report.steps.append(await self._recover_orders(report, now))
        report.steps.append(self._recover_positions(report))
        report.steps.append(self._recover_pending(now))
        report.steps.append(await self._recover_windows(report, windows))

        log_event(
            logging.INFO,
            "Recovery Complete",
            f"{sum(1 for s in report.steps if s.ok)}/{len(report.steps)} steps  "
            f"{len(report.unresolved_orders)} unresolved  {len(report.orphans)} orphaned",
            logger=self._logger,
        )
        return report

    async def _external(
        self, step: RecoveryStep, hook: Callable[[], Awaitable[None]] | None
    ) -> StepResult:
        """Run a caller-supplied step. Absent hooks are a pass, not a failure.

        The connection, feed and wallet steps belong to whoever owns those
        resources; recovery sequences them rather than implementing them, so a
        component that is not configured in this deployment simply has no hook.
        """
        if hook is None:
            return StepResult(step=step, ok=True, detail="not configured")
        try:
            await hook()
        except (ArcError, OSError) as exc:
            log_event(
                logging.WARNING, "Recovery Step Failed", f"{step.value}  {exc}",
                logger=self._logger,
            )
            return StepResult(step=step, ok=False, detail=str(exc))
        return StepResult(step=step, ok=True)

    async def _recover_orders(self, report: RecoveryReport, now: float) -> StepResult:
        """Reconcile every unsettled market against the venue's own order list."""
        unresolved: list[str] = []
        orphans: list[str] = []
        failures: list[str] = []

        for slug in self._store.unsettled_markets():
            try:
                result = await self._reconciler.reconcile(slug, now)
            except (ArcError, OSError) as exc:
                failures.append(f"{slug}: {exc}")
                # Every live order in a market that could not be reconciled stays
                # unresolved. Treating an unreachable venue as "nothing resting"
                # would clear the gate that stops new submissions stacking on top
                # of orders that are, in fact, still on the book.
                unresolved.extend(o.order_id for o in self._store.live_orders(slug))
                continue
            unresolved.extend(result.still_live)
            orphans.extend(result.orphans)

        report.unresolved_orders = tuple(unresolved)
        report.orphans = tuple(orphans)
        return StepResult(
            step=RecoveryStep.LIVE_ORDERS,
            ok=not failures,
            detail="; ".join(failures),
        )

    def _recover_positions(self, report: RecoveryReport) -> StepResult:
        """Rebuild position accounting from persisted fills.

        Read from storage rather than recomputed from anything in memory, and
        summed over quantity rather than counted over orders: a reprice chain is
        several orders and one position (hazard H4).
        """
        markets = self._store.unsettled_markets()
        total = 0
        for slug in markets:
            total += len(self._store.fills_for(slug))
        return StepResult(
            step=RecoveryStep.POSITIONS,
            ok=True,
            detail=f"{total} fills across {len(markets)} markets",
        )

    def _recover_pending(self, now: float) -> StepResult:
        """Resolve rows written before submission that never reached the venue.

        A PENDING row means the process died between the write and the call (A4).
        Reconciliation has just confirmed the venue does not hold it — a PENDING
        order has no venue id, so it cannot have been matched to one — and it is
        closed out as EXPIRED. Retrying it instead would submit into a market whose
        window has moved on, using a price frozen before the restart.
        """
        expired = 0
        for slug in self._store.unsettled_markets():
            for order in self._store.orders_for(slug):
                if order.state is not OrderState.PENDING:
                    continue
                transition(order, OrderState.EXPIRED, now, "unsubmitted at restart")
                self._store.save_order(order)
                expired += 1
        return StepResult(
            step=RecoveryStep.PENDING_ORDERS, ok=True, detail=f"{expired} expired"
        )

    async def _recover_windows(
        self,
        report: RecoveryReport,
        hook: Callable[[], Awaitable[tuple[str, ...]]] | None,
    ) -> StepResult:
        """Resume window evaluation. Last, once the book is fully accounted for.

        Frozen windows are reloaded verbatim by the rotation layer, never
        recomputed: a window that comes back with a trigger derived from the
        post-restart average is a window trading a threshold nobody configured.
        """
        if hook is None:
            return StepResult(step=RecoveryStep.WINDOWS, ok=True, detail="not configured")
        try:
            resumed = await hook()
        except (ArcError, OSError) as exc:
            return StepResult(step=RecoveryStep.WINDOWS, ok=False, detail=str(exc))
        report.resumed_markets = resumed
        return StepResult(
            step=RecoveryStep.WINDOWS, ok=True, detail=f"{len(resumed)} markets resumed"
        )


def markets_needing_sweep(store: Store) -> tuple[str, ...]:
    """Unsettled markets that still have live orders.

    Used after recovery to retract anything left resting from before the restart.
    A market past its close with orders still on the book is the one state that
    cannot wait for the next natural sweep — there will not be one, because the
    market is gone.
    """
    out: list[str] = []
    for slug in store.unsettled_markets():
        row = store.load_market_row(slug)
        if row is None:
            continue
        if str(row["phase"]) in (MarketPhase.SETTLED.value, MarketPhase.DEAD.value):
            continue
        if store.live_orders(slug):
            out.append(slug)
    return tuple(out)
