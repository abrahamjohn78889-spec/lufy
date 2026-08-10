"""Shared test fixtures."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from arc.clock import FrozenClock
from arc.logging_setup import LOGGER_NAME
from arc.storage.store import Store

# A grid-aligned timestamp: 1754400000 % 300 == 0.
WINDOW_TS = 1754400000
CLOSE_TS = WINDOW_TS + 300
OFFSETS = (15, 10, 7, 5, 3)


VALID_TRADING_VALUES: dict[str, str] = {
    "execution_windows": "15,10,7,5,3",
    "buffers": "15:2.00,10:2.00,7:1.50,5:1.25,3:1.00",
    "position_notional_usd": "25.00",
    "max_trades_per_market": "3",
    "max_concurrent_positions": "3",
    "max_daily_loss_usd": "50.00",
    "max_consecutive_losses": "5",
    "entry_price_min": "0.55",
    "entry_price_max": "0.85",
    "tick_size": "0.01",
    "min_tradable_size": "5",
    "submission_count": "1",
    "cancel_lead_ms": "500",
    "cancel_ack_timeout_ms": "400",
    "feed_stale_warn_ms": "3000",
    "feed_stale_critical_ms": "10000",
    "clock_drift_warn_ms": "250",
    "clock_drift_critical_ms": "900",
    "outbound_rate_sustained": "8",
    "outbound_rate_burst": "16",
    "allow_opposing_directions": "false",
    "observation_retention_days": "90",
}


@pytest.fixture
def trading_values() -> dict[str, str]:
    """A configuration that passes every invariant. Tests mutate a copy."""
    return dict(VALID_TRADING_VALUES)


@pytest.fixture(autouse=True)
def _restore_arc_logger() -> Iterator[None]:
    """Undo global logging mutations between tests.

    `setup_logging` deliberately sets `propagate = False` on the shared "arc" logger
    and installs file handlers on it — correct in production, where the root logger
    belongs to uvicorn and would re-render ARC lines in a foreign format.

    But `logging` is process-global. Once any test calls `setup_logging`, every
    subsequent test's `arc.*` child logger stops reaching caplog's root handler, so
    log assertions pass alone and fail in a full run purely on file ordering. Handlers
    also stay open on rotating files, which on Windows keeps tmp_path locked.

    Restoring here fixes the class of bug rather than each symptom.
    """
    logger = logging.getLogger(LOGGER_NAME)
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_handlers = list(logger.handlers)
    logger.handlers.clear()
    logger.propagate = True
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.handlers.extend(saved_handlers)
        logger.propagate = saved_propagate
        logger.setLevel(saved_level)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(now=float(WINDOW_TS))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "arc.db")
    s.migrate(1.0)
    yield s
    s.close()


@pytest.fixture
def source_root() -> Path:
    return Path(__file__).resolve().parent.parent
