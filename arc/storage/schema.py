"""SQLite schema and migrations.

Eleven tables. Nine come from the phase scope; two more follow directly from
operator decisions in this phase:

    settings      the Settings page is the source of truth after first run, so the
                  configuration has to live in the database rather than in .env
    observations  every raw Chainlink observation is persisted, which is what will
                  let U1 and U4 be answered from real data instead of guessed (A8)

There is no `events` table and no `audit_log` table. Event sourcing is removed as
an architecture (A4); what survives is the plain habit of writing a row BEFORE the
action it describes, so that a restart reloads frozen values verbatim.

Every money column is TEXT. A REAL column would store 0.85 as
0.84999999999999998 and a price that round-trips through it can land on the wrong
side of an entry cap.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from arc.errors import SchemaMigrationError

__all__ = ["EXPECTED_TABLES", "SCHEMA_VERSION", "apply_pragmas", "migrate", "schema_version"]

SCHEMA_VERSION: Final[int] = 3

EXPECTED_TABLES: Final[tuple[str, ...]] = (
    "schema_migrations",
    "settings",
    "markets",
    "observations",
    "windows",
    "intents",
    "orders",
    "fills",
    "settlements",
    "candles",
    "runtime_state",
    "runtime_sessions",
)

# Tables that must NEVER exist. Asserted by the test suite: their reappearance
# would mean event-sourcing machinery had been reintroduced (A3/A4).
FORBIDDEN_TABLES: Final[tuple[str, ...]] = (
    "events",
    "event_store",
    "audit_log",
    "projections",
    "read_models",
)


def apply_pragmas(conn: sqlite3.Connection) -> None:
    """Set the connection pragmas that make the durability guarantees real.

    WAL so a reader (the dashboard) never blocks the writer (the engine) at the
    moment a window is freezing.

    synchronous=FULL because the entire restart-recovery guarantee rests on a
    frozen window's row being on disk before the order it authorises goes out.
    Under NORMAL, a power loss can lose the last transactions in the WAL, and the
    bot would restart having placed an order it has no record of freezing.

    foreign_keys=ON so an orphan fill pointing at a market that was never written
    fails at insert time rather than surfacing later as a position with no market.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


_MIGRATION_1: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  REAL NOT NULL
);

-- Settings: source of truth after the first run. .env seeds this once.
-- Values are TEXT so a Decimal round-trips exactly.
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS markets (
    slug              TEXT PRIMARY KEY,
    window_ts         INTEGER NOT NULL UNIQUE,
    close_ts          INTEGER NOT NULL,
    phase             TEXT NOT NULL,
    -- Written exactly once, under `WHERE ptb IS NULL`. TEXT, never REAL.
    ptb               TEXT,
    ptb_frozen_at     REAL,
    running_sum       TEXT NOT NULL DEFAULT '0',
    observation_count INTEGER NOT NULL DEFAULT 0,
    -- The VENUE's 30s Chainlink mean. Recorded observationally; feeds no decision.
    settlement_twap   TEXT,
    dead_reason       TEXT NOT NULL DEFAULT '',
    archived          INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_window_ts ON markets(window_ts);
CREATE INDEX IF NOT EXISTS idx_markets_phase ON markets(phase);

-- Every raw observation from the official feed.
CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug     TEXT NOT NULL REFERENCES markets(slug),
    ts              REAL NOT NULL,
    price           TEXT NOT NULL,
    feed_id         TEXT NOT NULL DEFAULT '',
    -- The payload's declared lookback length. Stored so that a stream which does
    -- not carry it (the reference stream, TRAP 2) is visible in the data rather
    -- than silently producing a plausible wrong model.
    window_seconds  INTEGER,
    received_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_market ON observations(market_slug, ts);
CREATE INDEX IF NOT EXISTS idx_observations_ts ON observations(ts);

-- The five frozen values per window, written BEFORE the order they authorise.
CREATE TABLE IF NOT EXISTS windows (
    market_slug     TEXT NOT NULL REFERENCES markets(slug),
    offset_seconds  INTEGER NOT NULL,
    state           TEXT NOT NULL,
    opening_twap    TEXT,
    ptb             TEXT,
    buffer          TEXT,
    direction       TEXT,
    locked_trigger  TEXT,
    frozen_at       REAL,
    fired_at        REAL,
    PRIMARY KEY (market_slug, offset_seconds)
);

-- UNIQUE(market_slug, offset_seconds) is the intent arbiter. Enforced by the
-- database rather than in memory so it survives a crash between the decision and
-- the submission: exactly one intent per window, ever.
CREATE TABLE IF NOT EXISTS intents (
    intent_id       TEXT PRIMARY KEY,
    market_slug     TEXT NOT NULL REFERENCES markets(slug),
    offset_seconds  INTEGER NOT NULL,
    direction       TEXT NOT NULL,
    signal_twap     TEXT NOT NULL,
    locked_trigger  TEXT NOT NULL,
    created_at      REAL NOT NULL,
    UNIQUE (market_slug, offset_seconds)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id          TEXT PRIMARY KEY,
    market_slug       TEXT NOT NULL REFERENCES markets(slug),
    offset_seconds    INTEGER NOT NULL,
    direction         TEXT NOT NULL,
    price             TEXT NOT NULL,
    size              TEXT NOT NULL,
    state             TEXT NOT NULL,
    filled_size       TEXT NOT NULL DEFAULT '0',
    venue_order_id    TEXT NOT NULL DEFAULT '',
    -- Groups every order produced by one window's cancel-then-place reprice chain,
    -- so cumulative filled quantity can be summed across the chain (hazard H4).
    reprice_chain_id  TEXT NOT NULL DEFAULT '',
    rejection_reason  TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_slug);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);

-- fill_id is the primary key and inserts are INSERT OR IGNORE: a websocket
-- redelivery of the same fill must not double-count position or P/L.
CREATE TABLE IF NOT EXISTS fills (
    fill_id      TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders(order_id),
    market_slug  TEXT NOT NULL REFERENCES markets(slug),
    size         TEXT NOT NULL,
    price        TEXT NOT NULL,
    ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_market ON fills(market_slug);

-- outcome comes from the venue resolution event, never from ARC's signal TWAP.
CREATE TABLE IF NOT EXISTS settlements (
    market_slug       TEXT PRIMARY KEY REFERENCES markets(slug),
    outcome           TEXT NOT NULL,
    settlement_twap   TEXT,
    ptb               TEXT,
    pnl               TEXT NOT NULL DEFAULT '0',
    divergence_logged INTEGER NOT NULL DEFAULT 0,
    settled_at        REAL NOT NULL
);

-- Historical 5m klines for the signal-visualisation page. Research data only;
-- nothing here reaches the trading path.
CREATE TABLE IF NOT EXISTS candles (
    open_ts   INTEGER PRIMARY KEY,
    open      TEXT NOT NULL,
    high      TEXT NOT NULL,
    low       TEXT NOT NULL,
    close     TEXT NOT NULL,
    volume    TEXT NOT NULL,
    source    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""

_MIGRATION_2: Final[str] = """
-- Version 2 widens `intents` into the SELF-SUFFICIENT snapshot the execution layer
-- acts on. Before this, an intent carried only the direction and the two TWAP
-- numbers, so anything placing an order had to go back to the live MarketInstance
-- for the price, the size and the frozen reference values. Those move: the signal
-- TWAP advances with every observation and the instance itself is dropped at close
-- (A11). Re-reading them at submission time submits against different numbers than
-- the ones the decision was made on.
--
-- ALTER rather than a rebuild: an existing database carries real recorded intents
-- and dropping the table to widen it would destroy the only evidence of what was
-- decided. Every added column is NOT NULL with a default so old rows stay legal.
ALTER TABLE intents ADD COLUMN opening_twap   TEXT NOT NULL DEFAULT '0';
ALTER TABLE intents ADD COLUMN ptb            TEXT NOT NULL DEFAULT '0';
ALTER TABLE intents ADD COLUMN buffer         TEXT NOT NULL DEFAULT '0';
ALTER TABLE intents ADD COLUMN limit_price    TEXT NOT NULL DEFAULT '0';
ALTER TABLE intents ADD COLUMN size           TEXT NOT NULL DEFAULT '0';
ALTER TABLE intents ADD COLUMN strategy_id    TEXT NOT NULL DEFAULT '';
ALTER TABLE intents ADD COLUMN close_ts       INTEGER NOT NULL DEFAULT 0;
"""

_MIGRATION_3: Final[str] = """
ALTER TABLE intents ADD COLUMN trace_id TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN trace_id TEXT NOT NULL DEFAULT '';
ALTER TABLE fills ADD COLUMN trace_id TEXT NOT NULL DEFAULT '';
ALTER TABLE settlements ADD COLUMN trace_id TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS runtime_sessions (
    runtime_session_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    provider TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    start_reason TEXT NOT NULL,
    stop_reason TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    duration_seconds TEXT NOT NULL,
    markets_seen INTEGER NOT NULL,
    -- Three separate counters, straight off the Window Engine. There is no
    -- "windows opened" number in this system: a window is frozen, then fires or
    -- expires, and each of those is counted at its own transition. There is also
    -- deliberately no buffer_misses column -- windows_expired conflates "the
    -- buffer was too wide" with "the window never froze at all", so it cannot
    -- answer that question and a column named for it would be a guess.
    windows_frozen INTEGER NOT NULL,
    windows_fired INTEGER NOT NULL,
    windows_expired INTEGER NOT NULL,
    orders_submitted INTEGER NOT NULL,
    orders_filled INTEGER NOT NULL,
    fill_rate TEXT,
    reconnects INTEGER NOT NULL,
    disconnects INTEGER NOT NULL,
    recoveries INTEGER NOT NULL,
    warnings INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    final_status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runtime_sessions_started ON runtime_sessions(started_at);
"""

_MIGRATIONS: Final[dict[int, str]] = {1: _MIGRATION_1, 2: _MIGRATION_2, 3: _MIGRATION_3}


def schema_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration, or 0 on a fresh database."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    )
    if cur.fetchone() is None:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def migrate(conn: sqlite3.Connection, now: float) -> int:
    """Bring the schema to SCHEMA_VERSION. Returns the resulting version.

    Each migration runs in its own transaction and records itself in the same
    transaction. A migration that half-applies and still marks itself done would
    leave the frozen-window columns in an unknown state, which is exactly the
    condition that breaks verbatim restart recovery — so this raises rather than
    continuing on any failure.
    """
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaMigrationError(
            f"database schema version {current} is newer than this build "
            f"({SCHEMA_VERSION}); refusing to run against it"
        )

    for version in sorted(_MIGRATIONS):
        if version <= current:
            continue
        try:
            with conn:
                conn.executescript(_MIGRATIONS[version])
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, now),
                )
        except sqlite3.Error as exc:
            raise SchemaMigrationError(f"migration {version} failed: {exc}") from exc
        current = version

    return current
