"""Configuration.

Two layers, and the order matters:

  1. .env seeds the settings ONCE, on the very first startup.
  2. From then on SQLite is the source of truth and the Settings page edits it.
     Every later startup loads the saved configuration and ignores .env for
     trading values.

An invalid configuration is FATAL and exits non-zero. That is the one thing that
DOES refuse to boot — documentation uncertainty never does (A8). The reason every
check below is fatal rather than a warning is that each describes a configuration
which would keep running and look healthy while behaving differently from what the
operator set: a window with no buffer simply never fires, an entry band narrower
than a tick admits no price at all, and nothing anywhere reports either.

There are NO default values for trading parameters. A missing buffer is an error,
never a substituted guess — an invented number that reaches the trading path is
indistinguishable, afterwards, from one the operator chose.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from arc.domain.enums import Mode
from arc.domain.money import dec_str, to_decimal
from arc.domain.timing import MARKET_DURATION_SECONDS
from arc.errors import BindAddressError, ConfigInvariantError

__all__ = ["ArcSettings", "Settings", "TradingConfig", "load_settings"]

# Trading keys that live in SQLite after first run. .env supplies them once.
TRADING_KEYS: Final[tuple[str, ...]] = (
    "execution_windows",
    "buffers",
    "position_notional_usd",
    "max_trades_per_market",
    "entry_price_min",
    "entry_price_max",
    "tick_size",
    "min_tradable_size",
    "cancel_lead_ms",
    "cancel_ack_timeout_ms",
    "feed_stale_warn_ms",
    "feed_stale_critical_ms",
    "clock_drift_warn_ms",
    "clock_drift_critical_ms",
    "outbound_rate_sustained",
    "outbound_rate_burst",
    "allow_opposing_directions",
    "observation_retention_days",
)

_SECRET_FIELDS: Final[tuple[str, ...]] = (
    "polymarket_api_key",
    "polymarket_api_secret",
    "polymarket_api_passphrase",
    "polymarket_private_key",
)


class ArcSettings(BaseSettings):
    """Bootstrap settings, read from the environment and .env."""

    model_config = SettingsConfigDict(
        env_prefix="ARC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    mode: Mode = Mode.V1

    api_bind: str = "127.0.0.1"
    api_port: int = 8080

    db_path: str = "data/arc.db"
    log_dir: str = "logs"
    observation_retention_days: int = 90

    execution_windows: str = ""
    buffers: str = ""

    position_notional_usd: str = ""
    max_trades_per_market: int = 0

    entry_price_min: str = ""
    entry_price_max: str = ""
    tick_size: str = ""
    min_tradable_size: str = ""

    cancel_lead_ms: int = 500
    cancel_ack_timeout_ms: int = 1000

    feed_stale_warn_ms: int = 3000
    feed_stale_critical_ms: int = 10000

    clock_drift_warn_ms: int = 250
    clock_drift_critical_ms: int = 1000

    outbound_rate_sustained: int = 8
    outbound_rate_burst: int = 16

    allow_opposing_directions: bool = False

    # SecretStr so the value never appears in repr(), str(), an f-string, a
    # pydantic validation error, or a traceback. Pydantic renders it as
    # '**********' everywhere; only .get_secret_value() returns the real string.
    polymarket_api_key: SecretStr = SecretStr("")
    polymarket_api_secret: SecretStr = SecretStr("")
    polymarket_api_passphrase: SecretStr = SecretStr("")
    polymarket_private_key: SecretStr = SecretStr("")

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, v: Any) -> Any:
        """Accept only V1 and V2.

        TESTNET is not a rejected member here — it does not exist in the enum at
        all, so it fails as an unknown value like any other typo (A3).
        """
        if isinstance(v, str):
            candidate = v.strip().upper()
            if candidate not in {m.value for m in Mode}:
                raise ValueError(
                    f"MODE must be one of {sorted(m.value for m in Mode)}, got {v!r}"
                )
            return candidate
        return v

    @field_validator("api_bind")
    @classmethod
    def _validate_bind(cls, v: str) -> str:
        """Refuse any non-loopback bind address.

        There is no authentication anywhere in this codebase (A3); the loopback
        bind IS the access control. Binding 0.0.0.0 would put an unauthenticated
        start/stop/settings surface for a live trading bot on the public internet.
        """
        address = v.strip()
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise BindAddressError(
                f"API_BIND must be a loopback IP address, got {address!r}. "
                "For remote access use an SSH tunnel: ssh -L 8080:localhost:8080 user@vps"
            ) from exc
        if not parsed.is_loopback:
            raise BindAddressError(
                f"API_BIND={address} is not a loopback address and is refused. "
                "ARC has no authentication; the loopback bind is what protects the "
                "dashboard. For remote access use an SSH tunnel: "
                "ssh -L 8080:localhost:8080 user@vps"
            )
        return address

    def redacted_dump(self) -> dict[str, str]:
        """Configuration for display. Secrets report SET or UNSET, never a value."""
        dump: dict[str, str] = {}
        for name in type(self).model_fields:
            if name in _SECRET_FIELDS:
                secret = getattr(self, name)
                dump[name] = "SET" if secret.get_secret_value() else "UNSET"
            else:
                dump[name] = str(getattr(self, name))
        return dump

    def secret_values(self) -> tuple[str, ...]:
        """Non-empty secret values, for the log redaction filter."""
        values = [getattr(self, n).get_secret_value() for n in _SECRET_FIELDS]
        return tuple(v for v in values if v)

    def has_credentials(self) -> bool:
        return all(getattr(self, n).get_secret_value() for n in _SECRET_FIELDS)


@dataclass(frozen=True, slots=True)
class TradingConfig:
    """Validated trading configuration. Frozen once built.

    Frozen because a configuration object that can be mutated after validation is
    a configuration that can become invalid while the engine holds it — the
    Settings page therefore builds a NEW validated instance and swaps it in, and
    the previous one stays active if the new values do not validate.
    """

    execution_windows: tuple[int, ...]
    buffers: dict[int, Decimal]
    position_notional_usd: Decimal
    max_trades_per_market: int
    entry_price_min: Decimal
    entry_price_max: Decimal
    tick_size: Decimal
    min_tradable_size: Decimal
    cancel_lead_ms: int
    cancel_ack_timeout_ms: int
    feed_stale_warn_ms: int
    feed_stale_critical_ms: int
    clock_drift_warn_ms: int
    clock_drift_critical_ms: int
    outbound_rate_sustained: int
    outbound_rate_burst: int
    allow_opposing_directions: bool
    observation_retention_days: int
    warnings: tuple[str, ...] = field(default=())

    @property
    def windows_by_priority(self) -> tuple[int, ...]:
        """3, 5, 7, 10, 15 — ascending offset, best-informed window first."""
        return tuple(sorted(self.execution_windows))

    def buffer_for(self, offset_seconds: int) -> Decimal:
        return self.buffers[offset_seconds]

    def implied_btc_move(self, offset_seconds: int) -> Decimal:
        """How far BTC must actually move for the signal TWAP to travel one buffer.

            required_BTC_deviation = buffer * (300 / window_seconds)

        Shown on the Settings buffer rows while tuning and, compactly, on the
        Active Window panel — nowhere else (A16). A buffer of 2.00 means a $60
        move on the 10s window and a $200 move on the 3s window; identical
        numbers, 3.3x different meaning.
        """
        return self.buffers[offset_seconds] * (
            Decimal(MARKET_DURATION_SECONDS) / Decimal(offset_seconds)
        )

    def as_storage_dict(self) -> dict[str, str]:
        """Serialise for the settings table. All values TEXT."""
        return {
            "execution_windows": ",".join(str(w) for w in self.windows_by_priority),
            "buffers": ",".join(
                f"{o}:{dec_str(self.buffers[o])}" for o in self.windows_by_priority
            ),
            "position_notional_usd": dec_str(self.position_notional_usd),
            "max_trades_per_market": str(self.max_trades_per_market),
            "entry_price_min": dec_str(self.entry_price_min),
            "entry_price_max": dec_str(self.entry_price_max),
            "tick_size": dec_str(self.tick_size),
            "min_tradable_size": dec_str(self.min_tradable_size),
            "cancel_lead_ms": str(self.cancel_lead_ms),
            "cancel_ack_timeout_ms": str(self.cancel_ack_timeout_ms),
            "feed_stale_warn_ms": str(self.feed_stale_warn_ms),
            "feed_stale_critical_ms": str(self.feed_stale_critical_ms),
            "clock_drift_warn_ms": str(self.clock_drift_warn_ms),
            "clock_drift_critical_ms": str(self.clock_drift_critical_ms),
            "outbound_rate_sustained": str(self.outbound_rate_sustained),
            "outbound_rate_burst": str(self.outbound_rate_burst),
            "allow_opposing_directions": "true" if self.allow_opposing_directions else "false",
            "observation_retention_days": str(self.observation_retention_days),
        }


def _parse_windows(raw: str) -> tuple[int, ...]:
    if not raw.strip():
        raise ConfigInvariantError(
            "EXECUTION_WINDOWS is empty. Set the offsets in seconds, e.g. 15,10,7,5,3"
        )
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    windows: list[int] = []
    for part in parts:
        try:
            windows.append(int(part))
        except ValueError as exc:
            raise ConfigInvariantError(
                f"EXECUTION_WINDOWS contains a non-integer offset: {part!r}"
            ) from exc
    return tuple(windows)


def _parse_buffers(raw: str) -> dict[int, Decimal]:
    if not raw.strip():
        raise ConfigInvariantError(
            "BUFFERS is empty. Set one buffer per window, e.g. 15:2.00,10:2.00,7:1.50,5:1.25,3:1.00"
        )
    buffers: dict[int, Decimal] = {}
    for part in (p.strip() for p in raw.split(",") if p.strip()):
        if ":" not in part:
            raise ConfigInvariantError(
                f"BUFFERS entry {part!r} is malformed; expected offset:buffer, e.g. 10:2.00"
            )
        offset_text, buffer_text = part.split(":", 1)
        try:
            offset = int(offset_text.strip())
        except ValueError as exc:
            raise ConfigInvariantError(
                f"BUFFERS entry {part!r} has a non-integer offset"
            ) from exc
        if offset in buffers:
            raise ConfigInvariantError(
                f"BUFFERS defines offset {offset}s more than once; "
                "the later value would silently win"
            )
        try:
            buffers[offset] = to_decimal(buffer_text.strip())
        except (TypeError, ValueError) as exc:
            raise ConfigInvariantError(
                f"BUFFERS entry {part!r} has an invalid buffer value"
            ) from exc
    return buffers


def _require_decimal(raw: str, name: str) -> Decimal:
    if not str(raw).strip():
        raise ConfigInvariantError(
            f"{name.upper()} is not set. There is no default: a trading value the "
            "operator did not choose must never reach the trading path."
        )
    try:
        return to_decimal(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigInvariantError(f"{name.upper()} is not a valid decimal: {raw!r}") from exc


def build_trading_config(values: dict[str, str]) -> TradingConfig:
    """Validate raw string values into a TradingConfig, or raise ConfigInvariantError.

    Every check here is fatal. Each one describes a configuration that would run
    and look healthy while trading differently from what was configured.
    """
    warnings: list[str] = []

    windows_raw = _parse_windows(values.get("execution_windows", ""))
    buffers = _parse_buffers(values.get("buffers", ""))

    # 1. Duplicate windows. The dict of ExecutionWindows is keyed by offset, so a
    #    duplicate would silently collapse to one window and the operator would
    #    see four windows where five were configured.
    if len(set(windows_raw)) != len(windows_raw):
        duplicates = sorted({w for w in windows_raw if windows_raw.count(w) > 1})
        raise ConfigInvariantError(
            f"EXECUTION_WINDOWS contains duplicate offsets: {duplicates}. "
            "A duplicate collapses into a single window."
        )
    windows = tuple(sorted(windows_raw))

    # 2. Window offset must be positive and inside the market.
    for offset in windows:
        if offset <= 0:
            raise ConfigInvariantError(
                f"execution window {offset}s must be positive; an offset of {offset} "
                "would place the window at or after market close"
            )
        if offset >= MARKET_DURATION_SECONDS:
            raise ConfigInvariantError(
                f"execution window {offset}s is not shorter than the {MARKET_DURATION_SECONDS}s "
                "market; it would activate before the market opens"
            )

    # 3. Missing buffer. A window with no buffer has no trigger and never fires —
    #    it would appear in the UI as a configured window that simply never acts.
    missing = [w for w in windows if w not in buffers]
    if missing:
        raise ConfigInvariantError(
            f"no buffer configured for execution window(s): {missing}. "
            "A window without a buffer can never fire."
        )

    # 4. Orphan buffer. A buffer for a window that is not enabled means the
    #    operator believes a window is active which is not.
    orphans = sorted(set(buffers) - set(windows))
    if orphans:
        raise ConfigInvariantError(
            f"buffer configured for offset(s) {orphans} which are not in EXECUTION_WINDOWS. "
            "That window does not exist and will never trade."
        )

    # 5. Zero or negative buffer. A zero buffer sets the trigger equal to the
    #    opening TWAP, so the window fires instantly and unconditionally on every
    #    market — which is not a strategy, it is an unconditional trade.
    for offset in windows:
        if buffers[offset] <= 0:
            raise ConfigInvariantError(
                f"buffer for window {offset}s is {buffers[offset]}; it must be positive. "
                "A zero buffer fires the window immediately on every market."
            )

    position_notional = _require_decimal(
        values.get("position_notional_usd", ""), "position_notional_usd"
    )
    if position_notional <= 0:
        raise ConfigInvariantError(
            f"POSITION_NOTIONAL_USD must be positive, got {position_notional}"
        )

    max_trades = _int_value(values, "max_trades_per_market")
    if max_trades <= 0:
        raise ConfigInvariantError(
            f"MAX_TRADES_PER_MARKET must be at least 1, got {max_trades}"
        )

    entry_min = _require_decimal(values.get("entry_price_min", ""), "entry_price_min")
    entry_max = _require_decimal(values.get("entry_price_max", ""), "entry_price_max")
    tick_size = _require_decimal(values.get("tick_size", ""), "tick_size")
    min_tradable = _require_decimal(values.get("min_tradable_size", ""), "min_tradable_size")

    if tick_size <= 0:
        raise ConfigInvariantError(f"TICK_SIZE must be positive, got {tick_size}")
    if min_tradable <= 0:
        raise ConfigInvariantError(f"MIN_TRADABLE_SIZE must be positive, got {min_tradable}")

    # 6. Entry band ordering. Inverted bounds admit no price at all, and the bot
    #    would run all day rejecting every order with a limit error.
    if entry_min >= entry_max:
        raise ConfigInvariantError(
            f"ENTRY_PRICE_MIN ({entry_min}) must be below ENTRY_PRICE_MAX ({entry_max}); "
            "an inverted band admits no price at all"
        )
    if entry_min <= 0 or entry_max >= 1:
        raise ConfigInvariantError(
            f"entry band {entry_min}-{entry_max} must sit strictly inside 0 and 1; "
            "prediction-market share prices are probabilities"
        )

    # 7. Band narrower than one tick. Prices are floored to the tick ladder before
    #    validation (defect D2), so a band thinner than a tick can contain no
    #    valid quantized price even though both bounds look reasonable.
    if (entry_max - entry_min) < tick_size:
        raise ConfigInvariantError(
            f"entry band width {entry_max - entry_min} is narrower than TICK_SIZE {tick_size}; "
            "no quantized price could ever fall inside it"
        )

    # 8. Budget too small to buy the exchange minimum at the worst allowed price.
    #    Without this the bot passes every check and then has every order rejected
    #    by the venue for being under the minimum size.
    max_affordable = position_notional / entry_max
    if max_affordable < min_tradable:
        raise ConfigInvariantError(
            f"POSITION_NOTIONAL_USD {position_notional} buys only {max_affordable} shares at "
            f"ENTRY_PRICE_MAX {entry_max}, below the exchange minimum {min_tradable}. "
            "Every order at the top of the band would be rejected."
        )

    cancel_lead_ms = _int_value(values, "cancel_lead_ms")
    cancel_ack_timeout_ms = _int_value(values, "cancel_ack_timeout_ms")

    # 9. Cancellation lead must be positive, or the sweep never runs and live
    #    orders ride into settlement.
    if cancel_lead_ms <= 0:
        raise ConfigInvariantError(
            f"CANCEL_LEAD_MS must be positive, got {cancel_lead_ms}; "
            "without a sweep, live orders ride into settlement"
        )
    if cancel_ack_timeout_ms <= 0:
        raise ConfigInvariantError(
            f"CANCEL_ACK_TIMEOUT_MS must be positive, got {cancel_ack_timeout_ms}"
        )

    # 10. The earliest window must not sit inside the cancellation sweep.
    #     If it did, that window would open in phase CANCELLING and the Risk
    #     Engine's phase check would deny it every single time — a window
    #     configured, displayed, and structurally incapable of ever trading.
    earliest = min(windows)
    cancel_lead_seconds = Decimal(cancel_lead_ms) / Decimal(1000)
    if Decimal(earliest) <= cancel_lead_seconds:
        raise ConfigInvariantError(
            f"execution window {earliest}s opens at or inside the cancellation sweep "
            f"({cancel_lead_seconds}s before close). That window would open in phase "
            "CANCELLING and could never submit an order. Lower CANCEL_LEAD_MS or "
            "remove the window."
        )

    feed_warn = _int_value(values, "feed_stale_warn_ms")
    feed_critical = _int_value(values, "feed_stale_critical_ms")
    if feed_warn <= 0 or feed_critical <= 0:
        raise ConfigInvariantError("feed staleness thresholds must be positive")

    # 11. Inverted staleness thresholds. If warn >= critical the warning never
    #     fires before the critical condition, removing the early notice entirely.
    if feed_warn >= feed_critical:
        raise ConfigInvariantError(
            f"FEED_STALE_WARN_MS ({feed_warn}) must be below FEED_STALE_CRITICAL_MS "
            f"({feed_critical}); inverted thresholds mean the warning never precedes the fault"
        )

    drift_warn = _int_value(values, "clock_drift_warn_ms")
    drift_critical = _int_value(values, "clock_drift_critical_ms")
    if drift_warn <= 0 or drift_critical <= 0:
        raise ConfigInvariantError("clock drift thresholds must be positive")

    # 12. Inverted drift thresholds, same reasoning.
    if drift_warn >= drift_critical:
        raise ConfigInvariantError(
            f"CLOCK_DRIFT_WARN_MS ({drift_warn}) must be below CLOCK_DRIFT_CRITICAL_MS "
            f"({drift_critical})"
        )

    rate_sustained = _int_value(values, "outbound_rate_sustained")
    rate_burst = _int_value(values, "outbound_rate_burst")
    if rate_sustained <= 0 or rate_burst <= 0:
        raise ConfigInvariantError("outbound rate limits must be positive")

    # 13. Burst below sustained. A token bucket whose capacity is under its refill
    #     rate throttles at the steady state it was configured to allow.
    if rate_burst < rate_sustained:
        raise ConfigInvariantError(
            f"OUTBOUND_RATE_BURST ({rate_burst}) must be at least "
            f"OUTBOUND_RATE_SUSTAINED ({rate_sustained}); a bucket smaller than its refill "
            "rate throttles the steady state it was meant to permit"
        )

    retention_days = _int_value(values, "observation_retention_days")
    if retention_days <= 0:
        raise ConfigInvariantError(
            f"OBSERVATION_RETENTION_DAYS must be positive, got {retention_days}"
        )

    allow_opposing = _bool_value(values, "allow_opposing_directions")

    # Advisory only. Holding both sides is a guaranteed loss, not a hedge
    # (hazard H3), but the operator is permitted to enable it deliberately.
    if allow_opposing:
        warnings.append(
            "ALLOW_OPPOSING_DIRECTIONS is enabled. Holding UP and DOWN in one market "
            "costs more than it can return: UP at 0.79 plus DOWN at 0.22 costs 1.01 "
            "and returns exactly 1.00 — a guaranteed loss of 0.01 per share."
        )

    if drift_critical >= 1000:
        warnings.append(
            f"CLOCK_DRIFT_CRITICAL_MS is {drift_critical}ms. On a 3-second window a "
            "drift near this threshold consumes a third of the window."
        )

    if cancel_ack_timeout_ms >= cancel_lead_ms:
        warnings.append(
            f"CANCEL_ACK_TIMEOUT_MS ({cancel_ack_timeout_ms}) is at least CANCEL_LEAD_MS "
            f"({cancel_lead_ms}), so a cancel can still be unacknowledged at close and the "
            "order becomes INDETERMINATE."
        )

    return TradingConfig(
        execution_windows=windows,
        buffers={w: buffers[w] for w in windows},
        position_notional_usd=position_notional,
        max_trades_per_market=max_trades,
        entry_price_min=entry_min,
        entry_price_max=entry_max,
        tick_size=tick_size,
        min_tradable_size=min_tradable,
        cancel_lead_ms=cancel_lead_ms,
        cancel_ack_timeout_ms=cancel_ack_timeout_ms,
        feed_stale_warn_ms=feed_warn,
        feed_stale_critical_ms=feed_critical,
        clock_drift_warn_ms=drift_warn,
        clock_drift_critical_ms=drift_critical,
        outbound_rate_sustained=rate_sustained,
        outbound_rate_burst=rate_burst,
        allow_opposing_directions=allow_opposing,
        observation_retention_days=retention_days,
        warnings=tuple(warnings),
    )


def _int_value(values: dict[str, str], name: str) -> int:
    raw = values.get(name, "")
    if str(raw).strip() == "":
        raise ConfigInvariantError(f"{name.upper()} is not set and has no default")
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise ConfigInvariantError(f"{name.upper()} is not an integer: {raw!r}") from exc


def _bool_value(values: dict[str, str], name: str) -> bool:
    raw = str(values.get(name, "")).strip().lower()
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off", ""):
        return False
    raise ConfigInvariantError(f"{name.upper()} must be true or false, got {raw!r}")


@dataclass(frozen=True, slots=True)
class Settings:
    """The complete validated configuration: bootstrap plus trading."""

    env: ArcSettings
    trading: TradingConfig
    seeded_from_env: bool

    @property
    def mode(self) -> Mode:
        return self.env.mode

    @property
    def db_path(self) -> Path:
        return Path(self.env.db_path)

    @property
    def log_dir(self) -> Path:
        return Path(self.env.log_dir)

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.trading.warnings

    def redacted_dump(self) -> dict[str, str]:
        return self.env.redacted_dump()


def env_trading_values(env: ArcSettings) -> dict[str, str]:
    """Extract the trading values from the bootstrap settings as raw strings."""
    return {
        "execution_windows": env.execution_windows,
        "buffers": env.buffers,
        "position_notional_usd": env.position_notional_usd,
        "max_trades_per_market": str(env.max_trades_per_market),
        "entry_price_min": env.entry_price_min,
        "entry_price_max": env.entry_price_max,
        "tick_size": env.tick_size,
        "min_tradable_size": env.min_tradable_size,
        "cancel_lead_ms": str(env.cancel_lead_ms),
        "cancel_ack_timeout_ms": str(env.cancel_ack_timeout_ms),
        "feed_stale_warn_ms": str(env.feed_stale_warn_ms),
        "feed_stale_critical_ms": str(env.feed_stale_critical_ms),
        "clock_drift_warn_ms": str(env.clock_drift_warn_ms),
        "clock_drift_critical_ms": str(env.clock_drift_critical_ms),
        "outbound_rate_sustained": str(env.outbound_rate_sustained),
        "outbound_rate_burst": str(env.outbound_rate_burst),
        "allow_opposing_directions": "true" if env.allow_opposing_directions else "false",
        "observation_retention_days": str(env.observation_retention_days),
    }


def load_settings(
    env: ArcSettings | None = None,
    stored: dict[str, str] | None = None,
) -> Settings:
    """Build the validated configuration.

    `stored` is the settings table. When it is empty this is the first run and
    .env seeds it; when it is populated it wins, because the Settings page is the
    source of truth after first run and an operator who changed a buffer in the UI
    must not have it silently reverted by a stale .env on the next restart.

    MODE=V2 without credentials is fatal here. Booting live-mode with no keys
    would produce a bot that looks armed on the dashboard and rejects every
    submission at the venue.
    """
    settings = env if env is not None else ArcSettings()

    if stored:
        merged = dict(stored)
        seeded = False
    else:
        merged = env_trading_values(settings)
        seeded = True

    trading = build_trading_config(merged)

    if settings.mode is Mode.V2 and not settings.has_credentials():
        missing = [
            n.upper()
            for n in _SECRET_FIELDS
            if not getattr(settings, n).get_secret_value()
        ]
        raise ConfigInvariantError(
            f"MODE=V2 (live) requires credentials; unset: {', '.join(missing)}. "
            "A live-mode bot without keys looks armed and rejects every submission."
        )

    return Settings(env=settings, trading=trading, seeded_from_env=seeded)
