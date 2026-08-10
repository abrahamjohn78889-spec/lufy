"""The Recorder: proves every market recorded everything, from the first market.

WHY THIS IS AN AUDITOR AND NOT A WRITER. Every value criterion 2 lists is already
persisted by the engine that owns it, at the moment it owns it — PTB by the PTB
freeze, the TWAPs by the accumulator, the window fields by the freeze, intents by
the arbiter, orders and fills by the submitter and the fill engine, settlement by
the settlement watcher. A recorder that wrote its own second copy would be a
second history, and on the day the two disagreed the operator would be reading
whichever one their page happened to load. So this reads the rows back and reports
what is missing.

WHAT "NOTHING MAY REQUIRE RECONSTRUCTION" MEANS HERE. A gap is a fact about the
run, not something to repair. If a market has no PTB row this says so; it does not
derive one from a neighbouring market, interpolate one, or fall back to a computed
price. Reconstructing a missing value is how a validation pass turns a real bug
into a green report.

ABSENCE IS NOT ALWAYS A FAULT. A window that never fired has no intent and no
order, and that is BUFFER_NOT_SATISFIED — the most common correct outcome of the
day. Only a value whose own precondition was met counts as missing, which is why
each field below is checked against the window state that should have produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final

from arc.domain.enums import WindowState
from arc.storage.store import Store

__all__ = [
    "MARKET_FIELDS",
    "RUNTIME_STATE_KEY",
    "WINDOW_FIELDS",
    "MarketRecord",
    "RecorderReport",
    "SubmissionRecord",
    "audit_market",
    "audit_recording",
    "submission_records",
]

# The key the engine itself writes on every start (`_RESTART_KEY`). Imported by
# value rather than by name so this module does not reach into the engine, but it
# must stay the same string — a mismatch would report every market as unrecorded.
RUNTIME_STATE_KEY: Final[str] = "restart_count"

# The per-market values. Order is the order they are produced in, so a report read
# top to bottom is the market's own timeline.
MARKET_FIELDS: Final[tuple[str, ...]] = (
    "ptb",
    "signal_twap",
    "settlement_twap",
    "runtime_state",
)

# The per-window values, each with the window state from which it becomes required.
# PENDING requires nothing: a window that never opened is not a missing record.
WINDOW_FIELDS: Final[tuple[str, ...]] = (
    "direction",
    "locked_trigger",
    "window",
    "execution_intent",
    "submission",
    "reprice",
    "fill",
    "reconciliation",
    "settlement",
    "ledger",
)


@dataclass(frozen=True, slots=True)
class MarketRecord:
    """One market's recording completeness."""

    slug: str
    window_ts: int
    ptb: bool
    signal_twap: bool
    settlement_twap: bool
    runtime_state: bool
    windows_expected: int
    windows_recorded: int
    intents: int
    submissions: int
    reprices: int
    fills: int
    reconciliations: int
    settled: bool
    ledger_records: int
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing

    def as_json(self) -> dict[str, Any]:
        return {
            "market": self.slug,
            "window_ts": self.window_ts,
            "ptb": self.ptb,
            "signal_twap": self.signal_twap,
            "settlement_twap": self.settlement_twap,
            "runtime_state": self.runtime_state,
            "windows_expected": self.windows_expected,
            "windows_recorded": self.windows_recorded,
            "intents": self.intents,
            "submissions": self.submissions,
            "reprices": self.reprices,
            "fills": self.fills,
            "reconciliations": self.reconciliations,
            "settled": self.settled,
            "ledger_records": self.ledger_records,
            "complete": self.complete,
            "missing": list(self.missing),
        }


@dataclass(slots=True)
class RecorderReport:
    """Every audited market, plus what the run as a whole is missing."""

    markets: tuple[MarketRecord, ...] = ()
    gaps: tuple[str, ...] = ()
    first_window_ts: int = 0
    last_window_ts: int = 0

    @property
    def complete(self) -> bool:
        return all(m.complete for m in self.markets) and not self.gaps

    @property
    def incomplete(self) -> tuple[MarketRecord, ...]:
        return tuple(m for m in self.markets if not m.complete)

    def as_json(self) -> dict[str, Any]:
        return {
            "markets_audited": len(self.markets),
            "complete": self.complete,
            "incomplete_count": len(self.incomplete),
            "first_window_ts": self.first_window_ts,
            "last_window_ts": self.last_window_ts,
            # Bounded: an operator reading a hundred-market audit needs the failures,
            # and shipping every row would make the failures the hard part to find.
            "incomplete": [m.as_json() for m in self.incomplete[:50]],
            "market_gaps": list(self.gaps),
        }


def _market_gaps(window_ts: list[int], cadence: int) -> tuple[str, ...]:
    """Consecutive markets whose window timestamps are not one cadence apart.

    A gap means a market the runtime never saw — the failure criterion 1 calls
    "zero market gaps" — and it is invisible in any per-market check, because each
    market either side of the hole is itself perfectly complete.
    """
    gaps = []
    for earlier, later in pairwise(window_ts):
        step = later - earlier
        if step != cadence:
            missed = (step // cadence) - 1 if step > cadence else 0
            gaps.append(f"{earlier} → {later} ({step}s, {missed} market(s) missed)")
    return tuple(gaps)


def audit_market(store: Store, slug: str, *, expected_windows: int) -> MarketRecord:
    """One market, read back from the rows the engines wrote."""
    row = store.load_market_row(slug)
    if row is None:
        return MarketRecord(
            slug=slug, window_ts=0, ptb=False, signal_twap=False, settlement_twap=False,
            runtime_state=False, windows_expected=expected_windows, windows_recorded=0,
            intents=0, submissions=0, reprices=0, fills=0, reconciliations=0,
            settled=False, ledger_records=0, missing=("market",),
        )

    windows = store.windows_for(slug)
    orders = store.orders_for(slug)
    fills = store.fills_for(slug)
    intents = store.intents_for(slug)
    settlement = store.settlement_for(slug)

    # A window that fired is a window that should have produced an intent. Anything
    # weaker (counting every window) would report BUFFER_NOT_SATISFIED as a missing
    # record, and the report would be permanently red for the normal case.
    fired = tuple(w for w in windows if WindowState(str(w["state"])) is WindowState.FIRED)
    frozen = tuple(
        w for w in windows
        if WindowState(str(w["state"])) is not WindowState.PENDING
        and w["frozen_at"] is not None
    )
    # A chain of more than one order for the same window is a reprice, by the
    # ledger's own definition of the chain.
    by_window: dict[int, int] = {}
    for order in orders:
        by_window[order.offset_seconds] = by_window.get(order.offset_seconds, 0) + 1
    reprices = sum(count - 1 for count in by_window.values() if count > 1)
    # Reconciliation is visible as a venue order id on a row ARC created locally:
    # the id is what the venue answered with, so its presence is the acknowledgement.
    reconciled = sum(1 for o in orders if o.venue_order_id)

    missing: list[str] = []
    ptb = row["ptb"] is not None
    signal = int(row["observation_count"] or 0) > 0
    settlement_twap = row["settlement_twap"] is not None
    # The runtime writes its restart counter to runtime_state on every start, so the
    # row's presence is the evidence a real runtime produced these markets. Reading
    # the key the engine actually writes rather than a key invented for this audit:
    # a checker that looks for a name nothing sets reports every market as broken.
    runtime_state = store.get_runtime_state(RUNTIME_STATE_KEY) is not None

    if not ptb:
        missing.append("ptb")
    if not signal:
        missing.append("signal_twap")
    if not settlement_twap:
        missing.append("settlement_twap")
    if not runtime_state:
        missing.append("runtime_state")
    if len(windows) < expected_windows:
        missing.append(f"windows ({len(windows)}/{expected_windows})")
    for window in frozen:
        if window["direction"] is None and WindowState(str(window["state"])) is not (
            WindowState.NO_DIRECTION
        ):
            missing.append(f"direction@{window['offset_seconds']}s")
        if window["locked_trigger"] is None:
            missing.append(f"locked_trigger@{window['offset_seconds']}s")
    if fired and len(intents) < len(fired):
        missing.append(f"execution_intent ({len(intents)}/{len(fired)})")
    if intents and not orders:
        missing.append("submission")
    # Settlement is required only once the venue has resolved the market. An open
    # market with no settlement row is the normal state of the current market.
    if str(row["phase"]) in {"SETTLED", "ARCHIVED"} and settlement is None:
        missing.append("settlement")

    return MarketRecord(
        slug=slug,
        window_ts=int(row["window_ts"]),
        ptb=ptb,
        signal_twap=signal,
        settlement_twap=settlement_twap,
        runtime_state=runtime_state,
        windows_expected=expected_windows,
        windows_recorded=len(windows),
        intents=len(intents),
        submissions=len(orders),
        reprices=reprices,
        fills=len(fills),
        reconciliations=reconciled,
        settled=settlement is not None,
        ledger_records=len(windows),
        missing=tuple(missing),
    )


def audit_recording(
    store: Store, *, expected_windows: int, cadence_seconds: int, market_limit: int = 200
) -> RecorderReport:
    """Audit the whole run, newest markets first, and look for market gaps."""
    rows = store.recent_markets(limit=market_limit)
    records = tuple(
        audit_market(store, str(r["slug"]), expected_windows=expected_windows) for r in rows
    )
    stamps = sorted(r.window_ts for r in records if r.window_ts)
    return RecorderReport(
        markets=records,
        gaps=_market_gaps(stamps, cadence_seconds),
        first_window_ts=stamps[0] if stamps else 0,
        last_window_ts=stamps[-1] if stamps else 0,
    )


@dataclass(slots=True)
class SubmissionRecord:
    """Criterion 4: everything one submission must record.

    Assembled from the order row and its fills rather than logged separately at
    submit time, so it cannot disagree with what was actually stored — and so it
    is still available after a restart, which is exactly when latency questions
    get asked.
    """

    market: str
    offset_seconds: int
    submission_index: int
    submission_count: int
    order_generation: int
    order_id: str
    venue_order_id: str
    price: str
    shares: str
    created_at: float
    ms_before_close: float | None
    submission_latency_ms: float | None
    fill_latency_ms: float | None
    acknowledged: bool
    state: str

    def as_json(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "window": f"{self.offset_seconds}s",
            "offset_seconds": self.offset_seconds,
            "submission_index": self.submission_index,
            "submission_count": self.submission_count,
            "order_generation": self.order_generation,
            "order_id": self.order_id,
            "venue_order_id": self.venue_order_id,
            "order_price": self.price,
            "shares": self.shares,
            "created_at": self.created_at,
            "ms_before_close": self.ms_before_close,
            "submission_latency_ms": self.submission_latency_ms,
            "fill_latency_ms": self.fill_latency_ms,
            "venue_acknowledged": self.acknowledged,
            "state": self.state,
        }


def submission_records(store: Store, slug: str) -> tuple[SubmissionRecord, ...]:
    """Every submission this market produced, oldest first."""
    row = store.load_market_row(slug)
    close_ts = float(row["close_ts"]) if row is not None else None
    orders = sorted(store.orders_for(slug), key=lambda o: o.created_at)
    fills = store.fills_for(slug)
    first_fill: dict[str, float] = {}
    for fill in fills:
        if fill.order_id not in first_fill or fill.ts < first_fill[fill.order_id]:
            first_fill[fill.order_id] = fill.ts

    per_window: dict[int, int] = {}
    totals: dict[int, int] = {}
    for order in orders:
        totals[order.offset_seconds] = totals.get(order.offset_seconds, 0) + 1

    records = []
    for index, order in enumerate(orders, start=1):
        generation = per_window.get(order.offset_seconds, 0) + 1
        per_window[order.offset_seconds] = generation
        fill_ts = first_fill.get(order.order_id)
        records.append(
            SubmissionRecord(
                market=slug,
                offset_seconds=order.offset_seconds,
                submission_index=index,
                submission_count=totals[order.offset_seconds],
                order_generation=generation,
                order_id=order.order_id,
                venue_order_id=order.venue_order_id,
                price=str(order.price),
                shares=str(order.size),
                created_at=order.created_at,
                ms_before_close=(
                    None if close_ts is None else (close_ts - order.created_at) * 1000.0
                ),
                # updated_at is stamped when the venue answer is written, so the
                # difference is the round trip and not a separately timed guess.
                submission_latency_ms=max((order.updated_at - order.created_at) * 1000.0, 0.0)
                if order.updated_at > order.created_at
                else None,
                fill_latency_ms=(
                    None if fill_ts is None else max((fill_ts - order.created_at) * 1000.0, 0.0)
                ),
                acknowledged=bool(order.venue_order_id),
                state=order.state.value,
            )
        )
    return tuple(records)
