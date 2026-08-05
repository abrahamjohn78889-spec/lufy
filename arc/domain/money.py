"""Decimal money handling.

Two rules govern this whole module:

  1. Floats never enter. to_decimal() REFUSES a float rather than converting it,
     because Decimal(0.1) is 0.1000000000000000055511151231257827021181583404541015625
     and a value that wrong in the seventh decimal place will eventually cross a
     tick boundary the operator never authorised.

  2. Rounding is always ROUND_FLOOR, never nearest. Nearest rounding on a price can
     move it UP, and a price rounded up past ENTRY_PRICE_MAX after validation has
     passed is defect D2.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Final

__all__ = [
    "dec_str",
    "notional",
    "quantize_floor",
    "quantize_price",
    "quantize_size",
    "shares_for_notional",
    "to_decimal",
]

_ZERO: Final[Decimal] = Decimal("0")


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Convert to Decimal. Floats are REFUSED, not converted.

    Accepting a float here would silently admit binary representation error into
    the TWAP sum and the price ladder. The caller must pass a string or a Decimal,
    which forces the imprecision to be resolved at the boundary where the value
    was parsed rather than deep inside the trigger arithmetic.
    """
    if isinstance(value, bool):
        # bool is a subclass of int; True would become Decimal("1") and read as a
        # price of one dollar. Refused explicitly rather than caught by the int arm.
        raise TypeError("to_decimal() refuses bool")
    if isinstance(value, float):
        raise TypeError(
            "to_decimal() refuses float; pass a str or Decimal so binary "
            "representation error is resolved at the parse boundary"
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"not a valid decimal: {value!r}") from exc
    else:
        raise TypeError(f"to_decimal() refuses {type(value).__name__}")

    if not result.is_finite():
        # NaN and Infinity compare in ways that make every downstream guard
        # useless: NaN >= trigger is False and NaN <= trigger is also False, so a
        # NaN TWAP would make both directions structurally unfireable.
        raise ValueError(f"non-finite decimal: {result}")
    return result


def quantize_floor(value: Decimal | int | str, step: Decimal | int | str) -> Decimal:
    """Floor `value` to a multiple of `step`, toward negative infinity."""
    dec_value = to_decimal(value)
    dec_step = to_decimal(step)
    if dec_step <= _ZERO:
        raise ValueError(f"quantization step must be positive, got {dec_step}")

    steps = (dec_value / dec_step).to_integral_value(rounding=ROUND_FLOOR)
    result = steps * dec_step
    # Re-quantize to the step's own exponent so 0.7 * 0.01 does not surface as
    # 0.7000 in a log line or as a different TEXT value in the database than the
    # same price written by another path.
    return result.quantize(dec_step.normalize() if dec_step == dec_step.normalize() else dec_step)


def quantize_price(value: Decimal | int | str, tick_size: Decimal | int | str) -> Decimal:
    """Floor a price to the tick ladder.

    Applied BEFORE entry validation, never after. Validating and then rounding is
    what lets a 0.8549 price pass an 0.85 cap and then get written as 0.86 with
    every check having reported success (defect D2).
    """
    return quantize_floor(value, tick_size)


def quantize_size(value: Decimal | int | str, step: Decimal | int | str = 1) -> Decimal:
    """Floor an order size to a whole multiple of the venue's size step.

    Floored rather than rounded so the resulting notional can never exceed the
    configured position budget.
    """
    return quantize_floor(value, step)


def shares_for_notional(
    budget: Decimal | int | str,
    price: Decimal | int | str,
    size_step: Decimal | int | str = 1,
) -> Decimal:
    """Largest quantized share count whose cost does not exceed `budget`.

    Floors the division, so shares * price <= budget holds at every price in the
    band. Rounding to nearest would overspend the budget by up to half a share's
    cost on roughly half of all prices — small per trade, unbounded over a day.
    """
    dec_budget = to_decimal(budget)
    dec_price = to_decimal(price)
    if dec_price <= _ZERO:
        raise ValueError(f"price must be positive, got {dec_price}")
    if dec_budget < _ZERO:
        raise ValueError(f"budget must not be negative, got {dec_budget}")
    return quantize_size(dec_budget / dec_price, size_step)


def notional(shares: Decimal | int | str, price: Decimal | int | str) -> Decimal:
    """Cost of `shares` at `price`. Exact, unrounded."""
    return to_decimal(shares) * to_decimal(price)


def dec_str(value: Decimal | int | str) -> str:
    """Render a Decimal as plain text, never scientific notation.

    Decimal("0.00001") formats as "1E-5" under str(), and a price written to the
    database in that form does not compare equal as TEXT to the same price written
    as "0.00001". Every money value stored or displayed goes through here.
    """
    dec = to_decimal(value)
    formatted = format(dec, "f")
    # format(Decimal("-0"), "f") yields "-0"; normalise so a zero balance never
    # renders with a negative sign.
    if formatted.startswith("-") and to_decimal(formatted) == _ZERO:
        formatted = formatted[1:]
    return formatted
