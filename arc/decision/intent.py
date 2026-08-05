"""Building the ExecutionIntent. The only construction site in the codebase.

Two rules, both about determinism.

`intent_id` is derived from the market slug and the window offset ONLY. Not from a
clock reading, not from a counter, not from uuid4. Those would all make two runs of
the same observation stream produce different ids, and the byte-identical
determinism assertion would then be untestable. The pair is already unique — SQLite
enforces UNIQUE(market_slug, offset_seconds) — so a derived id needs nothing else,
and it has the useful side effect that a retry after a crash computes the same id
and the INSERT OR IGNORE recognises it.

Every value is copied from the snapshot and the strategy decision. Nothing is
recomputed here.
"""

from __future__ import annotations

from arc.decision.snapshot import DecisionSnapshot
from arc.domain.models import ExecutionIntent
from arc.strategy.protocol import StrategyDecision

__all__ = ["build_intent", "intent_id_for"]


def intent_id_for(market_slug: str, offset_seconds: int) -> str:
    """`slug:offset`. Deterministic, and unique by the same key SQLite enforces."""
    return f"{market_slug}:{offset_seconds}"


def build_intent(
    snapshot: DecisionSnapshot,
    decision: StrategyDecision,
    *,
    strategy_id: str,
    created_at: float,
) -> ExecutionIntent:
    """Assemble the frozen, self-sufficient intent.

    `created_at` is passed in rather than read from a clock here, because this
    module must contain no clock access at all: a function that could read the time
    would be non-deterministic by construction and no test could prove otherwise.
    It is recorded but excluded from serialize() for the same reason.

    Every value execution needs is carried. Execution must never re-read mutable
    runtime state — the signal TWAP moves continuously and the MarketInstance is
    dropped at close (A11), so anything read at submission time would describe a
    different world than the one this decision was made in.
    """
    return ExecutionIntent(
        market_slug=snapshot.market_slug,
        offset_seconds=snapshot.offset_seconds,
        direction=snapshot.direction,
        signal_twap=snapshot.signal_twap,
        locked_trigger=snapshot.locked_trigger,
        created_at=created_at,
        intent_id=intent_id_for(snapshot.market_slug, snapshot.offset_seconds),
        opening_twap=snapshot.opening_twap,
        ptb=snapshot.ptb,
        buffer=snapshot.buffer,
        limit_price=decision.limit_price,
        size=decision.size,
        strategy_id=strategy_id,
        close_ts=snapshot.close_ts,
    )
