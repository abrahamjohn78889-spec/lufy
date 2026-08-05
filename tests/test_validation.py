"""Observation validation: REJECT, never repair.

The property under test throughout is that no value is ever invented. A repaired
observation is indistinguishable from a real one once it has been folded into the
cumulative mean, so a clamped price would move the locked trigger by a real amount
with nothing in the record showing a number had been made up.

The payload shapes here are the LIVE ones, captured from the relay on 2026-08-05.
That matters more than it sounds: the live frame carries its price twice, once as a
lossy JSON float and once as exact integer text, and a parser that read the obvious
field would reject every real observation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import WINDOW_TS

from arc.domain.models import Observation
from arc.errors import ObservationRejectedError
from arc.market.validation import (
    REJECT_BAD_PRICE,
    REJECT_BAD_TIMESTAMP,
    REJECT_DEVIATION,
    REJECT_FUTURE,
    REJECT_MALFORMED,
    REJECT_STALE_SAMPLE,
    REJECT_SYMBOL,
    ObservationValidator,
    ValidationLimits,
    parse_payload,
)

# A verbatim live frame payload. Both price fields present, exactly as sent.
LIVE_PAYLOAD: dict[str, object] = {
    "full_accuracy_value": "64195856404915870000000",
    "symbol": "btc/usd",
    "timestamp": 1785913500,
    "value": 64195.85640491587,
}


def _payload(**overrides: object) -> dict[str, object]:
    body = dict(LIVE_PAYLOAD)
    body.update(overrides)
    return body


def _parse(**overrides: object) -> Observation:
    return parse_payload(_payload(**overrides), expected_symbol="btc/usd")


class TestTheLiveFrame:
    def test_a_verbatim_live_frame_parses(self) -> None:
        """The whole point. Captured from wss://ws-live-data.polymarket.com,
        topic crypto_prices_chainlink, type update."""
        observation = parse_payload(LIVE_PAYLOAD, expected_symbol="btc/usd")
        assert observation.price == Decimal("64195.85640491587")
        assert observation.ts == 1785913500

    def test_the_exact_field_is_preferred_over_the_float_field(self) -> None:
        """Both are present in every live frame. `value` is a bare JSON number and is
        therefore already a C double; reading it would lose digits AND — since floats
        are refused — reject every single live observation, accumulating no TWAP at
        all while the feed looked healthy."""
        observation = parse_payload(LIVE_PAYLOAD, expected_symbol="btc/usd")
        # Had `value` been read, the float would have been refused outright.
        assert observation.price == Decimal("64195.85640491587")

    def test_the_float_field_alone_is_still_refused(self) -> None:
        """Removing the exact field must not silently fall back to lossy input."""
        payload = _payload()
        del payload["full_accuracy_value"]
        with pytest.raises(ObservationRejectedError, match=REJECT_BAD_PRICE):
            parse_payload(payload, expected_symbol="btc/usd")

    def test_a_string_value_is_accepted_when_the_exact_field_is_absent(self) -> None:
        payload = _payload(value="64195.85640491587")
        del payload["full_accuracy_value"]
        assert parse_payload(payload, expected_symbol="btc/usd").price == Decimal(
            "64195.85640491587"
        )

    def test_the_live_frame_carries_no_window_seconds(self) -> None:
        """TRAP 2's live reading: the stream declares no lookback length, which is
        recorded as None rather than defaulted to 30."""
        assert parse_payload(LIVE_PAYLOAD, expected_symbol="btc/usd").window_seconds is None


class TestFullAccuracyScaling:
    def test_the_scale_is_eighteen_decimal_places(self) -> None:
        assert _parse(full_accuracy_value="1000000000000000000").price == Decimal(1)

    def test_the_scaled_value_reproduces_the_float_field_exactly(self) -> None:
        """The two fields agree, which is what makes the exact one a safe substitute
        rather than a different quantity."""
        exact = Decimal("64195856404915870000000").scaleb(-18)
        assert exact == Decimal(str(LIVE_PAYLOAD["value"]))

    def test_fixed_point_padding_is_stripped(self) -> None:
        """Otherwise every stored price is 18 digits wide and compares unequal to the
        same value written plainly."""
        assert str(_parse(full_accuracy_value="64000000000000000000000").price) == "64000"

    def test_an_integral_price_is_not_rendered_in_exponent_form(self) -> None:
        """`Decimal.normalize()` alone yields "6.4E+4", which is the same number but
        not a price any log reader recognises."""
        text = str(_parse(full_accuracy_value="64000000000000000000000").price)
        assert "E" not in text and "e" not in text

    def test_the_conversion_is_an_exponent_shift_not_a_division(self) -> None:
        """A division would apply the rounding context and silently truncate at 28
        significant digits; the shift is exact for any input length."""
        digits = "1" + "0" * 17 + "123456789012345678"
        assert _parse(full_accuracy_value=digits).price == Decimal(digits).scaleb(-18)

    def test_a_float_in_the_exact_field_is_refused(self) -> None:
        """The field exists because `value` is lossy. A float arriving in it means
        something upstream already destroyed the precision it was read for."""
        with pytest.raises(ObservationRejectedError, match="float"):
            _parse(full_accuracy_value=6.4e22)

    def test_a_zero_price_is_refused(self) -> None:
        with pytest.raises(ObservationRejectedError, match="not positive"):
            _parse(full_accuracy_value="0")

    def test_a_negative_price_is_refused(self) -> None:
        with pytest.raises(ObservationRejectedError, match="not positive"):
            _parse(full_accuracy_value="-1000000000000000000")

    def test_non_numeric_text_is_refused(self) -> None:
        with pytest.raises(ObservationRejectedError, match="not a number"):
            _parse(full_accuracy_value="sixty-four thousand")

    def test_a_bool_is_not_a_price(self) -> None:
        with pytest.raises(ObservationRejectedError, match="not a price"):
            _parse(full_accuracy_value=True)

    def test_the_camel_case_spelling_is_accepted(self) -> None:
        payload = _payload(fullAccuracyValue="1000000000000000000")
        del payload["full_accuracy_value"]
        assert parse_payload(payload, expected_symbol="btc/usd").price == Decimal(1)


class TestSymbol:
    def test_a_mismatched_symbol_is_rejected(self) -> None:
        with pytest.raises(ObservationRejectedError, match=REJECT_SYMBOL):
            _parse(symbol="eth/usd")

    def test_the_comparison_ignores_case_and_padding(self) -> None:
        """The relay sends "btc/usd"; the settlement path asks for "BTC/USD". Both
        name the same instrument and neither is a repair of a price."""
        assert _parse(symbol="  BTC/USD  ") is not None

    def test_a_missing_symbol_is_malformed(self) -> None:
        payload = _payload()
        del payload["symbol"]
        with pytest.raises(ObservationRejectedError, match=REJECT_MALFORMED):
            parse_payload(payload, expected_symbol="btc/usd")

    def test_a_non_text_symbol_is_malformed(self) -> None:
        with pytest.raises(ObservationRejectedError, match="not text"):
            _parse(symbol=123)


class TestTimestamp:
    def test_a_millisecond_stamp_is_read_as_milliseconds(self) -> None:
        """A unit reading, not a repair: the instant denoted is unchanged."""
        assert _parse(timestamp=1785913500000).ts == 1785913500.0

    def test_a_missing_timestamp_is_malformed(self) -> None:
        payload = _payload()
        del payload["timestamp"]
        with pytest.raises(ObservationRejectedError, match=REJECT_MALFORMED):
            parse_payload(payload, expected_symbol="btc/usd")

    def test_an_implausible_epoch_is_rejected_not_shifted(self) -> None:
        with pytest.raises(ObservationRejectedError, match=REJECT_BAD_TIMESTAMP):
            _parse(timestamp=1)

    def test_a_non_numeric_timestamp_is_rejected(self) -> None:
        with pytest.raises(ObservationRejectedError, match=REJECT_BAD_TIMESTAMP):
            _parse(timestamp="yesterday")

    def test_a_bool_is_not_a_timestamp(self) -> None:
        with pytest.raises(ObservationRejectedError, match=REJECT_BAD_TIMESTAMP):
            _parse(timestamp=True)


class TestMalformedPayloads:
    def test_a_non_object_payload_is_rejected(self) -> None:
        with pytest.raises(ObservationRejectedError, match=REJECT_MALFORMED):
            parse_payload(["not", "an", "object"], expected_symbol="btc/usd")

    def test_a_payload_with_no_price_at_all_is_rejected(self) -> None:
        payload = _payload()
        del payload["full_accuracy_value"]
        del payload["value"]
        with pytest.raises(ObservationRejectedError, match="no price field"):
            parse_payload(payload, expected_symbol="btc/usd")

    def test_nothing_is_defaulted(self) -> None:
        """An empty object yields a rejection, never a zero-valued observation."""
        with pytest.raises(ObservationRejectedError):
            parse_payload({}, expected_symbol="btc/usd")


class TestValidatorLimits:
    def test_a_stale_sample_is_rejected(self) -> None:
        validator = ObservationValidator(ValidationLimits(max_age_ms=1_000))
        observation = parse_payload(LIVE_PAYLOAD, expected_symbol="btc/usd")
        with pytest.raises(ObservationRejectedError, match=REJECT_STALE_SAMPLE):
            validator.validate(observation, received_at=observation.ts + 5.0)

    def test_a_sample_from_the_future_is_rejected(self) -> None:
        validator = ObservationValidator(ValidationLimits(max_future_ms=100))
        observation = parse_payload(LIVE_PAYLOAD, expected_symbol="btc/usd")
        with pytest.raises(ObservationRejectedError, match=REJECT_FUTURE):
            validator.validate(observation, received_at=observation.ts - 5.0)

    def test_an_implausible_jump_is_rejected(self) -> None:
        validator = ObservationValidator(ValidationLimits(max_deviation_percent=Decimal(1)))
        first = parse_payload(LIVE_PAYLOAD, expected_symbol="btc/usd")
        validator.validate(first, received_at=first.ts)
        jumped = parse_payload(
            _payload(full_accuracy_value="90000000000000000000000"),
            expected_symbol="btc/usd",
        )
        with pytest.raises(ObservationRejectedError, match=REJECT_DEVIATION):
            validator.validate(jumped, received_at=jumped.ts)

    def test_a_rejected_sample_does_not_move_the_reference(self) -> None:
        """If a rejection moved the reference, one bad print would widen the band
        around itself and admit the next one, and a corrupt run would be accepted
        from the second sample onward."""
        validator = ObservationValidator(ValidationLimits(max_deviation_percent=Decimal(1)))
        first = parse_payload(LIVE_PAYLOAD, expected_symbol="btc/usd")
        validator.validate(first, received_at=first.ts)
        jumped = parse_payload(
            _payload(full_accuracy_value="90000000000000000000000"),
            expected_symbol="btc/usd",
        )
        with pytest.raises(ObservationRejectedError):
            validator.validate(jumped, received_at=jumped.ts)
        assert validator.last_accepted_price == first.price

    def test_the_same_object_is_returned_not_a_repaired_copy(self) -> None:
        """A caller cannot accidentally use a corrected version, because none exists."""
        validator = ObservationValidator()
        observation = parse_payload(LIVE_PAYLOAD, expected_symbol="btc/usd")
        assert validator.validate(observation, received_at=observation.ts) is observation

    def test_counters_track_both_outcomes(self) -> None:
        validator = ObservationValidator(ValidationLimits(max_age_ms=1_000))
        observation = parse_payload(LIVE_PAYLOAD, expected_symbol="btc/usd")
        validator.validate(observation, received_at=observation.ts)
        with pytest.raises(ObservationRejectedError):
            validator.validate(observation, received_at=observation.ts + 60.0)
        assert (validator.accepted, validator.rejected) == (1, 1)

    def test_a_parse_failure_counts_as_a_rejection_on_the_payload_path(self) -> None:
        validator = ObservationValidator()
        with pytest.raises(ObservationRejectedError):
            validator.validate_payload(
                {}, expected_symbol="btc/usd", received_at=float(WINDOW_TS)
            )
        assert validator.rejected == 1

    def test_two_validators_do_not_share_a_reference_price(self) -> None:
        """A11: instance state. Two connections comparing against each other's last
        price would reject on the other's history."""
        first = ObservationValidator()
        second = ObservationValidator()
        observation = parse_payload(LIVE_PAYLOAD, expected_symbol="btc/usd")
        first.validate(observation, received_at=observation.ts)
        assert second.last_accepted_price is None


class TestLimitsValidation:
    def test_a_non_positive_max_age_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_age_ms"):
            ValidationLimits(max_age_ms=0)

    def test_a_negative_future_allowance_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_future_ms"):
            ValidationLimits(max_future_ms=-1)

    def test_a_non_positive_deviation_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_deviation_percent"):
            ValidationLimits(max_deviation_percent=Decimal(0))

    def test_the_deviation_limit_is_coerced_to_decimal(self) -> None:
        assert ValidationLimits(max_deviation_percent=Decimal("2.5")).max_deviation_percent == (
            Decimal("2.5")
        )
