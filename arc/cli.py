"""Command line interface.

    arc doctor    validate configuration and storage, print a full report
    arc observe   run the market engine with no trading, ever
    arc run       errors clearly — the trading runtime does not exist yet

`arc run` deliberately fails. The feed connection, market discovery and PTB
fetching exist as of phase 2, but the decision engine, risk engine and execution
adapters do not. A `run` that started a loop which appeared to trade would be
exactly the fake implementation that is forbidden (A1 Rule 2), and its worst
property is that it would look fine.

`arc observe` is the honest half of that: it runs everything that does exist, and it
cannot trade because no execution adapter is in its import graph at all.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from typing import TextIO

from arc.clock import Clock, SystemClock
from arc.config import ArcSettings, Settings, load_settings
from arc.domain.money import dec_str
from arc.domain.timing import (
    close_ts_for,
    format_countdown,
    settlement_determined_fraction,
    slug_for,
    window_ts_for,
)
from arc.errors import ArcFatalError
from arc.logging_setup import ensure_utf8_streams, setup_logging
from arc.runtime.observe import observe as run_observe
from arc.storage.store import Store

__all__ = ["main"]

# `observe`, `doctor` and `run` are module-level functions rather than exports so the
# entry point stays the single documented surface.

_RULE = "─" * 68


def _line(out: TextIO, label: str, value: str) -> None:
    out.write(f"  {label.ljust(28)}{value}\n")


def _section(out: TextIO, title: str) -> None:
    out.write(f"\n{title}\n{_RULE}\n")


def doctor(out: TextIO, clock: Clock) -> int:
    """Validate configuration and storage and print the report."""
    out.write(f"\nARC doctor\n{_RULE}\n")

    # 1. Configuration. Loaded before anything else so a bad config fails before
    #    a database file is created for a configuration that will never run.
    try:
        env = ArcSettings()
    except ArcFatalError as exc:
        out.write(f"\nFATAL  configuration rejected\n\n  {exc}\n\n")
        return 1
    except Exception as exc:  # pydantic ValidationError and friends
        out.write(f"\nFATAL  configuration rejected\n\n  {exc}\n\n")
        return 1

    store: Store | None = None
    stored: dict[str, str] = {}
    try:
        store = Store(env.db_path)
        version = store.migrate(clock.now())
        stored = store.load_settings()
    except ArcFatalError as exc:
        out.write(f"\nFATAL  storage rejected\n\n  {exc}\n\n")
        return 1
    except Exception as exc:
        out.write(f"\nFATAL  storage could not be opened\n\n  {exc}\n\n")
        return 1

    try:
        settings: Settings = load_settings(env, stored)
    except ArcFatalError as exc:
        out.write(f"\nFATAL  configuration rejected\n\n  {exc}\n\n")
        store.close()
        return 1

    # First run: seed the settings table from .env, but only after the values have
    # validated. Seeding before validation would persist a broken configuration
    # that then becomes the source of truth on every later startup, and .env would
    # no longer be consulted to fix it.
    if settings.seeded_from_env:
        store.save_settings(settings.trading.as_storage_dict(), clock.now())

    _report(out, settings, store, version, clock)
    store.close()

    out.write("\nPhase 1 OK\n\n")
    return 0


def _report(out: TextIO, settings: Settings, store: Store, version: int, clock: Clock) -> None:
    env = settings.env
    trading = settings.trading

    _section(out, "CONFIGURATION")
    _line(out, "Mode", f"{env.mode.value}  ({'paper' if env.mode.value == 'V1' else 'LIVE'})")
    _line(out, "Dashboard", f"http://{env.api_bind}:{env.api_port}")
    _line(out, "Remote access", f"ssh -L {env.api_port}:localhost:{env.api_port} user@vps")
    _line(out, "Config source", ".env (first run, seeded)" if settings.seeded_from_env
          else "SQLite settings table")

    _section(out, "CREDENTIALS")
    dump = settings.redacted_dump()
    for name in (
        "polymarket_api_key",
        "polymarket_api_secret",
        "polymarket_api_passphrase",
        "polymarket_private_key",
    ):
        _line(out, name.replace("polymarket_", "").replace("_", " ").title(), dump[name])

    _section(out, "STORAGE")
    _line(out, "Database", str(store.path))
    _line(out, "Schema version", f"{version} (expected {store.expected_schema_version()})")
    _line(out, "Integrity", store.integrity_check())
    _line(out, "Tables", str(len(store.table_names())))
    _line(out, "Markets recorded", str(store.market_count()))
    _line(out, "Candles cached", str(store.candle_count()))

    _section(out, "EXECUTION WINDOWS")
    out.write("  offset   buffer     implied BTC move   settlement determined\n")
    for offset in trading.windows_by_priority:
        buffer_value = trading.buffer_for(offset)
        implied = trading.implied_btc_move(offset)
        fraction = settlement_determined_fraction(offset)
        percent = (fraction * Decimal(100)).quantize(Decimal("0.1"))
        out.write(
            f"  {str(offset) + 's':<8} {dec_str(buffer_value):<10} "
            f"~${dec_str(implied.quantize(Decimal('1'))):<16} {percent}%\n"
        )
    out.write("\n  Priority order: " + " → ".join(f"{o}s" for o in trading.windows_by_priority))
    out.write("  (closest to close first — best informed)\n")

    _section(out, "TRADING PARAMETERS")
    _line(out, "Position size", f"${dec_str(trading.position_notional_usd)}")
    _line(out, "Max trades per market", str(trading.max_trades_per_market))
    _line(
        out,
        "Entry band",
        f"{dec_str(trading.entry_price_min)} - {dec_str(trading.entry_price_max)} "
        f"(tick {dec_str(trading.tick_size)})",
    )
    _line(out, "Exchange minimum", f"{dec_str(trading.min_tradable_size)} shares")
    _line(out, "Cancellation sweep", f"{trading.cancel_lead_ms} ms before close")
    _line(out, "Cancel ack timeout", f"{trading.cancel_ack_timeout_ms} ms")
    _line(out, "Opposing directions", "ALLOWED" if trading.allow_opposing_directions
          else "blocked")

    _section(out, "THRESHOLDS")
    _line(out, "Feed stale", f"warn {trading.feed_stale_warn_ms} ms  /  "
          f"critical {trading.feed_stale_critical_ms} ms")
    _line(out, "Clock drift", f"warn ±{trading.clock_drift_warn_ms} ms  /  "
          f"critical ±{trading.clock_drift_critical_ms} ms")
    _line(out, "Outbound rate", f"{trading.outbound_rate_sustained}/s sustained, "
          f"{trading.outbound_rate_burst} burst  (cancels bypass)")
    _line(out, "Observation retention", f"{trading.observation_retention_days} days")

    now = clock.now()
    window_ts = window_ts_for(now)
    close_ts = close_ts_for(window_ts)

    _section(out, "CURRENT MARKET")
    _line(out, "Slug", slug_for(window_ts))
    _line(out, "Window opened", str(window_ts))
    _line(out, "Closes", str(close_ts))
    _line(out, "Countdown", format_countdown(now, close_ts))
    _line(out, "Next market", slug_for(close_ts))

    _section(out, "WARNINGS")
    if settings.warnings:
        for warning in settings.warnings:
            out.write(f"  ⚠  {warning}\n")
    else:
        out.write("  none\n")


def observe(out: TextIO, clock: Clock, *, market_target: int) -> int:
    """Run the market engine with no trading. Startup order per A8.

    Step 1 (config) is the only step that may refuse: a bad configuration must not
    boot. Everything after it starts regardless — a feed that will not connect or a
    settlement spec that cannot be verified disables trading and records why, and the
    process stays up collecting the data that resolves the problem.
    """
    out.write(f"\nARC observe\n{_RULE}\n")

    try:
        env = ArcSettings()
    except Exception as exc:
        out.write(f"\nFATAL  configuration rejected\n\n  {exc}\n\n")
        return 1

    store: Store | None = None
    try:
        store = Store(env.db_path)
        store.migrate(clock.now())
        stored = store.load_settings()
        settings: Settings = load_settings(env, stored)
    except ArcFatalError as exc:
        out.write(f"\nFATAL  configuration rejected\n\n  {exc}\n\n")
        if store is not None:
            store.close()
        return 1
    except Exception as exc:
        out.write(f"\nFATAL  storage could not be opened\n\n  {exc}\n\n")
        if store is not None:
            store.close()
        return 1

    if settings.seeded_from_env:
        store.save_settings(settings.trading.as_storage_dict(), clock.now())

    logger = setup_logging(
        settings.log_dir,
        secrets=settings.env.secret_values(),
        retention_days=settings.trading.observation_retention_days,
    )

    try:
        return asyncio.run(
            run_observe(
                settings, store, clock, out, market_target=market_target, logger=logger
            )
        )
    except KeyboardInterrupt:
        out.write("\n  interrupted\n\n")
        return 0
    finally:
        store.close()


def run(out: TextIO) -> int:
    """Refuse to run. There is no runtime yet, and pretending otherwise is worse."""
    out.write(
        "\nARC run is not available.\n"
        "\n"
        "Phases 1 and 2 built the deterministic foundation and the market engine:\n"
        "configuration, timing, domain models, storage, clock, logging, feed\n"
        "connection, market discovery, PTB fetching and market rotation. The window\n"
        "activation loop, decision engine, risk engine, execution adapters and\n"
        "dashboard are not implemented yet.\n"
        "\n"
        "Nothing here is stubbed to make this command appear to work — a runtime\n"
        "that looked healthy while trading nothing would be worse than this error.\n"
        "\n"
        "Validate what exists with:  arc doctor\n"
        "Watch the market engine with:  arc observe\n\n"
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arc",
        description="ARC — deterministic trading bot for Polymarket 5-minute BTC Up/Down markets",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="validate configuration and storage")
    observe_parser = subparsers.add_parser(
        "observe", help="run the market engine with no trading"
    )
    observe_parser.add_argument(
        "--markets",
        type=int,
        default=3,
        help="stop after this many consecutive markets (default 3)",
    )
    subparsers.add_parser("run", help="start the trading engine (not available yet)")

    args = parser.parse_args(argv)

    # Before any output: the report and the log markers use box rules, ⚠ and ⛔,
    # which a cp1252 Windows console cannot encode.
    ensure_utf8_streams()

    if args.command == "doctor":
        return doctor(sys.stdout, SystemClock())
    if args.command == "observe":
        return observe(sys.stdout, SystemClock(), market_target=args.markets)
    return run(sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
