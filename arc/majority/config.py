"""MAJORITY configuration, validated once and frozen.

Separate from TradingConfig on purpose. TWAP's numbers and MAJORITY's numbers
answer different questions, and a single object holding both would make "did this
edit change TWAP's risk envelope?" a question nobody can answer by reading a diff.

OPTIONAL BY CONSTRUCTION. When the MAJORITY keys are absent the engine is OFF and
the process boots exactly as it did before this package existed. That is not a
default trading value sneaking in — it is the absence of an engine. But the moment
MAJORITY is ENABLED every one of its numbers must be set explicitly, because from
that instant they reach the trading path, and a trading number the operator did not
choose is indistinguishable afterwards from one they did.

MULTI-WINDOW. A single MAJORITY config holds ANY NUMBER of independently
configured windows. Each window carries its own trigger price, target limit,
shares and entry band, and each window gets its own state object, its own intent
id, its own order and its own fill. A market's 3-second window and 90-second
window run side by side, neither reaching into the other's state, and a trigger
on one does not fire the other.

THE THREE SWITCHES. The engine has exactly three operator switches: the
combined TRIGGER + TARGET switch, the BUFFER switch and the PRICE RETRY switch
(final spec §5). The combined switch controls the editable trigger AND the
target limit price together — never two separate switches. With it ON the
window first waits for the configured Polymarket trigger price to be reached,
THEN evaluates the buffer condition, and the order is submitted at the TARGET
limit price. With it OFF the trigger price is validated but never waited on,
and the window trades the MAJORITY direction at the currently valid market
price — the majority side's live best bid — the target still validated but not
used as the submission price. The BUFFER switch turns the buffer entry
condition on and off entirely: OFF means entry never waits on BTC or TWAP
movement. PRICE RETRY enables +1/-1 tick repricing, and ONLY while the combined
switch is OFF (final spec §22): with the target price in force the order must
rest at the target, never walk a tick.

THE BUFFER'S ROLE. The buffer is a BTC-PRICE distance, never a Polymarket limit
price. With the BUFFER switch ON and a positive buffer, a window longer than
30 seconds defines two internal memory triggers — BTC reference PLUS buffer and
MINUS buffer, captured at window open — and the first one satisfied opens the
execution opportunity; the MAJORITY decision then picks the side. A window of
30 seconds or less enters the TWAP-supported entry calculation. Buffer ON with
a ZERO value must not invent movement (final spec §9): a short window uses the
running TWAP reference as the existing entry logic requires, and a long window
takes the MAJORITY direction from the best valid limit-order price without
waiting for BTC + 0 / BTC - 0. Buffer OFF means no buffer condition at all.

SCALE-AGNOSTIC NUMBERS. The trigger and target are Polymarket SHARE prices
(final spec §10-§13): the trigger is compared against the market's best bid,
the target becomes the limit price. The BTC-denominated mathematics live
elsewhere — the buffer defines memory-only BTC triggers computed at window open
and never becomes an order price. Validation checks positivity and the
relational rules (band order, band width, target inside band) and deliberately
does NOT bound values to (0, 1): an operator's deliberate configuration is not
second-guessed, and the final direction gate re-checks the price before every
submission.

PRESETS. The OPS Deck surfaces the conventional window set as one-click choices.
The presets are a UI affordance; the engine accepts any positive integer below
the market duration, so an operator typing `137` in the custom field is exactly
as valid as clicking `25`. The constants live here so a Settings page, a deck
panel and a test cannot disagree on what `15s` means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final

from arc.domain.money import dec_str, to_decimal
from arc.domain.timing import MARKET_DURATION_SECONDS
from arc.errors import ConfigInvariantError

__all__ = [
    "MAJORITY_DISABLED",
    "MAJORITY_ENGINE",
    "MAJORITY_KEYS",
    "MAJORITY_WINDOW_PRESETS",
    "EntryMode",
    "MajorityConfig",
    "MajorityWindowConfig",
    "build_majority_config",
    "env_majority_values",
]

_ZERO: Final[Decimal] = Decimal("0")

# Final spec §20: the pre-repricing attempt count is configurable in 5-10, with
# a safe default. A value outside the range is refused at configuration time —
# a retry count the operator cannot rely on is worse than none.
PRICE_RETRY_ATTEMPTS_MIN: Final[int] = 5
PRICE_RETRY_ATTEMPTS_MAX: Final[int] = 10
PRICE_RETRY_ATTEMPTS_DEFAULT: Final[int] = 5

# The engine name, stored in every MAJORITY row and carried on every MAJORITY
# event. A literal in one place: it is half of every identity this package derives,
# and two spellings of it would silently split one engine's orders into two sets
# that neither engine would then sweep.
MAJORITY_ENGINE: Final[str] = "MAJORITY"

# Conventional window lengths the OPS Deck offers as one-click presets. Sorted
# ascending so the panel renders them low-to-high without re-sorting at the call
# site, and so a Settings page that reads the same constant cannot list them in
# a different order. ANY positive integer below MARKET_DURATION_SECONDS is a
# legal custom window; presets are not a closed set.
MAJORITY_WINDOW_PRESETS: Final[tuple[int, ...]] = (3, 5, 7, 10, 15, 25, 40, 60, 90, 120)

# MAJORITY settings that live in SQLite alongside the trading values. Listed
# separately from TRADING_KEYS so a reader can see at a glance which numbers belong
# to which engine.
MAJORITY_KEYS: Final[tuple[str, ...]] = (
    "majority_enabled",
    "majority_trigger_limit_enabled",
    "majority_buffer_enabled",
    "majority_price_retry_enabled",
    "majority_price_retry_attempts",
    "majority_buffer",
    "majority_trigger_price",
    "majority_target_limit_price",
    "majority_shares",
    "majority_entry_price_min",
    "majority_entry_price_max",
    "majority_execution_windows",
)

# How a window decides WHEN its entry opportunity fires. WHICH side is traded is
# ALWAYS the MAJORITY decision taken afterwards — the mode is a timing gate only.
class EntryMode(StrEnum):
    DIRECT = "DIRECT"            # no buffer condition: fire at window open, no mathematics
    BTC_TRIGGER = "BTC_TRIGGER"  # > 30s, buffer ON > 0: BTC reference ± buffer triggers
    TWAP_SUPPORT = "TWAP_SUPPORT"  # window ≤ 30s, buffer ON: |signal TWAP - PTB| ≥ buffer

    @staticmethod
    def for_window(window: MajorityWindowConfig, *, buffer_enabled: bool) -> EntryMode:
        """The entry mode for one window, from the buffer switch, length and value.

        BUFFER OFF (final spec §7): entry never waits on BTC or TWAP movement —
        DIRECT, regardless of the stored buffer value.

        BUFFER ON with a positive buffer: 30 seconds is the boundary — a 30s
        window is TWAP-supported, a 31s window runs BTC memory triggers.

        BUFFER ON with a ZERO buffer must not invent movement (final spec §9):
        a short window uses the running TWAP reference exactly as the existing
        MAJORITY entry logic requires (|twap - ptb| ≥ 0 holds the moment a TWAP
        exists, so nothing is waited for), and a long window takes the MAJORITY
        direction from the best valid limit-order price — DIRECT — rather than
        waiting for BTC + 0 / BTC - 0.
        """
        if not buffer_enabled:
            return EntryMode.DIRECT
        if window.buffer <= _ZERO:
            if window.execution_window_seconds > 30:
                return EntryMode.DIRECT
            return EntryMode.TWAP_SUPPORT
        if window.execution_window_seconds > 30:
            return EntryMode.BTC_TRIGGER
        return EntryMode.TWAP_SUPPORT


def _bool_value(values: dict[str, str], name: str) -> bool:
    raw = str(values.get(name, "")).strip().lower()
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off", ""):
        return False
    raise ConfigInvariantError(f"{name.upper()} must be true or false, got {raw!r}")


def _require_decimal(values: dict[str, str], name: str) -> Decimal:
    raw = str(values.get(name, "")).strip()
    if not raw:
        raise ConfigInvariantError(
            f"{name.upper()} is not set but MAJORITY is enabled. There is no default: "
            "a trading value the operator did not choose must never reach the trading path."
        )
    try:
        return to_decimal(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigInvariantError(f"{name.upper()} is not a valid decimal: {raw!r}") from exc


def _require_int(values: dict[str, str], name: str) -> int:
    raw = str(values.get(name, "")).strip()
    if not raw:
        raise ConfigInvariantError(
            f"{name.upper()} is not set but MAJORITY is enabled. There is no default."
        )
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigInvariantError(f"{name.upper()} is not an integer: {raw!r}") from exc


def _parse_window_list(raw: str) -> tuple[int, ...]:
    """Parse a comma-separated window list. Deduplicated, sorted, validated.

    An empty string yields an empty tuple. A non-integer or a non-positive value
    is fatal at the top level: the operator who typed it cannot have intended it
    to reach the trading path, and the legacy `majority_execution_window_seconds`
    scalar is accepted as a single-element list for backward compatibility.
    """
    raw = (raw or "").strip()
    if not raw:
        return ()
    parts = [p.strip() for p in raw.split(",")]
    out: list[int] = []
    for p in parts:
        if not p:
            continue
        try:
            value = int(p)
        except ValueError as exc:
            raise ConfigInvariantError(
                f"MAJORITY_EXECUTION_WINDOWS contains a non-integer value {p!r}"
            ) from exc
        if value <= 0:
            raise ConfigInvariantError(
                f"MAJORITY_EXECUTION_WINDOWS contains a non-positive value {value}; "
                "every window must be a positive integer"
            )
        out.append(value)
    return tuple(sorted(set(out)))


def _split_window_values(values: dict[str, str]) -> dict[int, dict[str, str]]:
    """Carve a flat settings dict into one sub-dict per configured window.

    Two shapes are accepted:

    Multi-window (current):
        `majority_execution_windows` lists which windows are configured (e.g.
        "3,15,45"). All other MAJORITY_* keys are shared across those windows —
        every window runs the same trigger, the same limit, the same share count
        and the same band.

    Per-window override:
        `majority_w_<window>_trigger_price` and its siblings override the shared
        value for one specific window. The override keys are an extension point
        the OPS Deck uses to surface per-window inputs without exploding the
        flat settings space.

    Legacy single-window:
        The previous `majority_execution_window_seconds` scalar is honoured as a
        one-element list. New code should not write it; the loader accepts it so
        a database carrying it boots exactly as it did before this module existed.
    """
    raw_windows = values.get("majority_execution_windows", "").strip()
    legacy = values.get("majority_execution_window_seconds", "").strip()
    if raw_windows:
        windows = _parse_window_list(raw_windows)
    elif legacy:
        windows = (int(legacy),)
    else:
        windows = ()

    shared_fields = (
        "majority_buffer",
        "majority_trigger_price",
        "majority_target_limit_price",
        "majority_shares",
        "majority_entry_price_min",
        "majority_entry_price_max",
    )
    out: dict[int, dict[str, str]] = {}
    for window in windows:
        sub: dict[str, str] = {}
        for field_name in shared_fields:
            # Per-window override (`majority_w_15_trigger_price`) wins over the
            # shared key. Empty override means "fall back to the shared value".
            override = values.get(f"majority_w_{window}_{field_name.removeprefix('majority_')}")
            if override is not None and override.strip():
                sub[field_name] = override
            else:
                sub[field_name] = values.get(field_name, "")
        out[window] = sub
    return out


@dataclass(frozen=True, slots=True)
class MajorityWindowConfig:
    """One MAJORITY execution window. Frozen once validated.

    Carries every per-window number the engine needs: the offset before market
    close at which the window opens, the buffer's role inside it, the trigger
    threshold that activates the decision, the target limit the order is
    submitted at, the share count and the entry band the order is constrained to.

    `disable_reason` is the fail-closed channel for THIS window only. A window
    carrying a reason can therefore ship disabled alongside a tradable sibling,
    and the deck shows exactly which is which. The builder no longer sets it —
    the final spec gives every window length an approved entry formula — but the
    channel stays for engine-level refusals.
    """

    execution_window_seconds: int
    buffer: Decimal
    trigger_price: Decimal
    target_limit_price: Decimal
    shares: Decimal
    entry_price_min: Decimal
    entry_price_max: Decimal
    disable_reason: str = ""
    warnings: tuple[str, ...] = field(default=())

    @property
    def tradable(self) -> bool:
        return not self.disable_reason

    def as_storage_dict(self) -> dict[str, str]:
        return {
            "majority_execution_window_seconds": str(self.execution_window_seconds),
            "majority_buffer": dec_str(self.buffer),
            "majority_trigger_price": dec_str(self.trigger_price),
            "majority_target_limit_price": dec_str(self.target_limit_price),
            "majority_shares": dec_str(self.shares),
            "majority_entry_price_min": dec_str(self.entry_price_min),
            "majority_entry_price_max": dec_str(self.entry_price_max),
        }


def _build_one_window(
    window: int,
    values: dict[str, str],
    *,
    min_tradable_size: Decimal,
    tick_size: Decimal,
) -> MajorityWindowConfig:
    """Validate the per-window numbers for one window, or raise.

    Every check is fatal. Per-window errors belong here, while whole-config
    errors (no windows at all, two windows with the same offset) belong in
    `build_majority_config`.
    """
    if window <= 0:
        raise ConfigInvariantError(
            f"MAJORITY window {window}s must be positive; an offset of zero or less "
            "would place the window at or after market close"
        )
    if window >= MARKET_DURATION_SECONDS:
        raise ConfigInvariantError(
            f"MAJORITY window {window}s is not shorter than the "
            f"{MARKET_DURATION_SECONDS}s market; it would activate before the market opens"
        )

    buffer = _require_decimal(values, "majority_buffer")
    if buffer < _ZERO:
        raise ConfigInvariantError(
            f"MAJORITY_BUFFER must not be negative, got {buffer}. Use 0 for a "
            "direct entry with no buffer mathematics."
        )

    trigger = _require_decimal(values, "majority_trigger_price")
    target = _require_decimal(values, "majority_target_limit_price")
    entry_min = _require_decimal(values, "majority_entry_price_min")
    entry_max = _require_decimal(values, "majority_entry_price_max")
    shares = _require_decimal(values, "majority_shares")

    for name, price in (
        ("MAJORITY_TRIGGER_PRICE", trigger),
        ("MAJORITY_TARGET_LIMIT_PRICE", target),
    ):
        if price <= _ZERO:
            raise ConfigInvariantError(f"{name} must be positive, got {price}")

    if entry_min >= entry_max:
        raise ConfigInvariantError(
            f"MAJORITY_ENTRY_PRICE_MIN ({entry_min}) must be below "
            f"MAJORITY_ENTRY_PRICE_MAX ({entry_max}); an inverted band admits no price at all"
        )
    if entry_min <= _ZERO:
        raise ConfigInvariantError(
            f"MAJORITY_ENTRY_PRICE_MIN must be positive, got {entry_min}"
        )
    if (entry_max - entry_min) < tick_size:
        raise ConfigInvariantError(
            f"MAJORITY entry band width {entry_max - entry_min} is narrower than "
            f"TICK_SIZE {tick_size}; no quantized price could ever fall inside it"
        )

    if target > entry_max:
        raise ConfigInvariantError(
            f"MAJORITY_TARGET_LIMIT_PRICE {target} is above MAJORITY_ENTRY_PRICE_MAX "
            f"{entry_max}; the entry-band gate would deny every MAJORITY order"
        )
    if target < entry_min:
        raise ConfigInvariantError(
            f"MAJORITY_TARGET_LIMIT_PRICE {target} is below MAJORITY_ENTRY_PRICE_MIN "
            f"{entry_min}; the entry-band gate would deny every MAJORITY order"
        )

    if shares <= _ZERO:
        raise ConfigInvariantError(
            f"MAJORITY_SHARES must be positive, got {shares}"
        )
    if shares < min_tradable_size:
        raise ConfigInvariantError(
            f"MAJORITY_SHARES {shares} is below the exchange minimum {min_tradable_size}. "
            "It is refused rather than rounded up: a share count ARC raised is a "
            "position size the operator never chose."
        )

    warnings: list[str] = []
    if trigger > target:
        warnings.append(
            f"MAJORITY_TRIGGER_PRICE {dec_str(trigger)} is above "
            f"MAJORITY_TARGET_LIMIT_PRICE {dec_str(target)}. The trigger only "
            "activates the decision; the order is submitted at the target price."
        )

    return MajorityWindowConfig(
        execution_window_seconds=window,
        buffer=buffer,
        trigger_price=trigger,
        target_limit_price=target,
        shares=shares,
        entry_price_min=entry_min,
        entry_price_max=entry_max,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True, slots=True)
class MajorityConfig:
    """Validated MAJORITY configuration. Frozen once built.

    Frozen for the same reason TradingConfig is: a configuration object that can be
    mutated after validation can become invalid while the engine holds it. The
    Settings page builds a NEW instance and swaps it in, and the previous one stays
    active if the new values do not validate.

    Holds ONE OR MORE windows. A single-window configuration is just a tuple of
    length one, and `windows_by_offset` returns it in offset order so the engine
    iterates them consistently.
    """

    enabled: bool
    windows: tuple[MajorityWindowConfig, ...]
    # Spec §10: ONE combined switch for the editable trigger + target limit
    # price. ON = wait for the configured Polymarket trigger price to be
    # reached, then evaluate the buffer condition, then submit at the TARGET
    # limit price. OFF = the trigger price is validated but never waited on,
    # and the order trades the MAJORITY direction at the currently valid
    # market price (the majority side's live best bid, gated by the entry band
    # and every risk gate).
    trigger_limit_enabled: bool = False
    # Final spec §5 switch 2: the BUFFER switch. ON = the buffer entry
    # condition is active (its mode chosen by length and value, see
    # EntryMode.for_window). OFF = entry never waits on BTC or TWAP movement;
    # the stored buffer value is then not an entry condition at all.
    buffer_enabled: bool = False
    # Spec §11: optional price retry. Reprices an unfilled resting order by ONE
    # valid tick toward the book (UP +1 tick, DOWN -1 tick, band-bounded, PRICE
    # only — never size), at most MAX_PRICE_RETRIES generations, preserving
    # direction, market and window. OFF = no repricing at all. Applies ONLY
    # while the trigger/target switch is OFF (final spec §20/§22).
    price_retry_enabled: bool = False
    # Final spec §20: how many same-price fill attempts precede the first
    # reprice. Configurable in 5-10; absent reads as the safe default.
    price_retry_attempts: int = PRICE_RETRY_ATTEMPTS_DEFAULT
    disable_reason: str = ""
    warnings: tuple[str, ...] = field(default=())

    @property
    def tradable(self) -> bool:
        """Whether this configuration may produce an order at all.

        Enabled AND not fail-closed AND at least one tradable window. All three,
        because they are different facts: an operator who never turned MAJORITY
        on, an operator whose configuration was rejected, and an operator whose
        MAJORITY flag is on but who configured zero windows must not read the
        same on the deck. `all(w.tradable for w in self.windows)` would return
        True on an empty tuple by accident; the explicit `self.windows` check
        rules that out.
        """
        if not self.enabled or self.disable_reason or not self.windows:
            return False
        return all(w.tradable for w in self.windows)

    @property
    def windows_by_offset(self) -> tuple[MajorityWindowConfig, ...]:
        """The configured windows in offset order.

        Always sorted ascending. The constructor already sorts and deduplicates,
        so this property is a re-affirmation rather than a re-sort; it exists so
        callers cannot accidentally trust an unordered iteration.
        """
        return tuple(sorted(self.windows, key=lambda w: w.execution_window_seconds))

    @property
    def tradable_windows(self) -> tuple[MajorityWindowConfig, ...]:
        """The subset of windows that may actually trade.

        The engine iterates this, never `windows`, so a window carrying a
        disable_reason never enters the trigger evaluation or the order path.
        Its state still exists, as OFF — visible on the deck with its reason —
        but it does nothing.
        """
        return tuple(w for w in self.windows_by_offset if w.tradable)

    def window_for(self, offset_seconds: int) -> MajorityWindowConfig | None:
        """The configured window for this offset, or None.

        Lookup, not iteration: a 90s/15s config returns the 15s entry for 15 and
        the 90s entry for 90 and None for 45. Used by the runtime to route a
        market tick to the right per-window state object.
        """
        for w in self.windows:
            if w.execution_window_seconds == offset_seconds:
                return w
        return None

    def as_storage_dict(self) -> dict[str, str]:
        """Serialise for the settings table. All values TEXT.

        The legacy `majority_execution_window_seconds` is NOT written — the
        multi-window key is the source of truth now, and the legacy key would be
        redundant. Loaders accept either, so a process reading settings from a
        pre-multi-window database continues to see the same configuration.
        """
        out: dict[str, str] = {
            "majority_enabled": "true" if self.enabled else "false",
            "majority_trigger_limit_enabled": (
                "true" if self.trigger_limit_enabled else "false"
            ),
            "majority_buffer_enabled": (
                "true" if self.buffer_enabled else "false"
            ),
            "majority_price_retry_enabled": (
                "true" if self.price_retry_enabled else "false"
            ),
            "majority_price_retry_attempts": str(self.price_retry_attempts),
            "majority_execution_windows": ",".join(
                str(w.execution_window_seconds) for w in self.windows_by_offset
            ),
        }
        # Shared numbers — only written when at least one window is configured,
        # because the legacy single-window storage path used one value per field
        # and writing a synthetic one for an empty config would corrupt a future
        # load.
        if self.windows:
            base = self.windows_by_offset[0]
            for name, value in base.as_storage_dict().items():
                if name == "majority_execution_window_seconds":
                    continue
                out[name] = value
            for w in self.windows_by_offset:
                key_prefix = f"majority_w_{w.execution_window_seconds}_"
                for name, value in w.as_storage_dict().items():
                    if name == "majority_execution_window_seconds":
                        continue
                    out[f"{key_prefix}{name.removeprefix('majority_')}"] = value
        return out


# The configuration of a MAJORITY engine that was never configured. Every number is
# zero and `enabled` is False, so nothing here can reach the trading path: the
# engine reports OFF and no book is read, no trigger is evaluated and no intent is
# built. This is the absence of an engine, not a set of default trading values.
#
# Public so `Settings` can carry it as the default for its `majority` field. A
# process that never heard of MAJORITY then holds exactly the object a process that
# switched MAJORITY off holds, and neither can reach the trading path.
MAJORITY_DISABLED: Final[MajorityConfig] = MajorityConfig(
    enabled=False,
    windows=(),
)

_DISABLED: Final[MajorityConfig] = MAJORITY_DISABLED


def build_majority_config(
    values: dict[str, str],
    *,
    min_tradable_size: Decimal,
    tick_size: Decimal,
) -> MajorityConfig:
    """Validate the MAJORITY values, or raise ConfigInvariantError.

    `min_tradable_size` and `tick_size` arrive as arguments rather than being read
    from a TradingConfig, so this function holds no reference to TWAP's
    configuration and cannot come to depend on one of its numbers by accident. They
    are venue constraints, not TWAP policy — the venue does not have a separate
    minimum for each of ARC's engines.

    Returns a disabled configuration when MAJORITY is off or no windows are
    configured. Per-window validation errors raise; a per-window `disable_reason`
    can only come from outside the builder (engine-level refusal or a test).
    """
    if not _bool_value(values, "majority_enabled"):
        # Not configured, or deliberately off. Either way no MAJORITY number is
        # validated, because none of them can reach the trading path from here.
        return _DISABLED

    # Global switches (§10/§11, final spec §5). Absent or blank reads as False —
    # an operator who never set one gets the conservative behaviour, and garbage
    # raises.
    trigger_limit_enabled = _bool_value(values, "majority_trigger_limit_enabled")
    buffer_enabled = _bool_value(values, "majority_buffer_enabled")
    price_retry_enabled = _bool_value(values, "majority_price_retry_enabled")

    # Final spec §20: configurable pre-repricing attempt count, 5-10. Absent or
    # blank reads as the safe default — this is a default the spec itself names,
    # unlike the per-window trading numbers — while a present value outside the
    # range is refused: a retry count the operator cannot rely on is worse than
    # no retry at all.
    attempts_raw = str(values.get("majority_price_retry_attempts", "")).strip()
    if attempts_raw:
        try:
            price_retry_attempts = int(attempts_raw)
        except ValueError as exc:
            raise ConfigInvariantError(
                f"MAJORITY_PRICE_RETRY_ATTEMPTS is not an integer: {attempts_raw!r}"
            ) from exc
        if not (
            PRICE_RETRY_ATTEMPTS_MIN <= price_retry_attempts <= PRICE_RETRY_ATTEMPTS_MAX
        ):
            raise ConfigInvariantError(
                f"MAJORITY_PRICE_RETRY_ATTEMPTS must be between "
                f"{PRICE_RETRY_ATTEMPTS_MIN} and {PRICE_RETRY_ATTEMPTS_MAX}, "
                f"got {price_retry_attempts}"
            )
    else:
        price_retry_attempts = PRICE_RETRY_ATTEMPTS_DEFAULT

    per_window = _split_window_values(values)
    if not per_window:
        # MAJORITY is on but no windows were named. That is not a configuration
        # the operator intended — it would produce an engine that is enabled,
        # has no windows, and reports OFF. Returning MAJORITY_DISABLED silently
        # would mask the misconfiguration, so we honour the `enabled` flag and
        # produce an empty-windows config; the engine treats it as OFF and the
        # deck reports exactly that.
        return MajorityConfig(
            enabled=True,
            windows=(),
            trigger_limit_enabled=trigger_limit_enabled,
            buffer_enabled=buffer_enabled,
            price_retry_enabled=price_retry_enabled,
            price_retry_attempts=price_retry_attempts,
        )

    windows: list[MajorityWindowConfig] = []
    all_warnings: list[str] = []
    for offset in sorted(per_window):
        window = _build_one_window(
            offset, per_window[offset], min_tradable_size=min_tradable_size, tick_size=tick_size
        )
        windows.append(window)
        all_warnings.extend(window.warnings)

    return MajorityConfig(
        enabled=True,
        windows=tuple(windows),
        trigger_limit_enabled=trigger_limit_enabled,
        buffer_enabled=buffer_enabled,
        price_retry_enabled=price_retry_enabled,
        price_retry_attempts=price_retry_attempts,
        warnings=tuple(all_warnings),
    )


def env_majority_values(
    *,
    enabled: bool,
    execution_windows: tuple[int, ...] = (),
    trigger_limit_enabled: bool = False,
    buffer_enabled: bool = False,
    price_retry_enabled: bool = False,
    price_retry_attempts: int = PRICE_RETRY_ATTEMPTS_DEFAULT,
    buffer: str = "",
    trigger_price: str = "",
    target_limit_price: str = "",
    shares: str = "",
    entry_price_min: str = "",
    entry_price_max: str = "",
) -> dict[str, str]:
    """Project bootstrap settings into the raw-string form the builder validates.

    Keyword-only and explicit rather than taking an ArcSettings, so this module
    imports nothing from `arc.config` and the two cannot become circular.
    """
    return {
        "majority_enabled": "true" if enabled else "false",
        "majority_trigger_limit_enabled": "true" if trigger_limit_enabled else "false",
        "majority_buffer_enabled": "true" if buffer_enabled else "false",
        "majority_price_retry_enabled": "true" if price_retry_enabled else "false",
        "majority_price_retry_attempts": str(price_retry_attempts),
        "majority_execution_windows": ",".join(str(w) for w in execution_windows),
        "majority_buffer": buffer,
        "majority_trigger_price": trigger_price,
        "majority_target_limit_price": target_limit_price,
        "majority_shares": shares,
        "majority_entry_price_min": entry_price_min,
        "majority_entry_price_max": entry_price_max,
    }
