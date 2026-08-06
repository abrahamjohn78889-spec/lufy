"""Runtime metrics for the validation report: eleven numbers, none of them invented.

WHY A SEPARATE MODULE. `validation.py` answers "did the run satisfy criterion N".
This answers "how did the run behave" — uptime, restarts, reconnects, latencies,
recorder size, database growth. They are different questions and the second one
must never be able to flip the first one's verdict.

WHY SO MANY OF THEM ARE UNAVAILABLE. ARC measures what it stores. It stores
submission and fill latency, so those are real numbers. It does not timestamp
individual websocket frames, CLOB round trips or RTDS responses at the transport
layer, so those are reported as UNAVAILABLE rather than as a plausible figure
derived from something adjacent. A latency number nobody measured, printed beside
ten that were, is read as measured — and the whole point of this report is that
the operator can trust what it prints.

DATABASE GROWTH IS PER MARKET, NOT PER DAY. Bytes-per-day depends on how long the
process happened to be up; bytes-per-market is the figure that projects forward,
because markets arrive every five minutes forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from arc.runtime.stats import FillStats
from arc.storage.store import Store

__all__ = ["UNAVAILABLE", "RuntimeMetrics", "runtime_metrics"]

# The one string used for every unmeasured figure, so the report cannot show two
# different phrasings for the same fact.
UNAVAILABLE: Final[str] = "UNAVAILABLE (not instrumented)"

_MARKETS_PER_DAY: Final[int] = 288  # 24h / 5min


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


@dataclass(frozen=True, slots=True)
class RuntimeMetrics:
    """The eleven figures the addendum names, plus what produced them."""

    uptime_seconds: float
    restarts: int
    reconnects: int
    websocket_latency_ms: float | str
    clob_latency_ms: float | str
    rtds_latency_ms: float | str
    chainlink_latency_ms: float | str
    order_latency_ms: float | str
    recorder_markets: int
    recorder_observations: int
    database_bytes: int
    database_bytes_per_market: float | str
    validation_duration_seconds: float | str

    def as_json(self) -> dict[str, Any]:
        return {
            "runtime_uptime_seconds": self.uptime_seconds,
            "runtime_restarts": self.restarts,
            "runtime_reconnects": self.reconnects,
            "avg_websocket_latency_ms": self.websocket_latency_ms,
            "avg_clob_latency_ms": self.clob_latency_ms,
            "avg_rtds_latency_ms": self.rtds_latency_ms,
            "avg_chainlink_latency_ms": self.chainlink_latency_ms,
            "avg_order_latency_ms": self.order_latency_ms,
            "recorder_markets": self.recorder_markets,
            "recorder_observations": self.recorder_observations,
            "database_bytes": self.database_bytes,
            "database_bytes_per_market": self.database_bytes_per_market,
            "database_bytes_per_day_projected": (
                round(self.database_bytes_per_market * _MARKETS_PER_DAY)
                if isinstance(self.database_bytes_per_market, float)
                else self.database_bytes_per_market
            ),
            "validation_duration_seconds": self.validation_duration_seconds,
        }


def _db_bytes(store: Store) -> int:
    """The .db file plus its WAL sidecar.

    Both, because in WAL mode the newest rows live in the sidecar: reporting only
    the main file would show a database that appears not to grow during a run and
    then jumps at every checkpoint.
    """
    total = 0
    for suffix in ("", "-wal", "-shm"):
        path = store.path.with_name(store.path.name + suffix)
        if path.exists():
            total += path.stat().st_size
    return total


def runtime_metrics(
    store: Store,
    stats: FillStats,
    *,
    uptime_seconds: float,
    restarts: int,
    reconnects: int,
    observations: int,
    duration_seconds: float | None = None,
    chainlink_enabled: bool = False,
) -> RuntimeMetrics:
    """Assemble the metrics block. Reads rows and file sizes; measures nothing new."""
    order_latencies = [
        ms for bucket in stats.by_offset.values() for ms in bucket.submission_latencies_ms
    ]
    markets = stats.markets
    db_bytes = _db_bytes(store)
    order_mean = _mean(order_latencies)
    return RuntimeMetrics(
        uptime_seconds=round(max(uptime_seconds, 0.0), 3),
        restarts=restarts,
        reconnects=reconnects,
        # Individual frames are not timestamped at the transport layer, so there is
        # no per-frame round trip to average. The feed's health is reported instead
        # by the watchdog age and the clock drift already on the System page.
        websocket_latency_ms=UNAVAILABLE,
        clob_latency_ms=UNAVAILABLE,
        rtds_latency_ms=UNAVAILABLE,
        chainlink_latency_ms=(
            UNAVAILABLE if chainlink_enabled else "N/A (TWAP_PROVIDER is not CHAINLINK)"
        ),
        # This one IS measured: created_at to updated_at on the order row is the
        # submit call's own round trip, stored per order.
        # Explicitly against None, not falsiness: a genuine 0.0 ms mean is a
        # measurement, and `or` would print it as UNAVAILABLE.
        order_latency_ms=UNAVAILABLE if order_mean is None else order_mean,
        recorder_markets=markets,
        recorder_observations=observations,
        database_bytes=db_bytes,
        database_bytes_per_market=round(db_bytes / markets, 1) if markets else UNAVAILABLE,
        validation_duration_seconds=(
            round(duration_seconds, 3) if duration_seconds is not None else UNAVAILABLE
        ),
    )
