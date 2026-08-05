"""The settlement TWAP stream. OBSERVATIONAL ONLY.

Nothing collected here feeds a decision (A6). The three quantities are never
conflated: `signal_twap` is ARC's own 300s cumulative mean and is the strategy
input; `settlement_twap` is the venue's 30s Chainlink mean recorded here purely for
the record; `ptb` is the immutable official opening reference.

TRAP 2 is the failure this module exists to catch. The feed IDs changed at mainnet
launch, so the stream must be asserted to declare `windowSeconds == 30` — and a
stream carrying NO such field is a reference stream, which is the case that would
otherwise pass silently and get recorded as a settlement mean.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from conftest import CLOSE_TS

from arc.domain.models import Observation
from arc.errors import FeedError
from arc.market.settlement_feed import (
    EXPECTED_WINDOW_SECONDS,
    SettlementTwapCollector,
    SettlementWindowAssertionError,
    assert_settlement_window,
)


def _collector() -> SettlementTwapCollector:
    return SettlementTwapCollector(market_slug="btc-updown-5m-x", close_ts=CLOSE_TS)


def _obs(ts: float, price: str) -> Observation:
    return Observation(ts=ts, price=Decimal(price))


def _payload(ts: float, price: str, window: object = 30) -> dict[str, object]:
    body: dict[str, object] = {"symbol": "BTC/USD", "timestamp": ts, "value": price}
    if window is not None:
        body["windowSeconds"] = window
    return body


class TestWindowAssertion:
    def test_the_expected_window_comes_from_the_timing_module(self) -> None:
        """Not restated, so a change cannot leave two constants disagreeing."""
        from arc.domain.timing import SETTLEMENT_WINDOW_SECONDS

        assert EXPECTED_WINDOW_SECONDS == SETTLEMENT_WINDOW_SECONDS == 30

    def test_a_thirty_second_declaration_is_accepted(self) -> None:
        assert assert_settlement_window({"windowSeconds": 30}) == 30

    def test_the_snake_case_spelling_is_accepted(self) -> None:
        assert assert_settlement_window({"window_seconds": 30}) == 30

    def test_an_absent_field_is_the_reference_stream_signature(self) -> None:
        """TRAP 2. Treating absence as "unknown, probably fine" is exactly how
        reference prices end up recorded as settlement means."""
        with pytest.raises(SettlementWindowAssertionError, match="reference stream"):
            assert_settlement_window({"symbol": "BTC/USD", "value": "1"})

    def test_a_sixty_second_declaration_is_the_wrong_stream(self) -> None:
        with pytest.raises(SettlementWindowAssertionError, match="expected 30"):
            assert_settlement_window({"windowSeconds": 60})

    def test_a_non_numeric_declaration_is_rejected(self) -> None:
        with pytest.raises(SettlementWindowAssertionError, match="not a number"):
            assert_settlement_window({"windowSeconds": "thirty"})

    def test_a_non_object_payload_is_rejected(self) -> None:
        with pytest.raises(SettlementWindowAssertionError, match="not an object"):
            assert_settlement_window(["windowSeconds", 30])

    def test_the_assertion_error_is_operational_not_fatal(self) -> None:
        """The process must still start, still serve its dashboard, still accumulate
        its signal TWAP. What it must not do is trade, and the spec check decides that.
        """
        assert issubclass(SettlementWindowAssertionError, FeedError)
        from arc.errors import ArcError, ArcFatalError

        assert issubclass(SettlementWindowAssertionError, ArcError)
        assert not issubclass(SettlementWindowAssertionError, ArcFatalError)


class TestWindowPlacement:
    def test_the_window_start_is_thirty_seconds_before_close(self) -> None:
        """This is the UNVERIFIED U1 reading (A8) and nothing decides on it."""
        collector = _collector()
        assert collector.window_start == CLOSE_TS - 30

    def test_close_ts_itself_is_inside_the_window(self) -> None:
        assert _collector().in_window(float(CLOSE_TS)) is True

    def test_the_first_instant_is_inside_the_window(self) -> None:
        assert _collector().in_window(float(CLOSE_TS - 30)) is True

    def test_one_second_before_the_window_is_outside(self) -> None:
        assert _collector().in_window(float(CLOSE_TS - 31)) is False

    def test_after_close_is_outside(self) -> None:
        assert _collector().in_window(float(CLOSE_TS + 1)) is False


class TestAccumulation:
    def test_nothing_collected_yields_none_not_zero(self) -> None:
        """A zero would be compared against a real PTB and imply a confident outcome
        from no data at all."""
        assert _collector().settlement_twap is None

    def test_the_mean_is_the_exact_sum_divided_on_read(self) -> None:
        """Hazard H1: never M += (x - M) / n. Sum exactly, divide once."""
        collector = _collector()
        collector.offer(_obs(float(CLOSE_TS - 20), "100"))
        collector.offer(_obs(float(CLOSE_TS - 10), "200"))
        collector.offer(_obs(float(CLOSE_TS - 5), "300"))
        assert collector.running_sum == Decimal("600")
        assert collector.observation_count == 3
        assert collector.settlement_twap == Decimal("200")

    def test_the_stored_state_is_the_sum_and_the_count_not_the_mean(self) -> None:
        collector = _collector()
        collector.offer(_obs(float(CLOSE_TS - 10), "0.1"))
        collector.offer(_obs(float(CLOSE_TS - 5), "0.2"))
        # The exact sum survives; a stored mean would already have rounded once.
        assert collector.running_sum == Decimal("0.3")

    def test_repeated_thirds_do_not_accumulate_rounding(self) -> None:
        """300 samples through the incremental form drift monotonically; the exact sum
        rounds exactly once, at the point of use."""
        collector = _collector()
        for i in range(30):
            collector.offer(_obs(float(CLOSE_TS - 30 + i), "1"))
        assert collector.settlement_twap == Decimal("1")
        assert collector.running_sum == Decimal("30")

    def test_an_out_of_window_observation_is_dropped_not_clamped(self) -> None:
        """A clamped sample would shift the recorded mean and make the U1 comparison —
        the entire reason this data exists — answer the wrong question."""
        collector = _collector()
        assert collector.offer(_obs(float(CLOSE_TS - 100), "999999")) is False
        assert collector.observation_count == 0
        assert collector.settlement_twap is None

    def test_dropping_does_not_count_as_a_rejection(self) -> None:
        """Out of window is not malformed; only a parse or assertion failure is."""
        collector = _collector()
        collector.offer(_obs(float(CLOSE_TS - 100), "1"))
        assert collector.rejected_count == 0


class TestPayloadPath:
    def test_a_valid_payload_is_asserted_parsed_and_folded_in(self) -> None:
        collector = _collector()
        accepted = collector.offer_payload(
            _payload(float(CLOSE_TS - 10), "120000.50"), expected_symbol="BTC/USD"
        )
        assert accepted is True
        assert collector.window_asserted is True
        assert collector.settlement_twap == Decimal("120000.50")

    def test_the_window_is_asserted_on_the_first_payload_only(self) -> None:
        """It is a property of the stream, not of a message. Re-asserting per message
        would turn one malformed frame into a stream-level failure."""
        collector = _collector()
        collector.offer_payload(
            _payload(float(CLOSE_TS - 20), "100"), expected_symbol="BTC/USD"
        )
        # No windowSeconds on the second frame, and it is still accepted.
        accepted = collector.offer_payload(
            _payload(float(CLOSE_TS - 10), "200", window=None), expected_symbol="BTC/USD"
        )
        assert accepted is True
        assert collector.observation_count == 2

    def test_a_reference_stream_is_rejected_on_the_first_payload(self) -> None:
        collector = _collector()
        accepted = collector.offer_payload(
            _payload(float(CLOSE_TS - 10), "100", window=None), expected_symbol="BTC/USD"
        )
        assert accepted is False
        assert collector.window_asserted is False
        assert collector.rejected_count == 1
        assert collector.observation_count == 0

    def test_a_sixty_second_stream_is_rejected(self) -> None:
        collector = _collector()
        assert (
            collector.offer_payload(
                _payload(float(CLOSE_TS - 10), "100", window=60), expected_symbol="BTC/USD"
            )
            is False
        )
        assert collector.rejected_count == 1

    def test_a_malformed_price_counts_as_a_rejection(self) -> None:
        collector = _collector()
        payload = _payload(float(CLOSE_TS - 10), "100")
        payload["value"] = None
        assert collector.offer_payload(payload, expected_symbol="BTC/USD") is False
        assert collector.rejected_count == 1

    def test_a_float_price_is_refused(self) -> None:
        """A JSON number arrives already binary-rounded; exact decimal text is required."""
        collector = _collector()
        payload = _payload(float(CLOSE_TS - 10), "100")
        payload["value"] = 120000.05
        assert collector.offer_payload(payload, expected_symbol="BTC/USD") is False
        assert collector.rejected_count == 1

    def test_the_wrong_symbol_counts_as_a_rejection(self) -> None:
        collector = _collector()
        payload = _payload(float(CLOSE_TS - 10), "100")
        payload["symbol"] = "ETH/USD"
        assert collector.offer_payload(payload, expected_symbol="BTC/USD") is False
        assert collector.rejected_count == 1

    def test_the_window_assertion_is_logged_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        collector = _collector()
        logger = logging.getLogger("arc.test.settlement")
        with caplog.at_level(logging.INFO, logger="arc.test.settlement"):
            collector.offer_payload(
                _payload(float(CLOSE_TS - 20), "100"),
                expected_symbol="BTC/USD",
                logger=logger,
            )
            collector.offer_payload(
                _payload(float(CLOSE_TS - 10), "200"),
                expected_symbol="BTC/USD",
                logger=logger,
            )
        assert caplog.text.count("Settlement Window") == 1

    def test_a_rejection_is_logged_with_its_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        collector = _collector()
        logger = logging.getLogger("arc.test.settlement.reject")
        with caplog.at_level(logging.WARNING, logger="arc.test.settlement.reject"):
            collector.offer_payload(
                _payload(float(CLOSE_TS - 10), "100", window=None),
                expected_symbol="BTC/USD",
                logger=logger,
            )
        details = [getattr(r, "arc_detail", "") for r in caplog.records]
        assert any("reference stream" in d for d in details)


class TestPerMarketIsolation:
    def test_two_collectors_do_not_share_an_accumulator(self) -> None:
        """A11: created fresh per market, dropped at close, no reset path."""
        first = SettlementTwapCollector(market_slug="a", close_ts=CLOSE_TS)
        second = SettlementTwapCollector(market_slug="b", close_ts=CLOSE_TS)
        first.offer(_obs(float(CLOSE_TS - 10), "100"))
        assert second.observation_count == 0
        assert second.settlement_twap is None

    def test_there_is_no_reset_method(self) -> None:
        """"TWAP resets per market" is satisfied by construction, not by a reset path
        that can be forgotten in one of the places that needed it."""
        collector = _collector()
        assert not hasattr(collector, "reset")
        assert not hasattr(collector, "clear")

    def test_adjacent_markets_use_their_own_window_bounds(self) -> None:
        first = SettlementTwapCollector(market_slug="a", close_ts=CLOSE_TS)
        second = SettlementTwapCollector(market_slug="b", close_ts=CLOSE_TS + 300)
        assert first.window_start == CLOSE_TS - 30
        assert second.window_start == CLOSE_TS + 270
