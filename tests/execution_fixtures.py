"""Shared builders for the Limit Order Engine tests.

Plain functions, not pytest fixtures, so each test states the one thing it varies
and inherits a valid everything-else.

Nothing internal is mocked. The real Store, the real Submitter, the real FillEngine,
the real Sweeper, the real Reconciler and the real order FSM are used throughout.
The only substituted component is the venue itself, and the substitute is the V1
paper adapter — which is production code, not a test double.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from conftest import OFFSETS, WINDOW_TS

from arc.domain.enums import Direction
from arc.domain.models import ExecutionIntent, MarketInstance
from arc.domain.timing import slug_for
from arc.execution.fill_engine import FillEngine
from arc.execution.ratelimit import TokenBucket
from arc.execution.reconcile import Reconciler
from arc.execution.submit import Submitter
from arc.execution.sweep import Sweeper
from arc.execution.v1_paper import PaperExecutor
from arc.storage.store import Store

__all__ = [
    "LIMIT_PRICE",
    "MINIMUM",
    "OFFSETS",
    "WINDOW_TS",
    "bucket",
    "fill_engine",
    "intent_for",
    "make_market",
    "reconciler",
    "store_at",
    "submitter",
    "sweeper",
]

LIMIT_PRICE = Decimal("0.70")
MINIMUM = Decimal("5")


def store_at(tmp_path: Path, name: str = "arc.db") -> Store:
    """A migrated store. Named so a restart test can reopen the same file."""
    store = Store(tmp_path / name)
    store.migrate(1.0)
    return store


def make_market(store: Store, window_ts: int = WINDOW_TS) -> MarketInstance:
    """Persist a market row. Orders carry a foreign key to it."""
    market = MarketInstance.create(window_ts, OFFSETS)
    store.create_market(market, float(window_ts))
    return market


def intent_for(
    *,
    window_ts: int = WINDOW_TS,
    offset_seconds: int = 3,
    direction: Direction = Direction.UP,
    size: Decimal = Decimal("35"),
    limit_price: Decimal = LIMIT_PRICE,
    intent_id: str | None = None,
) -> ExecutionIntent:
    """A frozen intent as the Decision Engine would have produced it.

    `intent_id` may be overridden at construction — never after, because
    ExecutionIntent is frozen by contract (a decision cannot be edited once made).
    """
    slug = slug_for(window_ts)
    return ExecutionIntent(
        market_slug=slug,
        offset_seconds=offset_seconds,
        direction=direction,
        signal_twap=Decimal("64010.00"),
        locked_trigger=Decimal("64011.00"),
        created_at=float(window_ts + 297),
        intent_id=intent_id if intent_id is not None else f"{slug}:{offset_seconds}",
        opening_twap=Decimal("64005.00"),
        ptb=Decimal("64000.00"),
        buffer=Decimal("1.00"),
        limit_price=limit_price,
        size=size,
        strategy_id="arc_twap_locked_buffer",
        close_ts=window_ts + 300,
    )


def bucket(now: float = float(WINDOW_TS)) -> TokenBucket:
    """A bucket wide enough that throttling never confounds a submission test."""
    return TokenBucket(sustained=1000, burst=1000, now=now)


def submitter(
    store: Store,
    executor: PaperExecutor,
    *,
    minimum: Decimal = MINIMUM,
    size_step: Decimal = Decimal("1"),
) -> Submitter:
    return Submitter(
        store,
        executor,
        bucket=bucket(),
        minimum=minimum,
        size_step=size_step,
    )


def fill_engine(store: Store, executor: PaperExecutor) -> FillEngine:
    return FillEngine(store, executor)


def sweeper(store: Store, executor: PaperExecutor) -> Sweeper:
    return Sweeper(store, executor)


def reconciler(store: Store, executor: PaperExecutor) -> Reconciler:
    return Reconciler(store, executor)
