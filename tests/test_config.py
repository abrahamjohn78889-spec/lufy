"""Configuration: the 13 fatal invariants, the 3 advisories, and layer precedence.

Every invariant in build_trading_config is fatal, and the tests below assert the
exception type as well as the message. The type is the contract: ConfigInvariantError
subclasses ArcFatalError, which is what makes the process exit rather than log and
carry on. A test asserting only "it raised something" would still pass on the day
one of these was demoted to a warning — and a demoted config check means a bot that
boots looking healthy while trading a configuration nobody chose.

Each invariant describes a configuration that runs without erroring. That is why
they are checked at startup rather than discovered later: a window with no buffer
never fires, an inverted entry band admits no price, and neither reports anything
anywhere.

ArcSettings is constructed with _env_file=None throughout. The repository has a
real .env, and letting it leak in would make these tests pass or fail depending on
a file that is not under test.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from pydantic import ValidationError

from arc.config import (
    ArcSettings,
    TradingConfig,
    build_trading_config,
    env_trading_values,
    load_settings,
)
from arc.domain.enums import Mode
from arc.errors import ArcError, ArcFatalError, BindAddressError, ConfigInvariantError

_CREDENTIALS = {
    "polymarket_api_key": "key-aaaaaaaaaaaa",
    "polymarket_api_secret": "secret-bbbbbbbbbb",
    "polymarket_api_passphrase": "pass-cccccccccc",
    "polymarket_private_key": "0xdddddddddddddddd",
}


def _env(**overrides: object) -> ArcSettings:
    """ArcSettings isolated from the repository's own .env.

    _env_file is a pydantic-settings runtime keyword that its generated __init__
    signature does not declare, hence the ignore.
    """
    return ArcSettings(_env_file=None, **overrides)  # type: ignore[call-arg, arg-type]


def _seed_env(**overrides: object) -> ArcSettings:
    """An ArcSettings carrying a complete, valid set of trading values.

    ArcSettings' own defaults are deliberately unusable — max_trades_per_market
    defaults to 0 and the decimal fields to "" — because there are no defaults for
    trading parameters. So a first-run seeding test has to supply all of them.
    """
    values: dict[str, object] = {
        "execution_windows": "15,10,7,5,3",
        "buffers": "15:2.00,10:2.00,7:1.50,5:1.25,3:1.00",
        "position_notional_usd": "25.00",
        "max_trades_per_market": 3,
        "max_concurrent_positions": 3,
        "max_daily_loss_usd": "50.00",
        "max_consecutive_losses": 5,
        "entry_price_min": "0.55",
        "entry_price_max": "0.85",
        "tick_size": "0.01",
        "min_tradable_size": "5",
        "cancel_ack_timeout_ms": 400,
        "clock_drift_critical_ms": 900,
    }
    return _env(**(values | overrides))


def _expect_fatal(values: dict[str, str], match: str) -> ConfigInvariantError:
    with pytest.raises(ConfigInvariantError, match=match) as caught:
        build_trading_config(values)
    return caught.value


class TestConfigErrorsAreFatalNotAdvisory:
    """The taxonomy itself. If this inverts, every other test here still passes."""

    def test_config_invariant_error_is_fatal(self) -> None:
        assert issubclass(ConfigInvariantError, ArcFatalError)
        assert not issubclass(ConfigInvariantError, ArcError)

    def test_bind_address_error_is_fatal(self) -> None:
        assert issubclass(BindAddressError, ArcFatalError)

    def test_the_valid_baseline_actually_passes(self, trading_values: dict[str, str]) -> None:
        """Anchors every negative test below: the only change is the one being tested."""
        config = build_trading_config(trading_values)
        assert config.execution_windows == (3, 5, 7, 10, 15)
        assert config.warnings == ()


class TestRequiredValuesHaveNoDefaults:
    """A missing trading value is an error, never a substituted guess.

    An invented number that reaches the trading path is indistinguishable,
    afterwards, from one the operator chose.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "execution_windows",
            "buffers",
            "position_notional_usd",
            "max_trades_per_market",
            "max_concurrent_positions",
            "max_daily_loss_usd",
            "max_consecutive_losses",
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
            "observation_retention_days",
        ],
    )
    def test_missing_key_is_fatal(self, trading_values: dict[str, str], key: str) -> None:
        del trading_values[key]
        with pytest.raises(ConfigInvariantError):
            build_trading_config(trading_values)

    @pytest.mark.parametrize(
        "key",
        ["execution_windows", "buffers", "position_notional_usd", "tick_size", "cancel_lead_ms"],
    )
    def test_empty_string_is_fatal_too(self, trading_values: dict[str, str], key: str) -> None:
        """An empty value is missing, not zero."""
        trading_values[key] = "   "
        with pytest.raises(ConfigInvariantError):
            build_trading_config(trading_values)

    def test_the_no_default_reason_is_stated_to_the_operator(
        self, trading_values: dict[str, str]
    ) -> None:
        trading_values["position_notional_usd"] = ""
        error = _expect_fatal(trading_values, "no default")
        assert "never reach the trading path" in str(error)


class TestParsing:
    def test_non_integer_window_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["execution_windows"] = "15,ten,3"
        _expect_fatal(trading_values, "non-integer offset")

    def test_malformed_buffer_entry_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["buffers"] = "15:2.00,10"
        _expect_fatal(trading_values, "malformed")

    def test_non_integer_buffer_offset_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["buffers"] = "fifteen:2.00"
        _expect_fatal(trading_values, "non-integer offset")

    def test_invalid_buffer_value_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["buffers"] = "15:abc"
        _expect_fatal(trading_values, "invalid buffer value")

    def test_float_buffer_value_is_still_accepted_as_text(
        self, trading_values: dict[str, str]
    ) -> None:
        """Values arrive as strings, so Decimal('2.00') is exact — no float anywhere."""
        trading_values["buffers"] = "3:1.00,5:1.25,7:1.50,10:2.00,15:2.05"
        config = build_trading_config(trading_values)
        assert config.buffer_for(15) == Decimal("2.05")

    def test_duplicate_buffer_offset_is_fatal(self, trading_values: dict[str, str]) -> None:
        """The later value would silently win over the earlier one."""
        trading_values["buffers"] = "3:1.00,5:1.25,7:1.50,10:2.00,15:2.00,15:9.99"
        _expect_fatal(trading_values, "more than once")

    def test_whitespace_around_values_is_tolerated(self, trading_values: dict[str, str]) -> None:
        trading_values["execution_windows"] = " 15 , 10 , 7 , 5 , 3 "
        trading_values["buffers"] = " 15 : 2.00 , 10:2.00, 7:1.50 ,5:1.25,3:1.00 "
        assert build_trading_config(trading_values).execution_windows == (3, 5, 7, 10, 15)

    def test_non_integer_int_field_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["cancel_lead_ms"] = "500ms"
        _expect_fatal(trading_values, "not an integer")

    def test_invalid_decimal_field_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["tick_size"] = "one cent"
        _expect_fatal(trading_values, "not a valid decimal")

    @pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
    def test_truthy_bool_spellings(self, trading_values: dict[str, str], raw: str) -> None:
        trading_values["allow_opposing_directions"] = raw
        assert build_trading_config(trading_values).allow_opposing_directions is True

    @pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "off", ""])
    def test_falsy_bool_spellings(self, trading_values: dict[str, str], raw: str) -> None:
        trading_values["allow_opposing_directions"] = raw
        assert build_trading_config(trading_values).allow_opposing_directions is False

    def test_unrecognised_bool_is_fatal_not_falsy(self, trading_values: dict[str, str]) -> None:
        """'maybe' must not quietly mean False; the operator wrote something."""
        trading_values["allow_opposing_directions"] = "maybe"
        _expect_fatal(trading_values, "must be true or false")


class TestInvariant1DuplicateWindows:
    """A duplicate collapses into one window in the offset-keyed dict."""

    def test_duplicate_offset_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["execution_windows"] = "15,10,10,7,5,3"
        error = _expect_fatal(trading_values, "duplicate offsets")
        assert "[10]" in str(error)

    def test_the_operator_would_have_seen_a_missing_window(
        self, trading_values: dict[str, str]
    ) -> None:
        error = _expect_fatal(
            {**trading_values, "execution_windows": "15,15,10,7,5,3"}, "duplicate"
        )
        assert "collapses" in str(error)


class TestInvariant2WindowBounds:
    """The offset must be positive and strictly inside the 300s market."""

    @pytest.mark.parametrize("offset", ["0", "-3"])
    def test_non_positive_offset_is_fatal(
        self, trading_values: dict[str, str], offset: str
    ) -> None:
        trading_values["execution_windows"] = offset
        trading_values["buffers"] = f"{offset}:1.00"
        _expect_fatal(trading_values, "must be positive")

    @pytest.mark.parametrize("offset", ["300", "301", "600"])
    def test_offset_at_or_beyond_market_duration_is_fatal(
        self, trading_values: dict[str, str], offset: str
    ) -> None:
        """At 300s the window would activate before the market opens."""
        trading_values["execution_windows"] = offset
        trading_values["buffers"] = f"{offset}:1.00"
        _expect_fatal(trading_values, "not shorter than")

    def test_299_seconds_is_permitted(self, trading_values: dict[str, str]) -> None:
        """The boundary is exclusive, and 299 is inside the market."""
        trading_values["execution_windows"] = "299"
        trading_values["buffers"] = "299:1.00"
        assert build_trading_config(trading_values).execution_windows == (299,)


class TestInvariant3MissingBuffer:
    """A window without a buffer has no trigger and can never fire."""

    def test_window_without_buffer_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["buffers"] = "15:2.00,10:2.00,7:1.50,5:1.25"  # 3s omitted
        error = _expect_fatal(trading_values, "no buffer configured")
        assert "[3]" in str(error)
        assert "never fire" in str(error)


class TestInvariant4OrphanBuffer:
    """A buffer for a disabled window means the operator believes it is active."""

    def test_orphan_buffer_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["buffers"] = "15:2.00,10:2.00,7:1.50,5:1.25,3:1.00,20:3.00"
        error = _expect_fatal(trading_values, "not in EXECUTION_WINDOWS")
        assert "[20]" in str(error)


class TestInvariant5BufferMustBePositive:
    """A zero buffer sets the trigger to the opening TWAP: an unconditional trade."""

    @pytest.mark.parametrize("buffer_value", ["0", "0.00", "-1.00"])
    def test_non_positive_buffer_is_fatal(
        self, trading_values: dict[str, str], buffer_value: str
    ) -> None:
        trading_values["buffers"] = f"15:2.00,10:2.00,7:1.50,5:1.25,3:{buffer_value}"
        error = _expect_fatal(trading_values, "must be positive")
        assert "immediately on every market" in str(error)

    def test_a_tiny_positive_buffer_is_permitted(self, trading_values: dict[str, str]) -> None:
        """Aggressive is the operator's call; unconditional is not a strategy."""
        trading_values["buffers"] = "15:2.00,10:2.00,7:1.50,5:1.25,3:0.01"
        assert build_trading_config(trading_values).buffer_for(3) == Decimal("0.01")


class TestInvariant6EntryBand:
    """Inverted or out-of-range bounds admit no price at all."""

    def test_inverted_band_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["entry_price_min"] = "0.85"
        trading_values["entry_price_max"] = "0.55"
        error = _expect_fatal(trading_values, "must be below")
        assert "admits no price at all" in str(error)

    def test_equal_bounds_are_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["entry_price_min"] = "0.70"
        trading_values["entry_price_max"] = "0.70"
        _expect_fatal(trading_values, "must be below")

    @pytest.mark.parametrize(("low", "high"), [("0", "0.85"), ("-0.10", "0.85")])
    def test_min_at_or_below_zero_is_fatal(
        self, trading_values: dict[str, str], low: str, high: str
    ) -> None:
        trading_values["entry_price_min"] = low
        trading_values["entry_price_max"] = high
        _expect_fatal(trading_values, "strictly inside 0 and 1")

    @pytest.mark.parametrize("high", ["1", "1.00", "1.5"])
    def test_max_at_or_above_one_is_fatal(
        self, trading_values: dict[str, str], high: str
    ) -> None:
        """Share prices are probabilities; 1.00 is a certainty nobody sells."""
        trading_values["entry_price_max"] = high
        _expect_fatal(trading_values, "strictly inside 0 and 1")

    def test_prices_just_inside_the_bounds_are_permitted(
        self, trading_values: dict[str, str]
    ) -> None:
        trading_values["entry_price_min"] = "0.01"
        trading_values["entry_price_max"] = "0.99"
        config = build_trading_config(trading_values)
        assert (config.entry_price_min, config.entry_price_max) == (
            Decimal("0.01"),
            Decimal("0.99"),
        )


class TestInvariant7BandNarrowerThanATick:
    """Prices are floored to the tick ladder BEFORE validation (defect D2).

    So a band thinner than one tick can contain no valid quantized price even
    though both bounds look perfectly reasonable on the Settings page.
    """

    def test_band_narrower_than_tick_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["entry_price_min"] = "0.845"
        trading_values["entry_price_max"] = "0.850"
        trading_values["tick_size"] = "0.01"
        error = _expect_fatal(trading_values, "narrower than TICK_SIZE")
        assert "no quantized price could ever fall inside it" in str(error)

    def test_band_exactly_one_tick_wide_is_permitted(
        self, trading_values: dict[str, str]
    ) -> None:
        trading_values["entry_price_min"] = "0.84"
        trading_values["entry_price_max"] = "0.85"
        trading_values["tick_size"] = "0.01"
        assert build_trading_config(trading_values).tick_size == Decimal("0.01")

    @pytest.mark.parametrize("tick", ["0", "-0.01"])
    def test_non_positive_tick_is_fatal(self, trading_values: dict[str, str], tick: str) -> None:
        trading_values["tick_size"] = tick
        _expect_fatal(trading_values, "TICK_SIZE must be positive")

    @pytest.mark.parametrize("size", ["0", "-5"])
    def test_non_positive_min_tradable_size_is_fatal(
        self, trading_values: dict[str, str], size: str
    ) -> None:
        trading_values["min_tradable_size"] = size
        _expect_fatal(trading_values, "MIN_TRADABLE_SIZE must be positive")


class TestInvariant8BudgetBuysTheExchangeMinimum:
    """Checked at the WORST allowed price, not the best.

    Without this the config passes every check and then every order at the top of
    the band is rejected by the venue for being under the minimum size — a failure
    that only appears once real money is moving.
    """

    def test_budget_too_small_at_the_top_of_the_band_is_fatal(
        self, trading_values: dict[str, str]
    ) -> None:
        trading_values["position_notional_usd"] = "4.00"
        trading_values["entry_price_max"] = "0.85"
        trading_values["min_tradable_size"] = "5"
        error = _expect_fatal(trading_values, "below the exchange minimum")
        assert "top of the band would be rejected" in str(error)

    def test_a_budget_sufficient_only_at_the_bottom_is_still_fatal(
        self, trading_values: dict[str, str]
    ) -> None:
        """4.00 buys 7 shares at 0.55 but only 4.7 at 0.85. The cap is what matters."""
        trading_values["position_notional_usd"] = "4.00"
        assert Decimal("4.00") / Decimal("0.55") > Decimal("5")
        _expect_fatal(trading_values, "below the exchange minimum")

    def test_exactly_the_minimum_is_permitted(self, trading_values: dict[str, str]) -> None:
        trading_values["position_notional_usd"] = "4.25"  # 4.25 / 0.85 == 5 exactly
        trading_values["entry_price_max"] = "0.85"
        trading_values["min_tradable_size"] = "5"
        assert build_trading_config(trading_values).position_notional_usd == Decimal("4.25")

    @pytest.mark.parametrize("notional", ["0", "-25.00"])
    def test_non_positive_notional_is_fatal(
        self, trading_values: dict[str, str], notional: str
    ) -> None:
        trading_values["position_notional_usd"] = notional
        _expect_fatal(trading_values, "POSITION_NOTIONAL_USD must be positive")

    @pytest.mark.parametrize("trades", ["0", "-1"])
    def test_max_trades_below_one_is_fatal(
        self, trading_values: dict[str, str], trades: str
    ) -> None:
        """Zero permitted trades is a bot configured never to trade."""
        trading_values["max_trades_per_market"] = trades
        _expect_fatal(trading_values, "at least 1")


class TestInvariant9And10Cancellation:
    """The sweep must exist, and no window may open inside it."""

    @pytest.mark.parametrize("lead", ["0", "-500"])
    def test_non_positive_cancel_lead_is_fatal(
        self, trading_values: dict[str, str], lead: str
    ) -> None:
        error = _expect_fatal(
            {**trading_values, "cancel_lead_ms": lead}, "CANCEL_LEAD_MS must be positive"
        )
        assert "ride into settlement" in str(error)

    @pytest.mark.parametrize("timeout", ["0", "-1"])
    def test_non_positive_ack_timeout_is_fatal(
        self, trading_values: dict[str, str], timeout: str
    ) -> None:
        trading_values["cancel_ack_timeout_ms"] = timeout
        _expect_fatal(trading_values, "CANCEL_ACK_TIMEOUT_MS must be positive")

    def test_earliest_window_inside_the_sweep_is_fatal(
        self, trading_values: dict[str, str]
    ) -> None:
        """That window would open in phase CANCELLING and be denied every time."""
        trading_values["cancel_lead_ms"] = "4000"  # 4s sweep, 3s window
        error = _expect_fatal(trading_values, "inside the cancellation sweep")
        assert "could never submit an order" in str(error)
        assert "Lower CANCEL_LEAD_MS" in str(error)

    def test_window_exactly_at_the_sweep_boundary_is_fatal(
        self, trading_values: dict[str, str]
    ) -> None:
        """3s window, 3000ms lead: the window opens exactly as the sweep begins."""
        trading_values["cancel_lead_ms"] = "3000"
        _expect_fatal(trading_values, "inside the cancellation sweep")

    def test_window_just_outside_the_sweep_is_permitted(
        self, trading_values: dict[str, str]
    ) -> None:
        trading_values["cancel_lead_ms"] = "2999"
        assert build_trading_config(trading_values).cancel_lead_ms == 2999

    def test_only_the_earliest_window_is_checked(self, trading_values: dict[str, str]) -> None:
        """Later windows are trivially outside a sweep the earliest one clears."""
        trading_values["execution_windows"] = "15,10"
        trading_values["buffers"] = "15:2.00,10:2.00"
        trading_values["cancel_lead_ms"] = "9000"
        assert build_trading_config(trading_values).cancel_lead_ms == 9000


class TestInvariant11And12InvertedThresholds:
    """warn >= critical removes the early notice entirely."""

    def test_inverted_feed_thresholds_are_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["feed_stale_warn_ms"] = "10000"
        trading_values["feed_stale_critical_ms"] = "3000"
        error = _expect_fatal(trading_values, "FEED_STALE_WARN_MS")
        assert "never precedes the fault" in str(error)

    def test_equal_feed_thresholds_are_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["feed_stale_warn_ms"] = "3000"
        trading_values["feed_stale_critical_ms"] = "3000"
        _expect_fatal(trading_values, "must be below")

    @pytest.mark.parametrize(
        ("warn", "critical"), [("0", "10000"), ("3000", "0"), ("-1", "10000")]
    )
    def test_non_positive_feed_thresholds_are_fatal(
        self, trading_values: dict[str, str], warn: str, critical: str
    ) -> None:
        trading_values["feed_stale_warn_ms"] = warn
        trading_values["feed_stale_critical_ms"] = critical
        _expect_fatal(trading_values, "feed staleness thresholds must be positive")

    def test_inverted_drift_thresholds_are_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["clock_drift_warn_ms"] = "900"
        trading_values["clock_drift_critical_ms"] = "250"
        _expect_fatal(trading_values, "CLOCK_DRIFT_WARN_MS")

    def test_equal_drift_thresholds_are_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["clock_drift_warn_ms"] = "250"
        trading_values["clock_drift_critical_ms"] = "250"
        _expect_fatal(trading_values, "must be below")

    @pytest.mark.parametrize(("warn", "critical"), [("0", "900"), ("250", "0")])
    def test_non_positive_drift_thresholds_are_fatal(
        self, trading_values: dict[str, str], warn: str, critical: str
    ) -> None:
        trading_values["clock_drift_warn_ms"] = warn
        trading_values["clock_drift_critical_ms"] = critical
        _expect_fatal(trading_values, "clock drift thresholds must be positive")


class TestInvariant13BurstBelowSustained:
    """A token bucket smaller than its refill rate throttles its own steady state."""

    def test_burst_below_sustained_is_fatal(self, trading_values: dict[str, str]) -> None:
        trading_values["outbound_rate_sustained"] = "16"
        trading_values["outbound_rate_burst"] = "8"
        error = _expect_fatal(trading_values, "must be at least")
        assert "throttles the steady state" in str(error)

    def test_burst_equal_to_sustained_is_permitted(self, trading_values: dict[str, str]) -> None:
        trading_values["outbound_rate_sustained"] = "8"
        trading_values["outbound_rate_burst"] = "8"
        assert build_trading_config(trading_values).outbound_rate_burst == 8

    @pytest.mark.parametrize(("sustained", "burst"), [("0", "16"), ("8", "0"), ("-1", "16")])
    def test_non_positive_rates_are_fatal(
        self, trading_values: dict[str, str], sustained: str, burst: str
    ) -> None:
        trading_values["outbound_rate_sustained"] = sustained
        trading_values["outbound_rate_burst"] = burst
        _expect_fatal(trading_values, "outbound rate limits must be positive")

    @pytest.mark.parametrize("days", ["0", "-30"])
    def test_non_positive_retention_is_fatal(
        self, trading_values: dict[str, str], days: str
    ) -> None:
        trading_values["observation_retention_days"] = days
        _expect_fatal(trading_values, "OBSERVATION_RETENTION_DAYS must be positive")


class TestAdvisoryWarningsDoNotBlockStartup:
    """Three conditions warn and boot. They are the operator's call, not errors.

    The distinction is deliberate: an invariant describes a configuration that
    cannot do what it says, while a warning describes one that can, at a cost the
    operator is entitled to accept.
    """

    def test_opposing_directions_warns_with_the_arithmetic(
        self, trading_values: dict[str, str]
    ) -> None:
        """Hazard H3: holding both sides is a guaranteed loss, not a hedge."""
        trading_values["allow_opposing_directions"] = "true"
        config = build_trading_config(trading_values)
        assert config.allow_opposing_directions is True
        assert len(config.warnings) == 1
        warning = config.warnings[0]
        assert "guaranteed loss" in warning
        # The concrete numbers matter: an abstract warning gets dismissed.
        assert "1.01" in warning and "1.00" in warning

    def test_large_drift_critical_warns_about_the_three_second_window(
        self, trading_values: dict[str, str]
    ) -> None:
        trading_values["clock_drift_critical_ms"] = "1000"
        config = build_trading_config(trading_values)
        assert any("3-second window" in w for w in config.warnings)

    def test_drift_critical_just_below_the_threshold_is_silent(
        self, trading_values: dict[str, str]
    ) -> None:
        trading_values["clock_drift_critical_ms"] = "999"
        assert build_trading_config(trading_values).warnings == ()

    def test_ack_timeout_at_or_above_lead_warns_about_indeterminate(
        self, trading_values: dict[str, str]
    ) -> None:
        """A cancel unacknowledged at close leaves the order INDETERMINATE (A13)."""
        trading_values["cancel_ack_timeout_ms"] = "500"
        trading_values["cancel_lead_ms"] = "500"
        config = build_trading_config(trading_values)
        assert any("INDETERMINATE" in w for w in config.warnings)

    def test_ack_timeout_below_lead_is_silent(self, trading_values: dict[str, str]) -> None:
        trading_values["cancel_ack_timeout_ms"] = "499"
        trading_values["cancel_lead_ms"] = "500"
        assert build_trading_config(trading_values).warnings == ()

    def test_all_three_warnings_accumulate(self, trading_values: dict[str, str]) -> None:
        trading_values["allow_opposing_directions"] = "true"
        trading_values["clock_drift_critical_ms"] = "1000"
        trading_values["cancel_ack_timeout_ms"] = "500"
        assert len(build_trading_config(trading_values).warnings) == 3


class TestTradingConfigIsFrozen:
    """A mutable config can become invalid while the engine holds it."""

    def test_assignment_is_refused(self, trading_values: dict[str, str]) -> None:
        config = build_trading_config(trading_values)
        with pytest.raises(FrozenInstanceError):
            config.position_notional_usd = Decimal("1000")  # type: ignore[misc]

    def test_the_settings_page_pattern_builds_a_new_instance(
        self, trading_values: dict[str, str]
    ) -> None:
        """An invalid edit must leave the previous config active, not half-applied."""
        original = build_trading_config(trading_values)
        bad = original.as_storage_dict() | {"buffers": "3:0,5:1.25,7:1.50,10:2.00,15:2.00"}
        with pytest.raises(ConfigInvariantError):
            build_trading_config(bad)
        assert original.buffer_for(3) == Decimal("1.00")


class TestDerivedValues:
    def test_windows_by_priority_is_ascending(self, trading_values: dict[str, str]) -> None:
        """Ascending offset: the 3s window is the best-informed, so it is first."""
        trading_values["execution_windows"] = "7,15,3,10,5"
        assert build_trading_config(trading_values).windows_by_priority == (3, 5, 7, 10, 15)

    def test_implied_btc_move_scales_with_window_length(
        self, trading_values: dict[str, str]
    ) -> None:
        """Identical buffers mean very different BTC moves per window (A16)."""
        trading_values["buffers"] = "15:2.00,10:2.00,7:2.00,5:2.00,3:2.00"
        config = build_trading_config(trading_values)
        assert config.implied_btc_move(10) == Decimal("60.00")
        assert config.implied_btc_move(15) == Decimal("40.00")
        # Same 2.00 buffer, 5x the required move on the 3s window.
        assert config.implied_btc_move(3) == Decimal("200.00")

    def test_implied_move_is_exact_decimal_not_float(
        self, trading_values: dict[str, str]
    ) -> None:
        trading_values["buffers"] = "15:2.00,10:2.00,7:1.50,5:1.25,3:1.00"
        move = build_trading_config(trading_values).implied_btc_move(7)
        assert isinstance(move, Decimal)
        assert move == Decimal("1.50") * (Decimal(300) / Decimal(7))

    def test_buffer_for_unknown_window_raises(self, trading_values: dict[str, str]) -> None:
        with pytest.raises(KeyError):
            build_trading_config(trading_values).buffer_for(999)


class TestStorageRoundTrip:
    """as_storage_dict feeds the settings table, which feeds build_trading_config."""

    def test_round_trip_is_identical(self, trading_values: dict[str, str]) -> None:
        original = build_trading_config(trading_values)
        assert build_trading_config(original.as_storage_dict()) == original

    def test_every_stored_value_is_text(self, trading_values: dict[str, str]) -> None:
        stored = build_trading_config(trading_values).as_storage_dict()
        assert all(isinstance(v, str) for v in stored.values())

    def test_decimals_are_stored_without_scientific_notation(
        self, trading_values: dict[str, str]
    ) -> None:
        trading_values["min_tradable_size"] = "0.00001"
        stored = build_trading_config(trading_values).as_storage_dict()
        assert stored["min_tradable_size"] == "0.00001"

    def test_round_trip_preserves_the_advisory_warnings(
        self, trading_values: dict[str, str]
    ) -> None:
        trading_values["allow_opposing_directions"] = "true"
        stored = build_trading_config(trading_values).as_storage_dict()
        assert stored["allow_opposing_directions"] == "true"
        assert build_trading_config(stored).warnings


class TestBindAddressIsTheAccessControl:
    """There is no authentication anywhere in this codebase (A3).

    The loopback bind IS the access control, which is why a non-loopback address is
    refused rather than warned about: binding 0.0.0.0 puts an unauthenticated
    start/stop/settings surface for a live trading bot on the public internet.
    """

    @pytest.mark.parametrize("address", ["127.0.0.1", "127.0.0.5", "::1", " 127.0.0.1 "])
    def test_loopback_addresses_are_accepted(self, address: str) -> None:
        assert _env(api_bind=address).api_bind == address.strip()

    @pytest.mark.parametrize(
        "address", ["0.0.0.0", "192.168.1.10", "10.0.0.1", "8.8.8.8", "::"]
    )
    def test_non_loopback_addresses_are_refused(self, address: str) -> None:
        with pytest.raises(BindAddressError, match="not a loopback"):
            _env(api_bind=address)

    def test_a_hostname_is_refused_rather_than_resolved(self) -> None:
        """Resolving 'localhost' would make the check depend on /etc/hosts."""
        with pytest.raises(BindAddressError, match="must be a loopback IP"):
            _env(api_bind="localhost")

    def test_refusal_names_the_ssh_tunnel_alternative(self) -> None:
        """An operator who wants remote access needs the supported route, not a no."""
        with pytest.raises(BindAddressError, match="ssh -L"):
            _env(api_bind="0.0.0.0")


class TestModeHasNoThirdOption:
    def test_v1_and_v2_are_accepted(self) -> None:
        assert _env(mode="V1").mode is Mode.V1
        assert _env(mode="V2").mode is Mode.V2

    def test_mode_is_case_insensitive_and_stripped(self) -> None:
        assert _env(mode=" v2 ").mode is Mode.V2

    @pytest.mark.parametrize("value", ["TESTNET", "PAPER", "DRY_RUN", "SIM", "V3", ""])
    def test_unknown_modes_are_rejected_identically(self, value: str) -> None:
        """TESTNET is not a special case here — it is a typo, like V3 (A3)."""
        with pytest.raises(ValidationError, match="MODE must be one of"):
            _env(mode=value)

    def test_the_error_lists_the_only_valid_values(self) -> None:
        with pytest.raises(ValidationError, match=r"\['V1', 'V2'\]"):
            _env(mode="TESTNET")


class TestSecretsNeverAppearInText:
    """SecretStr is what keeps a key out of a traceback pasted into a support chat."""

    def test_repr_and_str_do_not_contain_the_value(self) -> None:
        env = _env(**_CREDENTIALS)
        rendered = f"{env!r} {env} {env.polymarket_api_key!r} {env.polymarket_api_key}"
        for secret in _CREDENTIALS.values():
            assert secret not in rendered

    def test_get_secret_value_returns_the_real_string(self) -> None:
        env = _env(**_CREDENTIALS)
        assert env.polymarket_api_key.get_secret_value() == _CREDENTIALS["polymarket_api_key"]

    def test_a_validation_error_does_not_leak_a_secret(self) -> None:
        """The most likely leak path: a traceback from an unrelated bad field."""
        with pytest.raises((ValidationError, BindAddressError)) as caught:
            _env(api_bind="0.0.0.0", **_CREDENTIALS)
        for secret in _CREDENTIALS.values():
            assert secret not in str(caught.value)

    def test_redacted_dump_reports_set_or_unset_never_a_value(self) -> None:
        dump = _env(**_CREDENTIALS).redacted_dump()
        assert dump["polymarket_api_key"] == "SET"
        rendered = " ".join(dump.values())
        for secret in _CREDENTIALS.values():
            assert secret not in rendered

    def test_redacted_dump_reports_unset_for_empty_secrets(self) -> None:
        dump = _env().redacted_dump()
        assert all(dump[f] == "UNSET" for f in dump if f.startswith("polymarket_"))

    def test_redacted_dump_covers_every_field(self) -> None:
        """A field added later must not silently escape the dump."""
        env = _env()
        assert set(env.redacted_dump()) == set(type(env).model_fields)

    def test_secret_values_feeds_the_redaction_filter(self) -> None:
        values = _env(**_CREDENTIALS).secret_values()
        assert set(values) == set(_CREDENTIALS.values())

    def test_secret_values_omits_empty_secrets(self) -> None:
        """An empty string in the filter would match every line and redact all of it."""
        assert _env().secret_values() == ()
        partial = _env(polymarket_api_key="key-aaaaaaaaaaaa")
        assert partial.secret_values() == ("key-aaaaaaaaaaaa",)

    def test_has_credentials_requires_all_four(self) -> None:
        assert _env(**_CREDENTIALS).has_credentials() is True
        for omitted in _CREDENTIALS:
            partial = {k: v for k, v in _CREDENTIALS.items() if k != omitted}
            assert _env(**partial).has_credentials() is False, omitted


class TestLiveModeRequiresCredentials:
    """A live-mode bot with no keys looks armed and rejects every submission."""

    def test_v2_without_credentials_is_fatal(self, trading_values: dict[str, str]) -> None:
        with pytest.raises(ConfigInvariantError, match="requires credentials"):
            load_settings(_env(mode="V2"), trading_values)

    def test_the_error_names_the_missing_fields(self, trading_values: dict[str, str]) -> None:
        env = _env(mode="V2", polymarket_api_key="key-aaaaaaaaaaaa")
        with pytest.raises(ConfigInvariantError) as caught:
            load_settings(env, trading_values)
        message = str(caught.value)
        assert "POLYMARKET_API_SECRET" in message
        assert "POLYMARKET_API_KEY" not in message

    def test_v2_with_credentials_loads(self, trading_values: dict[str, str]) -> None:
        settings = load_settings(_env(mode="V2", **_CREDENTIALS), trading_values)
        assert settings.mode is Mode.V2

    def test_v1_without_credentials_loads(self, trading_values: dict[str, str]) -> None:
        """V1 does not submit orders, so it needs no keys."""
        assert load_settings(_env(mode="V1"), trading_values).mode is Mode.V1

    def test_a_bad_trading_value_is_reported_before_the_credential_check(
        self, trading_values: dict[str, str]
    ) -> None:
        """Otherwise the operator fixes credentials first and then hits this anyway."""
        trading_values["buffers"] = "3:0,5:1.25,7:1.50,10:2.00,15:2.00"
        with pytest.raises(ConfigInvariantError, match="buffer for window"):
            load_settings(_env(mode="V2"), trading_values)


class TestEnvSeedsOnceThenSqliteWins:
    """.env seeds on first run; after that the Settings page is the source of truth.

    An operator who lowered a buffer in the UI must not have it silently reverted
    by a stale .env on the next restart — and the reverted value would be a real
    trading parameter, applied without anyone being told.
    """

    def test_empty_store_seeds_from_env(self) -> None:
        env = _seed_env(execution_windows="10,5", buffers="10:2.00,5:1.25")
        settings = load_settings(env, stored=None)
        assert settings.seeded_from_env is True
        assert settings.trading.execution_windows == (5, 10)

    def test_populated_store_wins_over_env(self, trading_values: dict[str, str]) -> None:
        env = _seed_env(
            execution_windows="10,5",
            buffers="10:9.99,5:9.99",
            position_notional_usd="999.00",
            max_trades_per_market=99,
        )
        settings = load_settings(env, stored=trading_values)
        assert settings.seeded_from_env is False
        assert settings.trading.execution_windows == (3, 5, 7, 10, 15)
        assert settings.trading.buffer_for(10) == Decimal("2.00")
        assert settings.trading.position_notional_usd == Decimal("25.00")

    def test_an_empty_dict_counts_as_first_run(self) -> None:
        """{} and None must behave identically: neither is a saved configuration."""
        assert load_settings(_seed_env(), stored={}).seeded_from_env is True

    def test_stored_values_are_not_merged_with_env(self, trading_values: dict[str, str]) -> None:
        """A partial store must fail, not be quietly completed from .env.

        Silently filling a gap from .env is how a value the operator deleted in the
        UI comes back without anyone deciding it should.
        """
        del trading_values["buffers"]
        with pytest.raises(ConfigInvariantError, match="BUFFERS is empty"):
            load_settings(_env(buffers="15:2.00,10:2.00,7:1.50,5:1.25,3:1.00"), trading_values)

    def test_non_trading_settings_always_come_from_env(
        self, trading_values: dict[str, str]
    ) -> None:
        """db_path and api_port are deployment facts, not tunable trading values."""
        env = _env(db_path="custom/arc.db", log_dir="custom/logs", api_port=9999)
        settings = load_settings(env, trading_values)
        assert settings.db_path.as_posix() == "custom/arc.db"
        assert settings.log_dir.as_posix() == "custom/logs"
        assert settings.env.api_port == 9999

    def test_env_trading_values_covers_exactly_the_trading_keys(self) -> None:
        from arc.config import TRADING_KEYS

        assert set(env_trading_values(_env())) == set(TRADING_KEYS)

    def test_env_trading_values_are_all_strings(self) -> None:
        """build_trading_config parses text, so ints must already be rendered."""
        values = env_trading_values(_env(max_trades_per_market=3))
        assert all(isinstance(v, str) for v in values.values())
        assert values["max_trades_per_market"] == "3"


class TestSettingsFacade:
    def test_warnings_pass_through_from_trading(self, trading_values: dict[str, str]) -> None:
        trading_values["allow_opposing_directions"] = "true"
        settings = load_settings(_env(), trading_values)
        assert settings.warnings == settings.trading.warnings
        assert settings.warnings

    def test_settings_is_frozen(self, trading_values: dict[str, str]) -> None:
        settings = load_settings(_env(), trading_values)
        with pytest.raises(FrozenInstanceError):
            settings.seeded_from_env = True  # type: ignore[misc]

    def test_redacted_dump_delegates_to_env(self, trading_values: dict[str, str]) -> None:
        settings = load_settings(_env(**_CREDENTIALS), trading_values)
        assert settings.redacted_dump()["polymarket_api_key"] == "SET"

    def test_trading_config_type_is_exported(self) -> None:
        assert isinstance(TradingConfig, type)
