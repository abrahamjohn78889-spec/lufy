"""Money: flooring, float refusal, non-finite refusal, budget-safe sizing."""

from __future__ import annotations

from decimal import Decimal

import pytest

from arc.domain.money import (
    dec_str,
    notional,
    quantize_floor,
    quantize_price,
    quantize_size,
    shares_for_notional,
    to_decimal,
)


class TestFlooringTable:
    @pytest.mark.parametrize(
        ("value", "tick", "expected"),
        [
            # The acceptance criterion: both sides of the midpoint floor DOWN.
            ("0.7449", "0.01", "0.74"),
            ("0.7451", "0.01", "0.74"),
            ("0.7499", "0.01", "0.74"),
            ("0.74", "0.01", "0.74"),
            ("0.7500", "0.01", "0.75"),
            ("0.8599", "0.01", "0.85"),
            ("0.001", "0.01", "0.00"),
            ("0.999", "0.01", "0.99"),
            ("0.12345", "0.001", "0.123"),
            ("0.5", "0.25", "0.50"),
        ],
    )
    def test_price_floors_never_rounds_up(self, value: str, tick: str, expected: str) -> None:
        assert quantize_price(value, tick) == Decimal(expected)

    def test_acceptance_criterion_6_exactly(self) -> None:
        assert quantize_price("0.7449", "0.01") == quantize_price("0.7451", "0.01")
        assert quantize_price("0.7449", "0.01") == Decimal("0.74")

    def test_flooring_never_pushes_a_price_over_a_cap(self) -> None:
        """Defect D2: quantize BEFORE validate, and quantizing must only lower."""
        cap = Decimal("0.85")
        for thousandths in range(800, 860):
            raw = Decimal(thousandths) / Decimal(1000)
            floored = quantize_price(raw, "0.01")
            assert floored <= raw
            if raw <= cap:
                assert floored <= cap

    def test_size_floors_to_whole_shares(self) -> None:
        assert quantize_size("33.99") == Decimal("33")
        assert quantize_size("33.00") == Decimal("33")

    def test_zero_or_negative_step_refused(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            quantize_floor("1.0", "0")
        with pytest.raises(ValueError, match="positive"):
            quantize_floor("1.0", "-0.01")


class TestFloatRefusal:
    def test_to_decimal_refuses_float(self) -> None:
        """Refuses, does not convert: Decimal(0.1) is wrong in the 17th place."""
        # The ignores below are the point of these tests: the signature excludes
        # float statically, and this asserts the runtime guard rejects it too. A
        # caller reaching this from untyped JSON gets no help from the annotation.
        with pytest.raises(TypeError, match="refuses float"):
            to_decimal(0.1)  # type: ignore[arg-type]

    def test_to_decimal_refuses_bool(self) -> None:
        # bool subclasses int; True would silently become a price of 1.00.
        with pytest.raises(TypeError, match="refuses bool"):
            to_decimal(True)

    def test_quantize_price_refuses_float(self) -> None:
        with pytest.raises(TypeError, match="refuses float"):
            quantize_price(0.7449, "0.01")  # type: ignore[arg-type]

    def test_quantize_price_refuses_float_tick(self) -> None:
        with pytest.raises(TypeError, match="refuses float"):
            quantize_price("0.7449", 0.01)  # type: ignore[arg-type]

    def test_accepts_str_int_decimal(self) -> None:
        assert to_decimal("1.5") == Decimal("1.5")
        assert to_decimal(2) == Decimal("2")
        assert to_decimal(Decimal("3.25")) == Decimal("3.25")

    def test_refuses_unparseable_string(self) -> None:
        with pytest.raises(ValueError, match="not a valid decimal"):
            to_decimal("not-a-number")

    def test_refuses_other_types(self) -> None:
        with pytest.raises(TypeError):
            to_decimal(None)  # type: ignore[arg-type]


class TestNonFiniteRefusal:
    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "sNaN"])
    def test_refuses_non_finite(self, value: str) -> None:
        """NaN >= x and NaN <= x are BOTH false: a NaN TWAP disables both directions."""
        with pytest.raises(ValueError, match="non-finite"):
            to_decimal(value)

    def test_refuses_non_finite_decimal_instance(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            to_decimal(Decimal("NaN"))


class TestSizingNeverExceedsBudget:
    def test_across_the_whole_price_band(self) -> None:
        """shares * price <= budget at EVERY tick in the band, with no exceptions."""
        budget = Decimal("25.00")
        for cents in range(1, 100):
            price = Decimal(cents) / Decimal(100)
            shares = shares_for_notional(budget, price)
            assert notional(shares, price) <= budget, f"overspent at price {price}"

    def test_across_many_budgets_and_prices(self) -> None:
        for budget_cents in (137, 500, 2500, 10000, 99999):
            budget = Decimal(budget_cents) / Decimal(100)
            for cents in range(1, 100):
                price = Decimal(cents) / Decimal(100)
                shares = shares_for_notional(budget, price)
                assert notional(shares, price) <= budget

    def test_shares_are_whole_by_default(self) -> None:
        shares = shares_for_notional("25.00", "0.74")
        assert shares == shares.to_integral_value()
        assert shares == Decimal("33")

    def test_rejects_non_positive_price(self) -> None:
        with pytest.raises(ValueError, match="price must be positive"):
            shares_for_notional("25.00", "0")

    def test_rejects_negative_budget(self) -> None:
        with pytest.raises(ValueError, match="budget must not be negative"):
            shares_for_notional("-1", "0.5")

    def test_zero_budget_buys_nothing(self) -> None:
        assert shares_for_notional("0", "0.5") == Decimal("0")


class TestDecStr:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0.00001", "0.00001"),
            ("1E-5", "0.00001"),
            ("1E+5", "100000"),
            ("120000.00", "120000.00"),
            ("0", "0"),
            ("-0", "0"),
            ("-12.5", "-12.5"),
        ],
    )
    def test_never_scientific_notation(self, value: str, expected: str) -> None:
        assert dec_str(value) == expected

    def test_no_sci_notation_across_wide_exponent_range(self) -> None:
        for exponent in range(-12, 13):
            rendered = dec_str(Decimal(1).scaleb(exponent))
            assert "E" not in rendered.upper(), rendered

    def test_round_trips_through_text(self) -> None:
        """Storage writes TEXT; the value must survive the round trip exactly."""
        for value in ("120000.00", "0.7449", "1E-7", "33", "0.000000001"):
            assert to_decimal(dec_str(value)) == to_decimal(value)
