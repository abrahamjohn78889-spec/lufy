"""The Store: the ONLY component in ARC that touches SQLite.

Every other module receives a Store and calls methods on it. Nothing else opens a
connection, writes SQL, or knows a table name. That is what makes the guarantees
in this file worth anything — a single write path can enforce "PTB is written
once" in one place instead of hoping eleven call sites remember.

Money crosses this boundary as Decimal and is stored as TEXT, always through
dec_str() so the stored form is never scientific notation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

from arc.domain.enums import (
    LIVE_ORDER_STATES,
    Direction,
    MarketPhase,
    OrderState,
    Outcome,
    WindowState,
)
from arc.domain.models import (
    ExecutionIntent,
    ExecutionWindow,
    Fill,
    MarketInstance,
    Observation,
    Order,
    Settlement,
)
from arc.domain.money import dec_str, to_decimal
from arc.errors import StorageError
from arc.storage.schema import SCHEMA_VERSION, apply_pragmas, migrate, schema_version

__all__ = ["Store"]

_LIVE_STATE_VALUES: Final[tuple[str, ...]] = tuple(sorted(s.value for s in LIVE_ORDER_STATES))


def _opt_dec(value: str | None) -> Decimal | None:
    return None if value is None else to_decimal(value)


def _opt_str(value: Decimal | None) -> str | None:
    return None if value is None else dec_str(value)


class Store:
    """SQLite persistence for ARC."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        if self._path.parent and str(self._path.parent) not in ("", "."):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the FastAPI dashboard reads on a
        # different thread from the engine loop. Writes are still serialised by
        # SQLite's own locking plus busy_timeout.
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        apply_pragmas(self._conn)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def path(self) -> Path:
        return self._path

    def migrate(self, now: float) -> int:
        return migrate(self._conn, now)

    def schema_version(self) -> int:
        return schema_version(self._conn)

    def expected_schema_version(self) -> int:
        return SCHEMA_VERSION

    def checkpoint(self) -> None:
        """Fold the WAL back into the main file.

        Called before a file-copy backup: in WAL mode the newest committed rows live
        in the -wal sidecar, so copying only the .db would produce a backup missing
        everything since the last automatic checkpoint — silently, and only
        discovered when it is restored.
        """
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── settings ─────────────────────────────────────────────────────────────

    def load_settings(self) -> dict[str, str]:
        """All persisted settings. Empty dict on first run."""
        rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def save_settings(self, values: dict[str, str], now: float) -> None:
        """Replace the stored settings.

        Written in one transaction: a partially applied settings update would
        leave, say, a new buffer set beside an old window list, which is exactly
        the orphan-buffer condition the config invariants exist to reject.
        """
        try:
            with self._conn:
                self._conn.executemany(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    [(k, v, now) for k, v in values.items()],
                )
        except sqlite3.Error as exc:
            raise StorageError(f"failed to save settings: {exc}") from exc

    def has_settings(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM settings").fetchone()
        return bool(row["n"])

    # ── runtime state ────────────────────────────────────────────────────────

    def get_runtime_state(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def set_runtime_state(self, key: str, value: str, now: float) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value, now),
            )

    # ── markets ──────────────────────────────────────────────────────────────

    def create_market(self, market: MarketInstance, now: float) -> bool:
        """Insert a market row. Returns False if the slug already exists.

        INSERT OR IGNORE rather than upsert: rediscovering a market that is
        already underway must not reset its phase or blank its PTB.
        """
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO markets "
                    "(slug, window_ts, close_ts, phase, running_sum, observation_count, "
                    " dead_reason, archived, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        market.slug,
                        market.window_ts,
                        market.close_ts,
                        market.phase.value,
                        dec_str(market.running_sum),
                        market.observation_count,
                        market.dead_reason,
                        now,
                        now,
                    ),
                )
                for window in market.windows_by_priority():
                    self._conn.execute(
                        "INSERT OR IGNORE INTO windows "
                        "(market_slug, offset_seconds, state) VALUES (?, ?, ?)",
                        (market.slug, window.offset_seconds, window.state.value),
                    )
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise StorageError(f"failed to create market {market.slug}: {exc}") from exc

    def save_ptb(self, slug: str, ptb: Decimal, now: float) -> bool:
        """Write the official PTB. Returns False if one is already stored.

        The `WHERE ptb IS NULL` clause is the guarantee, not the Python check
        above it: two concurrent paths can both read NULL, but only one UPDATE can
        match. Every window in the market must use the identical frozen number
        (A12), and a second write would give later windows a different one.
        """
        try:
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE markets SET ptb = ?, ptb_frozen_at = ?, updated_at = ? "
                    "WHERE slug = ? AND ptb IS NULL",
                    (dec_str(ptb), now, now, slug),
                )
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise StorageError(f"failed to save PTB for {slug}: {exc}") from exc

    def load_ptb(self, slug: str) -> Decimal | None:
        row = self._conn.execute("SELECT ptb FROM markets WHERE slug = ?", (slug,)).fetchone()
        return None if row is None else _opt_dec(row["ptb"])

    def save_phase(self, slug: str, phase: MarketPhase, now: float, dead_reason: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE markets SET phase = ?, dead_reason = ?, updated_at = ? WHERE slug = ?",
                (phase.value, dead_reason, now, slug),
            )

    def save_accumulator(
        self, slug: str, running_sum: Decimal, observation_count: int, now: float
    ) -> None:
        """Persist the exact sum and count, never the mean.

        Storing the mean would bake one rounding into the restored state and then
        keep accumulating on top of it, so a restarted market's signal TWAP would
        diverge from an uninterrupted one (hazard H1).
        """
        with self._conn:
            self._conn.execute(
                "UPDATE markets SET running_sum = ?, observation_count = ?, updated_at = ? "
                "WHERE slug = ?",
                (dec_str(running_sum), observation_count, now, slug),
            )

    def save_settlement_twap(self, slug: str, settlement_twap: Decimal, now: float) -> None:
        """Record the VENUE's 30s mean, observationally. Feeds no decision."""
        with self._conn:
            self._conn.execute(
                "UPDATE markets SET settlement_twap = ?, updated_at = ? WHERE slug = ?",
                (dec_str(settlement_twap), now, slug),
            )

    def archive_market(self, slug: str, now: float) -> None:
        """Mark a market archived. NEVER deletes the row.

        The in-memory instance is dropped at close, but the recorded history is
        the dataset the whole waiting period exists to produce (A8/A17). Deleting
        it would throw away the only evidence of what the bot actually did.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE markets SET archived = 1, updated_at = ? WHERE slug = ?", (now, slug)
            )

    def market_exists(self, slug: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM markets WHERE slug = ?", (slug,)).fetchone()
        return row is not None

    def load_market_row(self, slug: str) -> sqlite3.Row | None:
        row = self._conn.execute("SELECT * FROM markets WHERE slug = ?", (slug,)).fetchone()
        # fetchone() is typed Any; narrow it here so callers get a real Row | None.
        return None if row is None else cast("sqlite3.Row", row)

    def unsettled_markets(self) -> tuple[str, ...]:
        """Slugs that were still in flight, for restart reconciliation."""
        rows = self._conn.execute(
            "SELECT slug FROM markets WHERE phase NOT IN (?, ?) ORDER BY window_ts",
            (MarketPhase.SETTLED.value, MarketPhase.DEAD.value),
        ).fetchall()
        return tuple(str(r["slug"]) for r in rows)

    def recent_markets(self, limit: int = 50) -> tuple[sqlite3.Row, ...]:
        rows = self._conn.execute(
            "SELECT * FROM markets ORDER BY window_ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return tuple(rows)

    def market_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM markets").fetchone()
        return int(row["n"])

    # ── observations ─────────────────────────────────────────────────────────

    def save_observation(self, slug: str, observation: Observation, received_at: float) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO observations "
                "(market_slug, ts, price, feed_id, window_seconds, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    slug,
                    observation.ts,
                    dec_str(observation.price),
                    observation.feed_id,
                    observation.window_seconds,
                    received_at,
                ),
            )

    def save_observations(
        self, slug: str, observations: Iterable[Observation], received_at: float
    ) -> int:
        """Batch insert. Returns the number written."""
        rows = [
            (
                slug,
                o.ts,
                dec_str(o.price),
                o.feed_id,
                o.window_seconds,
                received_at,
            )
            for o in observations
        ]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(
                "INSERT INTO observations "
                "(market_slug, ts, price, feed_id, window_seconds, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def observations_for(self, slug: str) -> tuple[Observation, ...]:
        rows = self._conn.execute(
            "SELECT ts, price, feed_id, window_seconds FROM observations "
            "WHERE market_slug = ? ORDER BY ts, id",
            (slug,),
        ).fetchall()
        return tuple(
            Observation(
                ts=float(r["ts"]),
                price=to_decimal(r["price"]),
                feed_id=str(r["feed_id"]),
                window_seconds=r["window_seconds"],
            )
            for r in rows
        )

    def observations_between(self, start_ts: float, end_ts: float) -> tuple[Observation, ...]:
        """Observations in a time range, for recording settlement_twap.

        Half-open [start, end) so an observation exactly at close belongs to one
        window only and cannot be counted in two adjacent markets.
        """
        rows = self._conn.execute(
            "SELECT ts, price, feed_id, window_seconds FROM observations "
            "WHERE ts >= ? AND ts < ? ORDER BY ts, id",
            (start_ts, end_ts),
        ).fetchall()
        return tuple(
            Observation(
                ts=float(r["ts"]),
                price=to_decimal(r["price"]),
                feed_id=str(r["feed_id"]),
                window_seconds=r["window_seconds"],
            )
            for r in rows
        )

    def observation_count(self, slug: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM observations WHERE market_slug = ?", (slug,)
        ).fetchone()
        return int(row["n"])

    def prune_observations(self, older_than_ts: float) -> int:
        """Delete raw observations past the retention horizon.

        Only the raw tick rows are pruned. The per-market aggregates
        (running_sum, observation_count, settlement_twap) live on the markets row
        and survive, so pruning never changes a historical signal TWAP.
        """
        with self._conn:
            cur = self._conn.execute("DELETE FROM observations WHERE ts < ?", (older_than_ts,))
            return cur.rowcount

    # ── windows ──────────────────────────────────────────────────────────────

    def save_window_frozen(self, slug: str, window: ExecutionWindow, now: float) -> bool:
        """Persist a window's five frozen values. REFUSES a partial freeze.

        Written BEFORE the order it authorises, so a crash between the freeze and
        the submission still leaves the exact trigger on disk to reload verbatim.

        A partially frozen window is refused outright rather than written with
        nulls: a row holding a real opening_twap and a null buffer would reload as
        a window that looks frozen and has no trigger, and the recovery path would
        have to invent one (A12).
        """
        missing = [
            name
            for name, value in (
                ("opening_twap", window.opening_twap),
                ("ptb", window.ptb),
                ("buffer", window.buffer),
                ("direction", window.direction),
                ("locked_trigger", window.locked_trigger),
            )
            if value is None
        ]
        if missing:
            raise StorageError(
                f"refusing to persist partially frozen window {window.offset_seconds}s "
                f"of {slug}: missing {', '.join(missing)}"
            )
        direction = window.direction
        if direction is None:  # unreachable: the missing-value check above covers it
            raise StorageError(
                f"refusing to persist window {window.offset_seconds}s of {slug}: no direction"
            )
        try:
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE windows SET state = ?, opening_twap = ?, ptb = ?, buffer = ?, "
                    "direction = ?, locked_trigger = ?, frozen_at = ? "
                    "WHERE market_slug = ? AND offset_seconds = ?",
                    (
                        window.state.value,
                        _opt_str(window.opening_twap),
                        _opt_str(window.ptb),
                        _opt_str(window.buffer),
                        direction.value,
                        _opt_str(window.locked_trigger),
                        window.frozen_at if window.frozen_at is not None else now,
                        slug,
                        window.offset_seconds,
                    ),
                )
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise StorageError(f"failed to save window {window.offset_seconds}s: {exc}") from exc

    def save_window_state(
        self, slug: str, offset_seconds: int, state: WindowState, fired_at: float | None = None
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE windows SET state = ?, fired_at = COALESCE(?, fired_at) "
                "WHERE market_slug = ? AND offset_seconds = ?",
                (state.value, fired_at, slug, offset_seconds),
            )

    def restore_frozen(self, slug: str, offset_seconds: int) -> dict[str, Any] | None:
        """Reload one window's frozen values VERBATIM. None if never frozen.

        Returns direction and locked_trigger as STORED VALUES. Nothing here
        recomputes them and this method has no access to a current TWAP with which
        to try. Recomputing the trigger after a restart produces a different
        number from the one the window actually froze, and the bot then trades a
        strategy that was never configured while looking entirely healthy (A4).
        """
        row = self._conn.execute(
            "SELECT state, opening_twap, ptb, buffer, direction, locked_trigger, "
            "frozen_at, fired_at FROM windows WHERE market_slug = ? AND offset_seconds = ?",
            (slug, offset_seconds),
        ).fetchone()
        if row is None or row["locked_trigger"] is None or row["direction"] is None:
            return None
        return {
            "state": WindowState(row["state"]),
            "opening_twap": _opt_dec(row["opening_twap"]),
            "ptb": _opt_dec(row["ptb"]),
            "buffer": _opt_dec(row["buffer"]),
            "direction": Direction(row["direction"]),
            "locked_trigger": to_decimal(row["locked_trigger"]),
            "frozen_at": row["frozen_at"],
            "fired_at": row["fired_at"],
        }

    def window_state(self, slug: str, offset_seconds: int) -> WindowState | None:
        """One window's persisted state. None when no row exists.

        Exists for the terminal states that carry NO frozen values — NO_DIRECTION is
        the only one — because restore_frozen deliberately refuses a row with a null
        trigger. Without this, a NO_DIRECTION window would reload as PENDING after a
        restart and the next pass would freeze it against a later TWAP, determining
        direction a second time from a value the contract forbids consulting.
        """
        row = self._conn.execute(
            "SELECT state FROM windows WHERE market_slug = ? AND offset_seconds = ?",
            (slug, offset_seconds),
        ).fetchone()
        if row is None:
            return None
        return WindowState(row["state"])

    def windows_for(self, slug: str) -> tuple[sqlite3.Row, ...]:
        rows = self._conn.execute(
            "SELECT * FROM windows WHERE market_slug = ? ORDER BY offset_seconds", (slug,)
        ).fetchall()
        return tuple(rows)

    # ── intents ──────────────────────────────────────────────────────────────

    def save_intent(self, intent: ExecutionIntent) -> bool:
        """Record an execution intent. Returns False if the window already has one.

        Arbitration is the UNIQUE(market_slug, offset_seconds) constraint, not an
        in-memory guard, so exactly-one-per-window holds across a crash between
        the decision and the submission (A12).

        Every snapshot column is written in the same statement. Persisting the
        decision without the price and size it was made with would leave a
        recovered intent unable to be submitted without recomputing them from
        state that has since moved.
        """
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO intents "
                    "(intent_id, market_slug, offset_seconds, direction, signal_twap, "
                    " locked_trigger, created_at, opening_twap, ptb, buffer, "
                    " limit_price, size, strategy_id, close_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        intent.intent_id or f"{intent.market_slug}:{intent.offset_seconds}",
                        intent.market_slug,
                        intent.offset_seconds,
                        intent.direction.value,
                        dec_str(intent.signal_twap),
                        dec_str(intent.locked_trigger),
                        intent.created_at,
                        dec_str(intent.opening_twap),
                        dec_str(intent.ptb),
                        dec_str(intent.buffer),
                        dec_str(intent.limit_price),
                        dec_str(intent.size),
                        intent.strategy_id,
                        intent.close_ts,
                    ),
                )
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise StorageError(f"failed to save intent: {exc}") from exc

    def intents_for(self, slug: str) -> tuple[ExecutionIntent, ...]:
        rows = self._conn.execute(
            "SELECT * FROM intents WHERE market_slug = ? ORDER BY offset_seconds", (slug,)
        ).fetchall()
        return tuple(
            ExecutionIntent(
                market_slug=str(r["market_slug"]),
                offset_seconds=int(r["offset_seconds"]),
                direction=Direction(r["direction"]),
                signal_twap=to_decimal(r["signal_twap"]),
                locked_trigger=to_decimal(r["locked_trigger"]),
                created_at=float(r["created_at"]),
                intent_id=str(r["intent_id"]),
                opening_twap=to_decimal(r["opening_twap"]),
                ptb=to_decimal(r["ptb"]),
                buffer=to_decimal(r["buffer"]),
                limit_price=to_decimal(r["limit_price"]),
                size=to_decimal(r["size"]),
                strategy_id=str(r["strategy_id"]),
                close_ts=int(r["close_ts"]),
            )
            for r in rows
        )

    def has_intent(self, slug: str, offset_seconds: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM intents WHERE market_slug = ? AND offset_seconds = ?",
            (slug, offset_seconds),
        ).fetchone()
        return row is not None

    # ── orders ───────────────────────────────────────────────────────────────

    def save_order(self, order: Order) -> None:
        """Insert or update an order row. Written BEFORE submission."""
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO orders (order_id, market_slug, offset_seconds, direction, "
                    " price, size, state, filled_size, venue_order_id, reprice_chain_id, "
                    " rejection_reason, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(order_id) DO UPDATE SET state=excluded.state, "
                    " filled_size=excluded.filled_size, venue_order_id=excluded.venue_order_id, "
                    " rejection_reason=excluded.rejection_reason, updated_at=excluded.updated_at",
                    (
                        order.order_id,
                        order.market_slug,
                        order.offset_seconds,
                        order.direction.value,
                        dec_str(order.price),
                        dec_str(order.size),
                        order.state.value,
                        dec_str(order.filled_size),
                        order.venue_order_id,
                        order.reprice_chain_id,
                        order.rejection_reason,
                        order.created_at,
                        order.updated_at,
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"failed to save order {order.order_id}: {exc}") from exc

    def save_order_state(
        self, order_id: str, state: OrderState, now: float, rejection_reason: str = ""
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE orders SET state = ?, rejection_reason = ?, updated_at = ? "
                "WHERE order_id = ?",
                (state.value, rejection_reason, now, order_id),
            )

    def live_orders(self, slug: str | None = None) -> tuple[Order, ...]:
        """Orders that may still be resting on the book.

        INCLUDES INDETERMINATE. An unacknowledged cancel might still be live, and
        omitting it here would let the cancellation sweep skip it and carry an
        unhedged position into settlement (A13).
        """
        placeholders = ",".join("?" for _ in _LIVE_STATE_VALUES)
        params: list[Any] = list(_LIVE_STATE_VALUES)
        sql = f"SELECT * FROM orders WHERE state IN ({placeholders})"
        if slug is not None:
            sql += " AND market_slug = ?"
            params.append(slug)
        sql += " ORDER BY created_at"
        rows = self._conn.execute(sql, params).fetchall()
        return tuple(self._order_from_row(r) for r in rows)

    def orders_for(self, slug: str) -> tuple[Order, ...]:
        rows = self._conn.execute(
            "SELECT * FROM orders WHERE market_slug = ? ORDER BY created_at", (slug,)
        ).fetchall()
        return tuple(self._order_from_row(r) for r in rows)

    def local_order_id(self, slug: str, venue_order_id: str) -> str:
        """ARC's derived order id for a venue id, or "" when it is not ours.

        The V2 adapter's only way back from a venue row to a local row: the CLOB
        assigns its own ids and documents no client-id field. Read from SQLite
        rather than from a process-local dict, because the process that most needs
        this mapping is the one that just restarted holding nothing in memory —
        without it every fill after a restart would be recorded as unlinked and
        would never advance the order it belongs to.
        """
        if not venue_order_id:
            return ""
        row = self._conn.execute(
            "SELECT order_id FROM orders WHERE market_slug = ? AND venue_order_id = ?",
            (slug, venue_order_id),
        ).fetchone()
        return "" if row is None else str(row["order_id"])

    @staticmethod
    def _order_from_row(row: sqlite3.Row) -> Order:
        return Order(
            order_id=str(row["order_id"]),
            market_slug=str(row["market_slug"]),
            offset_seconds=int(row["offset_seconds"]),
            direction=Direction(row["direction"]),
            price=to_decimal(row["price"]),
            size=to_decimal(row["size"]),
            state=OrderState(row["state"]),
            filled_size=to_decimal(row["filled_size"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            venue_order_id=str(row["venue_order_id"]),
            reprice_chain_id=str(row["reprice_chain_id"]),
            rejection_reason=str(row["rejection_reason"]),
        )

    # ── fills ────────────────────────────────────────────────────────────────

    def save_fill(self, fill: Fill) -> bool:
        """Record a fill idempotently. Returns False if fill_id already existed.

        INSERT OR IGNORE on the venue's fill_id: a websocket redelivery of the
        same fill would otherwise double the recorded position and the realised
        P/L, and the quota would decrement against phantom quantity.
        """
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO fills "
                    "(fill_id, order_id, market_slug, size, price, ts) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        fill.fill_id,
                        fill.order_id,
                        fill.market_slug,
                        dec_str(fill.size),
                        dec_str(fill.price),
                        fill.ts,
                    ),
                )
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise StorageError(f"failed to save fill {fill.fill_id}: {exc}") from exc

    def fills_for(self, slug: str) -> tuple[Fill, ...]:
        rows = self._conn.execute(
            "SELECT * FROM fills WHERE market_slug = ? ORDER BY ts", (slug,)
        ).fetchall()
        return tuple(
            Fill(
                fill_id=str(r["fill_id"]),
                order_id=str(r["order_id"]),
                market_slug=str(r["market_slug"]),
                size=to_decimal(r["size"]),
                price=to_decimal(r["price"]),
                ts=float(r["ts"]),
            )
            for r in rows
        )

    def filled_size_for_window(self, slug: str, offset_seconds: int) -> Decimal:
        """Cumulative filled quantity across a window's ENTIRE reprice chain.

        Summed over every order for the window, because a cancel-then-place
        reprice produces several order IDs for one logical position. Counting
        orders instead of quantity would let five sub-minimum fills open five
        positions against a three-trade budget (hazard H4).
        """
        rows = self._conn.execute(
            "SELECT f.size AS size FROM fills f JOIN orders o ON o.order_id = f.order_id "
            "WHERE o.market_slug = ? AND o.offset_seconds = ?",
            (slug, offset_seconds),
        ).fetchall()
        total = Decimal("0")
        for r in rows:
            total += to_decimal(r["size"])
        return total

    # ── settlements ──────────────────────────────────────────────────────────

    def save_settlement(self, settlement: Settlement) -> bool:
        """Record the venue's resolution. Returns False if already recorded.

        outcome is whatever the venue reported. It is never derived from ARC's own
        signal TWAP; where they disagree the venue wins and the divergence is
        logged separately (A12).
        """
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO settlements "
                    "(market_slug, outcome, settlement_twap, ptb, pnl, divergence_logged, "
                    " settled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        settlement.market_slug,
                        settlement.outcome.value,
                        _opt_str(settlement.settlement_twap),
                        _opt_str(settlement.ptb),
                        dec_str(settlement.pnl),
                        int(settlement.divergence_logged),
                        settlement.settled_at,
                    ),
                )
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise StorageError(f"failed to save settlement: {exc}") from exc

    def settlement_for(self, slug: str) -> Settlement | None:
        row = self._conn.execute(
            "SELECT * FROM settlements WHERE market_slug = ?", (slug,)
        ).fetchone()
        if row is None:
            return None
        return Settlement(
            market_slug=str(row["market_slug"]),
            outcome=Outcome(row["outcome"]),
            settlement_twap=_opt_dec(row["settlement_twap"]),
            ptb=_opt_dec(row["ptb"]),
            settled_at=float(row["settled_at"]),
            pnl=to_decimal(row["pnl"]),
            divergence_logged=bool(row["divergence_logged"]),
        )

    def settlement_history(self, limit: int = 100) -> tuple[Settlement, ...]:
        rows = self._conn.execute(
            "SELECT * FROM settlements ORDER BY settled_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return tuple(
            Settlement(
                market_slug=str(r["market_slug"]),
                outcome=Outcome(r["outcome"]),
                settlement_twap=_opt_dec(r["settlement_twap"]),
                ptb=_opt_dec(r["ptb"]),
                settled_at=float(r["settled_at"]),
                pnl=to_decimal(r["pnl"]),
                divergence_logged=bool(r["divergence_logged"]),
            )
            for r in rows
        )

    # ── candles ──────────────────────────────────────────────────────────────

    def save_candles(self, rows: Sequence[tuple[int, str, str, str, str, str, str]]) -> int:
        """Cache historical klines for the signal-visualisation page.

        Research data only. Nothing in the trading path reads this table (A18).
        """
        if not rows:
            return 0
        with self._conn:
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO candles (open_ts, open, high, low, close, volume, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            return cur.rowcount

    def candles_between(self, start_ts: int, end_ts: int) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._conn.execute(
                "SELECT * FROM candles WHERE open_ts >= ? AND open_ts < ? ORDER BY open_ts",
                (start_ts, end_ts),
            ).fetchall()
        )

    def candle_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM candles").fetchone()
        return int(row["n"])

    # ── integrity ────────────────────────────────────────────────────────────

    def table_names(self) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        return tuple(str(r["name"]) for r in rows)

    def integrity_check(self) -> str:
        row = self._conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0])
