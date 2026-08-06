"""Unified Ledger: the only history. One record per execution window.

There is no separate Orders page and no Trade History page, so this is the single
place an operator reconstructs what happened. It is a READ MODEL assembled from
the rows the engines already write — markets, windows, intents, orders, fills,
settlements — and it persists nothing of its own. A second copy of the history
would eventually disagree with the first, and the one an operator happened to be
looking at would decide whether a fill existed.

Keyed by (market, window) rather than by order. A reprice is cancel-then-place, so
one window can produce several order rows; keying by order would show one window as
three trades and treble the apparent submission count. Filled quantity is summed
across the whole reprice chain (hazard H4) because a partial fill on a cancelled
leg is still a real position.

Windows that never produced an order are still records. BUFFER_NOT_SATISFIED and
NO_DIRECTION are the two most important things the operator needs to see, and a
ledger that only listed orders would render both as silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from arc.domain.enums import (
    ORDER_STATE_DISPLAY,
    REJECTION_REASON_DISPLAY,
    OrderState,
    Outcome,
    WindowState,
)
from arc.domain.models import Fill, Order
from arc.domain.money import dec_str
from arc.storage.store import Store

__all__ = [
    "BUFFER_NOT_SATISFIED",
    "LedgerRecord",
    "ledger_records",
    "ledger_totals",
    "search_records",
]

_ZERO: Final[Decimal] = Decimal("0")

# The stable internal code, not venue error text. It survives restart because it is
# derived from the persisted window state, and the dashboard filters on it.
BUFFER_NOT_SATISFIED: Final[str] = "BUFFER_NOT_SATISFIED"

_BUFFER_STATUS: Final[dict[WindowState, str]] = {
    WindowState.PENDING: "WAITING",
    WindowState.FROZEN: "WAITING",
    WindowState.FIRED: "SATISFIED",
    WindowState.EXPIRED: BUFFER_NOT_SATISFIED,
    WindowState.NO_DIRECTION: "NO_DIRECTION",
}


def _dec(value: object) -> Decimal | None:
    """TEXT column to Decimal. NULL stays NULL rather than becoming zero.

    Zero and unknown are different facts here: a PTB of zero would be displayed as
    a real reference price and every deviation computed against it would be wrong.
    """
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _s(value: Decimal | None) -> str | None:
    """Decimal to JSON. STRING, never a float — the API boundary contract."""
    return None if value is None else dec_str(value)


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    """One window's whole life, from freeze through settlement."""

    market: str
    window_ts: int
    close_ts: int
    offset_seconds: int
    ptb: Decimal | None
    signal_twap: Decimal | None
    settlement_twap: Decimal | None
    direction: str
    locked_trigger: Decimal | None
    buffer: Decimal | None
    intent_id: str
    local_order_id: str
    venue_order_id: str
    submission_time: float | None
    fill_time: float | None
    settlement_time: float | None
    order_price: Decimal | None
    fill_price: Decimal | None
    quantity: Decimal | None
    filled_quantity: Decimal
    remaining_quantity: Decimal | None
    state: str
    state_display: str
    rejection_reason: str
    rejection_display: str
    buffer_status: str
    settlement_result: str
    pnl: Decimal | None
    notes: str

    def as_json(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "window_ts": self.window_ts,
            "close_ts": self.close_ts,
            "window": f"{self.offset_seconds}s",
            "offset_seconds": self.offset_seconds,
            "ptb": _s(self.ptb),
            "signal_twap": _s(self.signal_twap),
            "settlement_twap": _s(self.settlement_twap),
            "direction": self.direction,
            "locked_trigger": _s(self.locked_trigger),
            "buffer": _s(self.buffer),
            "intent_id": self.intent_id,
            "local_order_id": self.local_order_id,
            "venue_order_id": self.venue_order_id,
            "submission_time": self.submission_time,
            "fill_time": self.fill_time,
            "settlement_time": self.settlement_time,
            "order_price": _s(self.order_price),
            "fill_price": _s(self.fill_price),
            "quantity": _s(self.quantity),
            "filled_quantity": dec_str(self.filled_quantity),
            "remaining_quantity": _s(self.remaining_quantity),
            "state": self.state,
            "state_display": self.state_display,
            "rejection_reason": self.rejection_reason,
            "rejection_display": self.rejection_display,
            "buffer_status": self.buffer_status,
            "settlement_result": self.settlement_result,
            "pnl": _s(self.pnl),
            "notes": self.notes,
        }

    def haystack(self) -> str:
        """Everything the search box may match, lowercased once."""
        return " ".join(
            str(part)
            for part in (
                self.market,
                self.window_ts,
                f"{self.offset_seconds}s",
                self.direction,
                self.intent_id,
                self.local_order_id,
                self.venue_order_id,
                self.state,
                self.rejection_reason,
                self.buffer_status,
                self.settlement_result,
                _s(self.ptb) or "",
            )
        ).lower()


def _chain_orders(orders: tuple[Order, ...], offset: int) -> tuple[Order, ...]:
    """Every order this window produced, oldest first."""
    chain = (o for o in orders if o.offset_seconds == offset)
    return tuple(sorted(chain, key=lambda o: o.created_at))


def _leader(chain: tuple[Order, ...]) -> Order | None:
    """The order whose state the record reports.

    The newest leg, not the first: after a reprice the earlier legs are legitimately
    CANCELLED and reporting the first would show a live window as cancelled.
    """
    return chain[-1] if chain else None


def _settlement_result(outcome: Outcome | None, pnl: Decimal | None) -> str:
    if outcome is None:
        return "UNRESOLVED"
    if pnl is None or pnl == _ZERO:
        return f"{outcome.value} (no position)"
    return f"{outcome.value} · {'WIN' if pnl > _ZERO else 'LOSS'}"


def _record(
    # Rows, not a typed model: this reads six tables the engines own and typing the
    # parameter as sqlite3.Row would import the SQL boundary out of arc/storage/.
    market: Any,
    window: Any,
    orders: tuple[Order, ...],
    fills: tuple[Fill, ...],
    intent_id: str,
    settlement_time: float | None,
    outcome: Outcome | None,
    pnl: Decimal | None,
) -> LedgerRecord:
    offset = int(window["offset_seconds"])
    chain = _chain_orders(orders, offset)
    leader = _leader(chain)
    chain_ids = {o.order_id for o in chain}
    window_fills = tuple(f for f in fills if f.order_id in chain_ids)

    filled = sum((f.size for f in window_fills), _ZERO)
    fill_notional = sum((f.size * f.price for f in window_fills), _ZERO)
    # Size-weighted, so a partial fill at a worse price on a repriced leg is not
    # averaged away as if both legs had traded the same quantity.
    fill_price = (fill_notional / filled) if filled > _ZERO else None

    state = leader.state if leader is not None else None
    quantity = leader.size if leader is not None else None
    remaining = None if quantity is None else max(quantity - filled, _ZERO)
    window_state = WindowState(str(window["state"]))
    reason = leader.rejection_reason if leader is not None else ""
    if not reason and window_state is WindowState.EXPIRED and leader is None:
        # No order was ever created because the trigger never passed. That is the
        # single most common non-event of the day and it must be visible as a reason.
        reason = BUFFER_NOT_SATISFIED

    return LedgerRecord(
        market=str(market["slug"]),
        window_ts=int(market["window_ts"]),
        close_ts=int(market["close_ts"]),
        offset_seconds=offset,
        ptb=_dec(window["ptb"]) or _dec(market["ptb"]),
        signal_twap=_dec(window["opening_twap"]),
        settlement_twap=_dec(market["settlement_twap"]),
        direction=str(window["direction"] or WindowState.NO_DIRECTION.value)
        if window_state is not WindowState.PENDING
        else "",
        locked_trigger=_dec(window["locked_trigger"]),
        buffer=_dec(window["buffer"]),
        intent_id=intent_id,
        local_order_id=leader.order_id if leader is not None else "",
        venue_order_id=leader.venue_order_id if leader is not None else "",
        submission_time=leader.created_at if leader is not None else None,
        fill_time=max((f.ts for f in window_fills), default=None),
        settlement_time=settlement_time,
        order_price=leader.price if leader is not None else None,
        fill_price=fill_price,
        quantity=quantity,
        filled_quantity=filled,
        remaining_quantity=remaining,
        state=state.value if state is not None else window_state.value,
        state_display=ORDER_STATE_DISPLAY[state] if state is not None else window_state.value,
        rejection_reason=reason,
        rejection_display=REJECTION_REASON_DISPLAY.get(reason, reason),
        buffer_status=_BUFFER_STATUS[window_state],
        settlement_result=_settlement_result(outcome, pnl),
        pnl=pnl,
        notes=f"reprice chain of {len(chain)}" if len(chain) > 1 else "",
    )


def ledger_records(store: Store, *, market_limit: int = 50) -> tuple[LedgerRecord, ...]:
    """Assemble the ledger, newest market first.

    Bounded by markets rather than by rows: a fixed row limit would truncate the
    middle of a market's windows and show a market with three windows as having one.
    """
    records: list[LedgerRecord] = []
    for market in store.recent_markets(limit=market_limit):
        slug = str(market["slug"])
        orders = store.orders_for(slug)
        fills = store.fills_for(slug)
        intents = {i.offset_seconds: i.intent_id for i in store.intents_for(slug)}
        settlement = store.settlement_for(slug)
        for window in store.windows_for(slug):
            records.append(
                _record(
                    market,
                    window,
                    orders,
                    fills,
                    intents.get(int(window["offset_seconds"]), ""),
                    settlement.settled_at if settlement is not None else None,
                    settlement.outcome if settlement is not None else None,
                    settlement.pnl if settlement is not None else None,
                )
            )
    return tuple(records)


def search_records(
    records: tuple[LedgerRecord, ...],
    query: str = "",
    *,
    direction: str = "",
    state: str = "",
    result: str = "",
    since: float | None = None,
    until: float | None = None,
) -> tuple[LedgerRecord, ...]:
    """Filter in memory over the assembled ledger.

    In memory rather than in SQL because the record is a join across six tables and
    a WHERE clause per field would have to reproduce that join for every filter —
    six chances for the search results and the displayed rows to disagree.
    """
    needle = query.strip().lower()
    out = []
    for record in records:
        if needle and needle not in record.haystack():
            continue
        if direction and record.direction != direction:
            continue
        if state and record.state != state:
            continue
        if result and result.lower() not in record.settlement_result.lower():
            continue
        if since is not None and record.window_ts < since:
            continue
        if until is not None and record.window_ts > until:
            continue
        out.append(record)
    return tuple(out)


def ledger_totals(records: tuple[LedgerRecord, ...]) -> dict[str, Any]:
    """The Analytics counters. Informational only; nothing reads these back."""
    filled = sum(1 for r in records if r.state == OrderState.FILLED.value)
    rejected = sum(1 for r in records if r.state == OrderState.REJECTED.value)
    unsatisfied = sum(1 for r in records if r.buffer_status == BUFFER_NOT_SATISFIED)
    wins = sum(1 for r in records if r.pnl is not None and r.pnl > _ZERO)
    losses = sum(1 for r in records if r.pnl is not None and r.pnl < _ZERO)
    fill_latencies = [
        r.fill_time - r.submission_time
        for r in records
        if r.fill_time is not None and r.submission_time is not None
    ]
    return {
        "markets_processed": len({r.market for r in records}),
        "filled_orders": filled,
        "rejected_orders": rejected,
        "buffer_not_satisfied": unsatisfied,
        "win_count": wins,
        "loss_count": losses,
        "average_fill_seconds": (
            round(sum(fill_latencies) / len(fill_latencies), 3) if fill_latencies else None
        ),
    }
