"""V1 paper bankroll. The only paper accounting, read from the engines' rows.

There is no second ledger here. Starting balance and the account epoch live in the
runtime_state table; everything else — realised P&L, committed cost, open
positions — is assembled from the settlement, fill, order and market rows the
engines already write. A bankroll that kept its own counters would eventually
disagree with the Ledger, and the operator would be reading whichever one the
panel happened to render.

Committed cost is the same two halves execution_payload's exposure reads: the
entry cost of filled shares in unsettled markets, plus the notional of orders
still resting (price x remaining size). The halves cannot overlap — a fill
reduces remaining size — so nothing is counted twice.

Unrealised P&L marks each open position against the paper venue's own book. A
position with no quote is UNAVAILABLE (null), never zero: a missing mark and a
flat mark are different facts, same rule as the wallet block.

V1 only (#52). The caller renders this block null in V2, where real funds answer
through the wallet and balance blocks. One accounting system per mode.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from arc.domain.enums import Direction
from arc.domain.money import dec_str
from arc.errors import ArcError
from arc.majority.config import MAJORITY_ENGINE

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from arc.runtime.engine import ArcRuntime
    from arc.storage.store import Store

__all__ = [
    "DEFAULT_START_BALANCE",
    "KEY_PAPER_EPOCH",
    "KEY_PAPER_START",
    "paper_account",
    "reset_paper_account",
    "set_start_balance",
]

_ZERO: Final[Decimal] = Decimal("0")
DEFAULT_START_BALANCE: Final[str] = "100"

# Generic runtime_state keys; no schema change needed (spec #33).
KEY_PAPER_START: Final[str] = "paper_start_balance"
KEY_PAPER_EPOCH: Final[str] = "paper_epoch"


def _kv_decimal(store: Store, key: str, default: str) -> Decimal:
    raw = store.get_runtime_state(key)
    if raw is None or raw == "":
        return Decimal(default)
    # The only write paths validate before storing, so a value that no longer
    # parses is row corruption. Falling back to the default renders something
    # coherent instead of taking the whole status document down.
    try:
        return Decimal(raw)
    except InvalidOperation:
        return Decimal(default)


def set_start_balance(store: Store, value: str, now: float) -> Decimal:
    """Validate and persist a new starting bankroll. The route refuses while running."""
    try:
        start = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ArcError(f"starting bankroll must be a number, got {value!r}") from exc
    if not start.is_finite() or start <= _ZERO:
        raise ArcError(f"starting bankroll must be positive, got {value!r}")
    store.set_runtime_state(KEY_PAPER_START, dec_str(start), now)
    return start


def reset_paper_account(store: Store, now: float) -> float:
    """Move the account epoch to `now`. Settlement rows are never deleted.

    Realised P&L counts rows settled at/after the epoch, so a reset returns the
    balance to its starting value while leaving the entire history in place for
    the Ledger and Analytics to keep rendering.
    """
    store.set_runtime_state(KEY_PAPER_EPOCH, repr(now), now)
    return now


async def paper_account(run: ArcRuntime) -> dict[str, Any]:
    """The bankroll block for the status document. V1 only; see module docstring."""
    # Lazy: the engine imports api.app -> routes -> models -> this module, so a
    # top-level import here would close the circle at startup.
    from arc.runtime.engine import RuntimeStatus

    store = run.store
    start = _kv_decimal(store, KEY_PAPER_START, DEFAULT_START_BALANCE)
    epoch = float(store.get_runtime_state(KEY_PAPER_EPOCH) or "0")

    # Realised P&L: the settlement rows the settlement writer produced, newest
    # first, so the epoch boundary ends the scan rather than filtering the table.
    # "<=" because reset_paper_account stores the current clock as the new epoch,
    # and a settlement written at that exact instant belongs to the old account.
    realized = _ZERO
    wins = losses = 0
    for row in store.settlement_history(limit=100_000):
        if row.settled_at <= epoch:
            break
        if row.engine != MAJORITY_ENGINE:
            continue
        realized += row.pnl
        if row.pnl > _ZERO:
            wins += 1
        elif row.pnl < _ZERO:
            losses += 1

    unsettled = set(store.unsettled_markets())

    # Open positions per (market, direction): size and entry cost from the fills.
    # Fills carry no direction, so it is joined through the order that produced it.
    sizes: dict[tuple[str, Direction], Decimal] = {}
    costs: dict[tuple[str, Direction], Decimal] = {}
    for slug in unsettled:
        direction_of = {o.order_id: o.direction for o in store.orders_for(slug)}
        for fill in store.fills_for(slug):
            if fill.engine != MAJORITY_ENGINE:
                continue
            direction = direction_of.get(fill.order_id)
            if direction is None:
                continue
            key = (slug, direction)
            sizes[key] = sizes.get(key, _ZERO) + fill.size
            costs[key] = costs.get(key, _ZERO) + fill.size * fill.price

    # Committed: filled entry cost plus resting notional, MAJORITY only.
    committed = sum(costs.values(), _ZERO)
    for order in store.live_orders():
        if order.engine == MAJORITY_ENGINE and order.market_slug in unsettled:
            committed += order.price * order.remaining_size

    # Unrealised: mark each open position against the paper venue's book. One
    # unquoted position makes the whole figure unavailable, not partially real.
    unrealized = _ZERO
    missing_mark = False
    for (slug, direction), size in sizes.items():
        if size <= _ZERO:
            continue
        mark = await run.executor.best_price(slug, direction)
        if mark is None:
            missing_mark = True
            break
        unrealized += size * mark - costs[(slug, direction)]

    open_trades = sum(1 for size in sizes.values() if size > _ZERO)
    balance = start + realized
    return {
        "start_balance": dec_str(start),
        "epoch": epoch,
        "balance": dec_str(balance),
        "available": dec_str(balance - committed),
        "committed": dec_str(committed),
        "realized_pnl": dec_str(realized),
        "unrealized_pnl": None if missing_mark else dec_str(unrealized),
        "total_trades": wins + losses,
        "wins": wins,
        "losses": losses,
        "skipped": store.skipped_windows_since(epoch),
        "open_trades": open_trades,
        # The route enforces this independently; the flag only tells the panel
        # whether its edit and reset controls may be enabled.
        "editable": run.status == RuntimeStatus.STOPPED,
    }
