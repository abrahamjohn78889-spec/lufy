"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from arc.clock import FrozenClock
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
    "entry_price_min": "0.55",
    "entry_price_max": "0.85",
    "tick_size": "0.01",
    "min_tradable_size": "5",
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
