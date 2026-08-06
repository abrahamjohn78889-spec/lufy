"""Shared builders for the decision-layer tests.

Not a fixture file. These are plain functions so each test can state the one thing
it varies and inherit a valid everything-else, which is what keeps a test about the
loss limit from silently also depending on the entry band.

Nothing here mocks internal business logic. The real RiskEngine, the real registry,
the real strategy and a real on-disk Store are used throughout; only the order-book
quote and the process health readings are supplied by the test, and both are genuine
external inputs the decision layer is defined to receive from its caller.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from conftest import OFFSETS, VALID_TRADING_VALUES, WINDOW_TS

from arc.config import TradingConfig, build_trading_config
from arc.decision.engine import DecisionEngine, QuoteSource, RuntimeHealth
from arc.decision.quota import QuotaLedger
from arc.domain.enums import Direction, MarketPhase, SettlementSpecStatus
from arc.domain.models import Fill, MarketInstance, Order
from arc.risk.limits import RiskLimits, limits_from_trading
from arc.storage.store import Store
from arc.strategy.config import StrategyConfig, config_from_trading
from arc.strategy.registry import default_registry

__all__ = [
    "BASE_PTB",
    "DEFAULT_QUOTE",
    "OFFSETS",
    "WINDOW_TS",
    "fill_window",
    "fired_market",
    "fresh_store",
    "healthy",
    "limits",
    "make_engine",
    "quote",
    "strategy_config",
    "trading",
]

BASE_PTB = Decimal("64000.00")
DEFAULT_QUOTE = Decimal("0.70")


def trading(**overrides: str) -> TradingConfig:
    """A configuration that passes every invariant, with named overrides."""
    values = dict(VALID_TRADING_VALUES)
    values.update(overrides)
    return build_trading_config(values)


def strategy_config(config: TradingConfig | None = None) -> StrategyConfig:
    return config_from_trading(config if config is not None else trading())


def limits(config: TradingConfig | None = None) -> RiskLimits:
    return limits_from_trading(config if config is not None else trading())


def healthy(**overrides: object) -> RuntimeHealth:
    """A runtime in which every gate that can pass, passes.

    trading_enabled, VERIFIED and execution_armed are stated explicitly rather
    than defaulted, because all three ship DISABLED (A8, and the operator gate
    disarms on every startup) and a helper that quietly enabled trading would make
    the A8 boundary test and the arming test pass for the wrong reason.
    """
    base: dict[str, object] = {
        "trading_enabled": True,
        "spec_status": SettlementSpecStatus.VERIFIED,
        "execution_armed": True,
    }
    base.update(overrides)
    return RuntimeHealth(**base)  # type: ignore[arg-type]


def quote(price: Decimal | None = DEFAULT_QUOTE) -> QuoteSource:
    """A fixed book price for either side. The only external input mocked."""

    def source(slug: str, direction: Direction) -> Decimal | None:
        return price

    return source


def make_engine(
    store: Store,
    *,
    config: TradingConfig | None = None,
    quote_price: Decimal | None = DEFAULT_QUOTE,
    health: RuntimeHealth | None = None,
    health_source: Callable[[], RuntimeHealth] | None = None,
    logger: logging.Logger | None = None,
) -> DecisionEngine:
    """The real engine, wired to the real risk engine, registry and strategy.

    `health_source` is offered as well as `health` so a test can count how often the
    reading is taken, which is how the once-per-pass rule is proved without reaching
    into a private slot.
    """
    cfg = config if config is not None else trading()
    reading = health if health is not None else healthy()
    source = health_source if health_source is not None else (lambda: reading)
    return DecisionEngine(
        store,
        strategy_config=config_from_trading(cfg),
        limits=limits_from_trading(cfg),
        registry=default_registry(),
        quota=QuotaLedger(
            max_trades_per_market=cfg.max_trades_per_market,
            min_tradable_size=cfg.min_tradable_size,
        ),
        quote_source=quote(quote_price),
        health_source=source,
        logger=logger,
    )


def fired_market(
    *,
    direction: Direction = Direction.UP,
    offsets: tuple[int, ...] = OFFSETS,
    fired: tuple[int, ...] = (3,),
    window_ts: int = WINDOW_TS,
    ptb: Decimal = BASE_PTB,
    config: TradingConfig | None = None,
) -> MarketInstance:
    """A market whose named windows are genuinely FROZEN and FIRED.

    Built through the real freeze() and mark_fired() rather than by assigning the
    fields, so a test cannot construct a window state the production code would
    have refused — a frozen window with an inconsistent trigger, for instance.

    The opening TWAP is placed STRICTLY on the correct side of the PTB for the
    requested direction, because freeze() derives the direction itself with strict
    comparison and refuses equality outright (NoDirectionError). An UP fixture built
    on `opening == ptb` would raise rather than freeze.
    """
    cfg = config if config is not None else trading()
    market = MarketInstance.create(window_ts, offsets)
    market.phase = MarketPhase.ACTIVE
    market.freeze_ptb(ptb)
    gap = Decimal("100")
    opening = ptb + gap if direction is Direction.UP else ptb - gap
    # The signal TWAP must satisfy every frozen trigger, so it is pushed one whole
    # buffer past the widest one in the firing direction.
    widest = max(cfg.buffer_for(o) for o in offsets)
    if direction is Direction.UP:
        market.accumulator.add(opening + widest * 2)
    else:
        market.accumulator.add(opening - widest * 2)
    for offset in offsets:
        window = market.window(offset)
        window.freeze(
            opening_twap=opening,
            ptb=ptb,
            buffer=cfg.buffer_for(offset),
            frozen_at=float(window_ts),
        )
    for offset in fired:
        market.window(offset).mark_fired(float(window_ts + 1))
    return market


def fill_window(
    market: MarketInstance,
    offset_seconds: int,
    *,
    size: Decimal,
    price: Decimal = DEFAULT_QUOTE,
    order_suffix: str = "a",
) -> None:
    """Attach one order and one fill of `size` to a window.

    Two calls with different suffixes produce a two-order reprice chain, which is
    what makes it possible to test that the quota sums quantity across the chain
    rather than counting orders (hazard H4).
    """
    order_id = f"{market.slug}:{offset_seconds}:{order_suffix}"
    market.orders.append(
        Order(
            order_id=order_id,
            market_slug=market.slug,
            offset_seconds=offset_seconds,
            direction=market.window(offset_seconds).direction or Direction.UP,
            price=price,
            size=size,
            filled_size=size,
        )
    )
    market.fills.append(
        Fill(
            fill_id=f"f:{order_id}",
            order_id=order_id,
            market_slug=market.slug,
            size=size,
            price=price,
            ts=float(market.window_ts),
        )
    )


def fresh_store(tmp_path: Path, name: str = "arc.db") -> Store:
    store = Store(tmp_path / name)
    store.migrate(0.0)
    return store
