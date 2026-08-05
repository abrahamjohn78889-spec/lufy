"""Observation validation. REJECT, never repair.

Every check here ends in a rejection with a reason. Nothing is clamped,
interpolated, back-filled, or nudged into range.

The reason that matters: the signal TWAP is an exact cumulative mean, and a
repaired observation is indistinguishable from a real one once it has been folded
into the running sum. A clamped price would move the locked trigger by a real
amount with nothing in the record showing that a number had been invented. A
rejected observation costs one sample out of roughly three hundred and leaves a
logged reason; an invented one silently changes what the bot trades.

A rejection is not an outage either. The watchdog decides whether the gap between
accepted observations has become long enough to stop trading (see watchdog.py);
validation only ever answers "is this one sample usable".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from arc.domain.models import Observation
from arc.domain.money import to_decimal
from arc.errors import ObservationRejectedError

__all__ = [
    "REJECT_BAD_PRICE",
    "REJECT_BAD_TIMESTAMP",
    "REJECT_DEVIATION",
    "REJECT_FUTURE",
    "REJECT_MALFORMED",
    "REJECT_STALE_SAMPLE",
    "REJECT_SYMBOL",
    "ObservationValidator",
    "ValidationLimits",
    "parse_payload",
]

# Rejection reasons. Strings rather than an enum because they land in a log line
# and on the dashboard verbatim, and a reason nobody can read is a reason nobody
# acts on.
REJECT_MALFORMED: Final[str] = "MALFORMED_PAYLOAD"
REJECT_BAD_PRICE: Final[str] = "BAD_PRICE"
REJECT_BAD_TIMESTAMP: Final[str] = "BAD_TIMESTAMP"
REJECT_SYMBOL: Final[str] = "WRONG_SYMBOL"
REJECT_STALE_SAMPLE: Final[str] = "SAMPLE_TOO_OLD"
REJECT_FUTURE: Final[str] = "SAMPLE_FROM_FUTURE"
REJECT_DEVIATION: Final[str] = "IMPLAUSIBLE_DEVIATION"

# The RTDS envelope is documented but its exact key spelling is not pinned by
# anything this build can verify (A8/U2), so each field is looked up under the
# spellings the relay is known to use. This is tolerant *lookup* only: a payload
# carrying none of them is REJECTED with REJECT_MALFORMED rather than defaulted.
_PRICE_KEYS: Final[tuple[str, ...]] = ("value", "price")
_TS_KEYS: Final[tuple[str, ...]] = ("timestamp", "ts", "time")
_SYMBOL_KEYS: Final[tuple[str, ...]] = ("symbol", "pair", "asset")
_WINDOW_KEYS: Final[tuple[str, ...]] = ("windowSeconds", "window_seconds")
_FEED_KEYS: Final[tuple[str, ...]] = ("feedId", "feed_id")

# The exact-precision price, preferred over `value` whenever present.
#
# Confirmed against the live relay on 2026-08-05. Every payload carries both:
#
#     "value":               64195.85640491587          <- a bare JSON number
#     "full_accuracy_value": "64195856404915870000000"  <- exact integer TEXT
#
# and full_accuracy_value / 10**18 reproduces value exactly. `value` is a JSON
# number, so it is already bound to a C double by the time any parser sees it, and
# _as_price refuses floats by design — meaning a build that read only `value` would
# reject EVERY live observation and accumulate no signal TWAP at all. Reading the
# integer text instead keeps the whole pipeline exact and never touches a float.
_FULL_ACCURACY_KEYS: Final[tuple[str, ...]] = ("full_accuracy_value", "fullAccuracyValue")

# The fixed-point scale of full_accuracy_value: 18 decimal places, the Chainlink
# convention. Applied as an exact Decimal shift, never as a division by a float.
_FULL_ACCURACY_SCALE: Final[int] = 18

# Above this a numeric timestamp cannot be seconds: 1e11 seconds is the year 5138.
# Used to tell a millisecond stamp from a second stamp, never to correct one.
_MS_THRESHOLD: Final[float] = 1e11

# A timestamp outside this range is not a clock offset, it is a different field
# being read as a timestamp. 2020-01-01 .. 2100-01-01.
_TS_FLOOR: Final[float] = 1_577_836_800.0
_TS_CEILING: Final[float] = 4_102_444_800.0

_ZERO: Final[Decimal] = Decimal(0)
_ONE: Final[Decimal] = Decimal(1)
_HUNDRED: Final[Decimal] = Decimal(100)


def _first_key(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _as_seconds(raw: Any) -> float:
    """Interpret a numeric timestamp as seconds. Raises on anything else.

    Millisecond stamps are divided; that is a unit reading, not a repair — the
    instant it denotes is unchanged. A stamp that lands outside _TS_FLOOR.._TS_CEILING
    after that reading is rejected rather than shifted.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ObservationRejectedError(f"{REJECT_BAD_TIMESTAMP}: {raw!r} is not numeric")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ObservationRejectedError(f"{REJECT_BAD_TIMESTAMP}: {raw!r} is not numeric") from exc
    if value != value or value in (float("inf"), float("-inf")):  # NaN / infinity
        raise ObservationRejectedError(f"{REJECT_BAD_TIMESTAMP}: {raw!r} is not finite")
    if value >= _MS_THRESHOLD:
        value = value / 1000.0
    if not (_TS_FLOOR <= value <= _TS_CEILING):
        raise ObservationRejectedError(
            f"{REJECT_BAD_TIMESTAMP}: {value} is not a plausible epoch second"
        )
    return value


def _as_price(raw: Any) -> Decimal:
    """Convert a payload price to Decimal. Raises on anything unusable.

    float is never accepted as a *source*: a JSON number arrives already rounded
    to binary, and 120000.05 that reads back as 120000.04999999999 shifts the
    cumulative mean. The relay sends prices as strings; a numeric price is
    rejected so that a silent precision change in the relay is visible here
    instead of showing up as an unexplained trigger drift.
    """
    if raw is None:
        raise ObservationRejectedError(f"{REJECT_BAD_PRICE}: price field missing")
    if isinstance(raw, float):
        raise ObservationRejectedError(
            f"{REJECT_BAD_PRICE}: price arrived as a float ({raw!r}); "
            "exact decimal text is required"
        )
    if isinstance(raw, bool) or not isinstance(raw, (int, str, Decimal)):
        raise ObservationRejectedError(f"{REJECT_BAD_PRICE}: {raw!r} is not a price")
    try:
        price = to_decimal(raw)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ObservationRejectedError(f"{REJECT_BAD_PRICE}: {raw!r} is not a number") from exc
    if not price.is_finite():
        raise ObservationRejectedError(f"{REJECT_BAD_PRICE}: {raw!r} is not finite")
    if price <= _ZERO:
        raise ObservationRejectedError(f"{REJECT_BAD_PRICE}: {price} is not positive")
    return price


def _as_full_accuracy_price(raw: Any) -> Decimal:
    """Scale the 18-decimal fixed-point integer to a price. Raises on anything else.

    `scaleb(-18)` is an exact Decimal exponent shift, not a division: no quotient is
    computed and no rounding context applies, so the venue's digits survive intact.

    A float here is refused for the same reason as in `_as_price`. This field exists
    precisely because `value` is lossy, so a float arriving in it means something
    upstream already destroyed the precision the field was read for.
    """
    if isinstance(raw, float):
        raise ObservationRejectedError(
            f"{REJECT_BAD_PRICE}: full-accuracy price arrived as a float ({raw!r}); "
            "exact integer text is required"
        )
    if isinstance(raw, bool) or not isinstance(raw, (int, str, Decimal)):
        raise ObservationRejectedError(f"{REJECT_BAD_PRICE}: {raw!r} is not a price")
    try:
        scaled = to_decimal(raw).scaleb(-_FULL_ACCURACY_SCALE)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ObservationRejectedError(f"{REJECT_BAD_PRICE}: {raw!r} is not a number") from exc
    if not scaled.is_finite():
        raise ObservationRejectedError(f"{REJECT_BAD_PRICE}: {raw!r} is not finite")
    if scaled <= _ZERO:
        raise ObservationRejectedError(f"{REJECT_BAD_PRICE}: {scaled} is not positive")
    # Strip the fixed-point padding. 18 decimal places of trailing zeros carry no
    # information, and leaving them makes every stored and logged price 18 digits
    # wide while comparing unequal to the same value written plainly. `normalize`
    # would render an integral result in exponent form ("6E+4"), so an integral result
    # is re-quantized to exponent zero. Tested via `to_integral_value` rather than the
    # tuple exponent because `as_tuple().exponent` is a string for non-finite values,
    # which makes the comparison unsound even though the guard above rules them out.
    trimmed = scaled.normalize()
    if trimmed == trimmed.to_integral_value():
        trimmed = trimmed.quantize(_ONE)
    return trimmed


def _as_window_seconds(raw: Any) -> int | None:
    """The payload's declared lookback length, or None when absent.

    None is a legitimate answer and is stored as such: a stream that carries no
    windowSeconds is the reference stream, not the TWAP stream (TRAP 2), and that
    has to be visible in the recorded data rather than defaulted to 30.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def parse_payload(payload: object, *, expected_symbol: str) -> Observation:
    """Turn one RTDS message into an Observation, or raise ObservationRejectedError.

    No field is defaulted. A payload missing its price or its timestamp is a
    payload from a stream this build does not understand, and guessing a value for
    it would put an invented number into the signal TWAP.
    """
    if not isinstance(payload, dict):
        raise ObservationRejectedError(
            f"{REJECT_MALFORMED}: expected an object, got {type(payload)}"
        )

    symbol = _first_key(payload, _SYMBOL_KEYS)
    if symbol is None:
        raise ObservationRejectedError(f"{REJECT_MALFORMED}: no symbol field")
    if not isinstance(symbol, str):
        raise ObservationRejectedError(f"{REJECT_MALFORMED}: symbol {symbol!r} is not text")
    if symbol.strip().upper() != expected_symbol.strip().upper():
        raise ObservationRejectedError(f"{REJECT_SYMBOL}: {symbol} is not {expected_symbol}")

    raw_ts = _first_key(payload, _TS_KEYS)
    if raw_ts is None:
        raise ObservationRejectedError(f"{REJECT_MALFORMED}: no timestamp field")

    # The exact field first. The live relay always sends it, and `value` beside it is
    # a JSON number that has already lost digits — so preferring `value` would both
    # degrade precision and, because floats are refused, reject every real payload.
    raw_full = _first_key(payload, _FULL_ACCURACY_KEYS)
    if raw_full is not None:
        price = _as_full_accuracy_price(raw_full)
    else:
        raw_price = _first_key(payload, _PRICE_KEYS)
        if raw_price is None:
            raise ObservationRejectedError(f"{REJECT_MALFORMED}: no price field")
        price = _as_price(raw_price)

    feed_id = _first_key(payload, _FEED_KEYS)

    return Observation(
        ts=_as_seconds(raw_ts),
        price=price,
        feed_id=feed_id if isinstance(feed_id, str) else "",
        window_seconds=_as_window_seconds(_first_key(payload, _WINDOW_KEYS)),
    )


@dataclass(frozen=True, slots=True)
class ValidationLimits:
    """Bounds an observation must satisfy to be folded into the signal TWAP.

    Frozen: these come from validated configuration and must not be relaxed at
    runtime by whatever code happens to be holding the validator.
    """

    max_age_ms: int = 30_000
    max_future_ms: int = 2_000
    max_deviation_percent: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        if self.max_age_ms <= 0:
            raise ValueError(f"max_age_ms must be positive, got {self.max_age_ms}")
        if self.max_future_ms < 0:
            raise ValueError(f"max_future_ms must not be negative, got {self.max_future_ms}")
        deviation = to_decimal(self.max_deviation_percent)
        if deviation <= _ZERO:
            raise ValueError(f"max_deviation_percent must be positive, got {deviation}")
        object.__setattr__(self, "max_deviation_percent", deviation)


class ObservationValidator:
    """Validates observations for ONE feed connection.

    Holds the last ACCEPTED price so a jump can be measured. Instance state, not
    module state: two connections must never compare against each other's last
    price (A11).

    The last accepted price is deliberately not updated by a rejected sample. If a
    rejection moved the reference, one bad print would widen the band around
    itself and admit the next bad print, and a corrupt run of prices would be
    accepted from the second sample onward.
    """

    __slots__ = ("_last_price", "_limits", "accepted", "rejected")

    def __init__(self, limits: ValidationLimits | None = None) -> None:
        self._limits = limits if limits is not None else ValidationLimits()
        self._last_price: Decimal | None = None
        self.accepted = 0
        self.rejected = 0

    @property
    def limits(self) -> ValidationLimits:
        return self._limits

    @property
    def last_accepted_price(self) -> Decimal | None:
        return self._last_price

    def validate(self, observation: Observation, *, received_at: float) -> Observation:
        """Return the observation unchanged, or raise ObservationRejectedError.

        Returns the *same* object. There is no repaired copy to hand back, which
        is the point: a caller cannot accidentally use a corrected version.
        """
        age_ms = (received_at - observation.ts) * 1000.0
        if age_ms > self._limits.max_age_ms:
            self.rejected += 1
            raise ObservationRejectedError(
                f"{REJECT_STALE_SAMPLE}: {age_ms:.0f} ms old "
                f"(limit {self._limits.max_age_ms} ms)"
            )
        if -age_ms > self._limits.max_future_ms:
            self.rejected += 1
            raise ObservationRejectedError(
                f"{REJECT_FUTURE}: {-age_ms:.0f} ms ahead "
                f"(limit {self._limits.max_future_ms} ms)"
            )

        previous = self._last_price
        if previous is not None:
            move = abs(observation.price - previous) / previous * _HUNDRED
            if move > self._limits.max_deviation_percent:
                self.rejected += 1
                raise ObservationRejectedError(
                    f"{REJECT_DEVIATION}: {move:.2f}% from {previous} "
                    f"(limit {self._limits.max_deviation_percent}%)"
                )

        self._last_price = observation.price
        self.accepted += 1
        return observation

    def validate_payload(
        self, payload: object, *, expected_symbol: str, received_at: float
    ) -> Observation:
        """Parse then validate. Both failures raise the same rejection type."""
        try:
            observation = parse_payload(payload, expected_symbol=expected_symbol)
        except ObservationRejectedError:
            self.rejected += 1
            raise
        return self.validate(observation, received_at=received_at)
