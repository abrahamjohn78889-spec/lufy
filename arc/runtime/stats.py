"""Fill and submission statistics, independently per execution window offset.

WHY PER OFFSET. A buffer of 2.00 means a $60 BTC move on the 10s window and a $200
move on the 3s window — identical numbers, 3.3x different meaning. So an aggregate
fill rate across all five offsets is an average over five different strategies, and
tuning against it would move the buffer that is already correct. Every counter here
is bucketed by offset and nothing is summed across buckets except where the
aggregate is itself the answer (the run total).

DERIVED FROM STORED ROWS, NOT COUNTED LIVE. These read the orders, fills and
windows tables. In-memory counters would reset on the restart criterion 5 requires
us to perform, and a statistic that resets is a statistic that cannot answer "was
the 3s window filling before the reboot".

NOTHING HERE FEEDS A DECISION. Statistics are displayed and reported. No engine
reads them, and no threshold in the trading path is compared against them, because
a strategy that adapts to its own recent fill rate is a different strategy from the
one that was specified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from arc.domain.enums import OrderState, WindowState
from arc.storage.store import Store

__all__ = ["FillStats", "OffsetStats", "fill_statistics"]

_ZERO = Decimal("0")


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile. No interpolation.

    Interpolating would invent a latency no order actually had; the operator is
    asking which real submission was slowest, not what a model says.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(fraction * len(ordered)), len(ordered) - 1)
    return round(ordered[index], 3)


@dataclass(slots=True)
class OffsetStats:
    """One execution window offset, over every market in the audited range."""

    offset_seconds: int
    windows: int = 0
    frozen: int = 0
    fired: int = 0
    no_direction: int = 0
    buffer_not_satisfied: int = 0
    submissions: int = 0
    reprices: int = 0
    acknowledged: int = 0
    filled: int = 0
    partial: int = 0
    cancelled: int = 0
    rejected: int = 0
    indeterminate: int = 0
    filled_shares: Decimal = field(default_factory=lambda: _ZERO)
    fill_latencies_ms: list[float] = field(default_factory=list)
    submission_latencies_ms: list[float] = field(default_factory=list)
    ms_before_close: list[float] = field(default_factory=list)

    @property
    def fill_rate(self) -> float | None:
        """Filled orders per acknowledged order. None with no submissions.

        None rather than 0.0: a window that never submitted has no fill rate, and
        rendering it as zero would read as a window that submits and never fills.
        """
        if not self.submissions:
            return None
        return round(self.filled / self.submissions, 4)

    def as_json(self) -> dict[str, Any]:
        return {
            "window": f"{self.offset_seconds}s",
            "offset_seconds": self.offset_seconds,
            "windows": self.windows,
            "frozen": self.frozen,
            "fired": self.fired,
            "no_direction": self.no_direction,
            "buffer_not_satisfied": self.buffer_not_satisfied,
            "submissions": self.submissions,
            "reprices": self.reprices,
            "acknowledged": self.acknowledged,
            "filled": self.filled,
            "partial": self.partial,
            "cancelled": self.cancelled,
            "rejected": self.rejected,
            "indeterminate": self.indeterminate,
            "filled_shares": str(self.filled_shares),
            "fill_rate": self.fill_rate,
            "mean_fill_latency_ms": _mean(self.fill_latencies_ms),
            "p95_fill_latency_ms": _percentile(self.fill_latencies_ms, 0.95),
            "max_fill_latency_ms": (
                round(max(self.fill_latencies_ms), 3) if self.fill_latencies_ms else None
            ),
            "mean_submission_latency_ms": _mean(self.submission_latencies_ms),
            "p95_submission_latency_ms": _percentile(self.submission_latencies_ms, 0.95),
            "mean_ms_before_close": _mean(self.ms_before_close),
            "min_ms_before_close": (
                round(min(self.ms_before_close), 3) if self.ms_before_close else None
            ),
        }


@dataclass(slots=True)
class FillStats:
    """Every offset, plus the run totals."""

    by_offset: dict[int, OffsetStats] = field(default_factory=dict)
    markets: int = 0

    @property
    def submissions(self) -> int:
        return sum(o.submissions for o in self.by_offset.values())

    @property
    def filled(self) -> int:
        return sum(o.filled for o in self.by_offset.values())

    def as_json(self) -> dict[str, Any]:
        offsets = [self.by_offset[k].as_json() for k in sorted(self.by_offset)]
        latencies = [
            ms for o in self.by_offset.values() for ms in o.fill_latencies_ms
        ]
        return {
            "markets": self.markets,
            "submissions": self.submissions,
            "filled": self.filled,
            "fill_rate": (
                round(self.filled / self.submissions, 4) if self.submissions else None
            ),
            "mean_fill_latency_ms": _mean(latencies),
            "p95_fill_latency_ms": _percentile(latencies, 0.95),
            "by_offset": offsets,
        }


def fill_statistics(
    store: Store, *, offsets: tuple[int, ...], market_limit: int = 200
) -> FillStats:
    """Build the per-offset statistics from stored rows.

    Every configured offset gets a bucket whether or not it traded, so a window
    that produced nothing appears as a row of zeros rather than being absent. An
    absent row reads as "not configured", which is the one thing it is not.
    """
    stats = FillStats(by_offset={o: OffsetStats(offset_seconds=o) for o in offsets})
    rows = store.recent_markets(limit=market_limit)
    stats.markets = len(rows)

    for market in rows:
        slug = str(market["slug"])
        close_ts = float(market["close_ts"])
        orders = store.orders_for(slug)
        fills = store.fills_for(slug)

        first_fill: dict[str, float] = {}
        for fill in fills:
            if fill.order_id not in first_fill or fill.ts < first_fill[fill.order_id]:
                first_fill[fill.order_id] = fill.ts

        for window in store.windows_for(slug):
            offset = int(window["offset_seconds"])
            bucket = stats.by_offset.get(offset)
            if bucket is None:
                # A window offset present in the data but not in the current
                # configuration — the offsets were changed mid-history. Counted
                # rather than dropped, because dropping it would quietly shrink the
                # market count the report claims to cover.
                bucket = stats.by_offset[offset] = OffsetStats(offset_seconds=offset)
            state = WindowState(str(window["state"]))
            bucket.windows += 1
            if window["frozen_at"] is not None:
                bucket.frozen += 1
            if state is WindowState.FIRED:
                bucket.fired += 1
            elif state is WindowState.NO_DIRECTION:
                bucket.no_direction += 1
            elif state is WindowState.EXPIRED:
                bucket.buffer_not_satisfied += 1

        per_window: dict[int, int] = {}
        for order in orders:
            bucket = stats.by_offset.get(order.offset_seconds)
            if bucket is None:
                bucket = stats.by_offset[order.offset_seconds] = OffsetStats(
                    offset_seconds=order.offset_seconds
                )
            seen = per_window.get(order.offset_seconds, 0)
            per_window[order.offset_seconds] = seen + 1
            if seen:
                bucket.reprices += 1
            bucket.submissions += 1
            if order.venue_order_id:
                bucket.acknowledged += 1
            if order.state is OrderState.FILLED:
                bucket.filled += 1
            elif order.state is OrderState.PARTIAL:
                bucket.partial += 1
            elif order.state in {OrderState.CANCELLED, OrderState.EXPIRED}:
                # EXPIRED folds into cancelled here for the same reason the display
                # map does: the operator cannot act on the difference.
                bucket.cancelled += 1
            elif order.state is OrderState.REJECTED:
                bucket.rejected += 1
            elif order.state is OrderState.INDETERMINATE:
                bucket.indeterminate += 1
            bucket.filled_shares += order.filled_size
            bucket.ms_before_close.append((close_ts - order.created_at) * 1000.0)
            if order.updated_at > order.created_at:
                bucket.submission_latencies_ms.append(
                    (order.updated_at - order.created_at) * 1000.0
                )
            fill_ts = first_fill.get(order.order_id)
            if fill_ts is not None:
                bucket.fill_latencies_ms.append(max((fill_ts - order.created_at) * 1000.0, 0.0))

    return stats
