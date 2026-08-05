"""Startup step 5: automatic settlement-spec verification.

The invariant under test is A8's: THE PROCESS ALWAYS STARTS. Verification failure
does not exit, does not raise past the caller, and does not degrade the dashboard.
It sets `trading_enabled = False` with a recorded reason, and everything else keeps
running — feeds live, TWAP accumulating, windows opening.

That shape is not a convenience. A process that refused to boot on an unverified
spec would refuse to collect the very data that resolves the spec, so the unknown
could never be closed; a process that booted and traded anyway would be trading a
settlement model nobody has checked.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from conftest import CLOSE_TS

from arc.clock import FrozenClock
from arc.domain.enums import DenialReason, SettlementSpecStatus
from arc.domain.models import Observation
from arc.market.spec_check import (
    U1_WINDOW_PLACEMENT,
    U2_FEED_ID,
    U3_PTB_FORM,
    U4_COMPARISON,
    UNRESOLVED,
    SpecChecker,
)
from arc.runtime.state import RuntimeState
from arc.storage.store import Store


def _payload(feed_id: str = "0xfeed", window: object = 30) -> dict[str, object]:
    body: dict[str, object] = {"symbol": "BTC/USD", "feedId": feed_id}
    if window is not None:
        body["windowSeconds"] = window
    return body


def _verified_checker() -> SpecChecker:
    checker = SpecChecker()
    for _ in range(3):
        checker.offer(_payload())
    return checker


def _runtime(store: Store) -> RuntimeState:
    runtime = RuntimeState(store, FrozenClock(now=1_754_400_000.0))
    runtime.load()
    return runtime


class TestInitialState:
    def test_everything_starts_unresolved(self) -> None:
        result = SpecChecker().result
        assert result.status is SettlementSpecStatus.UNVERIFIED
        assert set(result.unresolved()) == {
            U1_WINDOW_PLACEMENT,
            U2_FEED_ID,
            U3_PTB_FORM,
            U4_COMPARISON,
        }

    def test_an_unverified_checker_is_not_verified(self) -> None:
        assert SpecChecker().result.verified is False


class TestVerification:
    def test_one_sample_is_not_enough(self) -> None:
        """A handful guards against a single odd frame at connect."""
        checker = SpecChecker()
        result = checker.offer(_payload())
        assert result.status is SettlementSpecStatus.UNVERIFIED
        assert "1 of 3" in result.reason

    def test_three_samples_verify_the_stream_identity(self) -> None:
        """That is the one thing checkable without waiting for a market to settle."""
        result = _verified_checker().result
        assert result.status is SettlementSpecStatus.VERIFIED
        assert result.verified is True
        assert result.reason == ""

    def test_the_declared_length_is_recorded_and_the_placement_is_not(self) -> None:
        """U1 has two halves. Recording the confirmed one explicitly keeps the
        remaining unknown from being mistaken for a total unknown."""
        finding = _verified_checker().result.findings[U1_WINDOW_PLACEMENT]
        assert "length=30s confirmed" in finding
        assert UNRESOLVED in finding

    def test_the_feed_id_is_read_from_the_payload_not_assumed(self) -> None:
        """The IDs changed at mainnet launch; whatever the stream reports is how the
        post-mainnet ID gets pinned down (U2/TRAP 2)."""
        checker = SpecChecker()
        checker.offer(_payload(feed_id="0xpost-mainnet"))
        assert checker.result.findings[U2_FEED_ID] == "0xpost-mainnet"

    def test_a_blank_feed_id_leaves_u2_unresolved(self) -> None:
        checker = SpecChecker()
        checker.offer(_payload(feed_id="   "))
        assert checker.result.findings[U2_FEED_ID] == UNRESOLVED

    def test_u3_and_u4_stay_unresolved_after_verification(self) -> None:
        """They need settled markets and are expected to read UNRESOLVED on a first
        run. They do not block, because blocking would prevent the collection that
        resolves them."""
        result = _verified_checker().result
        assert result.verified is True
        assert set(result.unresolved()) == {U3_PTB_FORM, U4_COMPARISON}

    def test_samples_seen_is_reported(self) -> None:
        checker = SpecChecker()
        checker.offer(_payload())
        checker.offer(_payload())
        assert checker.samples_seen == 2


class TestFailure:
    def test_a_reference_stream_fails_verification(self) -> None:
        """TRAP 2: no windowSeconds field is the reference-stream signature."""
        checker = SpecChecker()
        result = checker.offer(_payload(window=None))
        assert result.status is SettlementSpecStatus.FAILED
        assert "reference stream" in result.reason

    def test_a_wrong_window_length_fails_verification(self) -> None:
        result = SpecChecker().offer(_payload(window=60))
        assert result.status is SettlementSpecStatus.FAILED
        assert "expected 30" in result.reason

    def test_a_failing_payload_does_not_count_as_a_sample(self) -> None:
        checker = SpecChecker()
        checker.offer(_payload(window=60))
        assert checker.samples_seen == 0

    def test_offering_never_raises(self) -> None:
        """Step 5 must not take down a process required to keep serving (A8)."""
        checker = SpecChecker()
        payloads: list[object] = [None, [], "text", {}, {"windowSeconds": "x"}]
        for payload in payloads:
            checker.offer(payload)
        assert checker.result.status is SettlementSpecStatus.FAILED


class TestSettledMarketObservations:
    def test_an_exact_tie_is_recorded_for_u4(self) -> None:
        """Distinguishing >= from > needs a market that settled exactly equal. A guess
        from a market that was not equal would look like evidence and would be wrong."""
        checker = _verified_checker()
        price = Decimal("120000.50")
        result = checker.record_settled_market(
            settlement_twap=Observation(ts=float(CLOSE_TS), price=price),
            ptb=Observation(ts=float(CLOSE_TS - 300), price=price),
        )
        assert "exact tie observed" in result.findings[U4_COMPARISON]

    def test_unequal_values_leave_u4_unresolved(self) -> None:
        checker = _verified_checker()
        result = checker.record_settled_market(
            settlement_twap=Observation(ts=float(CLOSE_TS), price=Decimal("120001")),
            ptb=Observation(ts=float(CLOSE_TS - 300), price=Decimal("120000")),
        )
        assert result.findings[U4_COMPARISON] == UNRESOLVED

    def test_a_ptb_declaring_a_thirty_second_window_is_recorded_for_u3(self) -> None:
        checker = _verified_checker()
        result = checker.record_settled_market(
            settlement_twap=Observation(ts=float(CLOSE_TS), price=Decimal("120001")),
            ptb=Observation(ts=float(CLOSE_TS - 300), price=Decimal("120000"), window_seconds=30),
        )
        assert "windowSeconds=30" in result.findings[U3_PTB_FORM]

    def test_a_ptb_declaring_no_window_is_consistent_with_a_snapshot(self) -> None:
        checker = _verified_checker()
        result = checker.record_settled_market(
            settlement_twap=Observation(ts=float(CLOSE_TS), price=Decimal("120001")),
            ptb=Observation(ts=float(CLOSE_TS - 300), price=Decimal("120000")),
        )
        assert "snapshot" in result.findings[U3_PTB_FORM]

    def test_missing_values_record_nothing(self) -> None:
        checker = _verified_checker()
        result = checker.record_settled_market(settlement_twap=None, ptb=None)
        assert result.findings[U3_PTB_FORM] == UNRESOLVED
        assert result.findings[U4_COMPARISON] == UNRESOLVED

    def test_recording_does_not_change_the_verification_status(self) -> None:
        """U3 and U4 are observational; they neither grant nor withdraw permission."""
        checker = _verified_checker()
        result = checker.record_settled_market(settlement_twap=None, ptb=None)
        assert result.status is SettlementSpecStatus.VERIFIED


class TestApplyToRuntime:
    def test_verification_enables_trading(self, store: Store) -> None:
        runtime = _runtime(store)
        _verified_checker().apply(runtime)
        assert runtime.spec_status is SettlementSpecStatus.VERIFIED
        assert runtime.trading_enabled is True
        assert runtime.reason == ""

    def test_an_unverified_spec_disables_trading_with_the_stated_reason(
        self, store: Store
    ) -> None:
        runtime = _runtime(store)
        checker = SpecChecker()
        checker.offer(_payload())  # one sample: short of the target
        checker.apply(runtime)
        assert runtime.trading_enabled is False
        assert runtime.spec_status is SettlementSpecStatus.UNVERIFIED

    def test_a_failed_spec_disables_trading(self, store: Store) -> None:
        runtime = _runtime(store)
        checker = SpecChecker()
        checker.offer(_payload(window=None))
        checker.apply(runtime)
        assert runtime.trading_enabled is False
        assert runtime.spec_status is SettlementSpecStatus.FAILED
        assert "reference stream" in runtime.reason

    def test_apply_never_raises_on_failure(self, store: Store) -> None:
        """A raise in step 5 would take down a process that must keep serving its
        dashboard and keep accumulating its TWAP (A8)."""
        runtime = _runtime(store)
        checker = SpecChecker()
        checker.offer(_payload(window=None))
        assert checker.apply(runtime).status is SettlementSpecStatus.FAILED

    def test_the_disabled_state_is_persisted(self, store: Store) -> None:
        """Criterion 14: the flag and its reason survive a restart."""
        runtime = _runtime(store)
        checker = SpecChecker()
        checker.offer(_payload(window=60))
        checker.apply(runtime)

        restarted = _runtime(store)
        assert restarted.trading_enabled is False
        assert restarted.spec_status is SettlementSpecStatus.FAILED

    def test_the_enabled_state_is_persisted(self, store: Store) -> None:
        runtime = _runtime(store)
        _verified_checker().apply(runtime)

        restarted = _runtime(store)
        assert restarted.trading_enabled is True
        assert restarted.spec_status is SettlementSpecStatus.VERIFIED

    def test_the_failure_is_logged_at_error_level(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        runtime = _runtime(store)
        logger = logging.getLogger("arc.test.spec")
        checker = SpecChecker(logger=logger)
        checker.offer(_payload(window=None))
        with caplog.at_level(logging.ERROR, logger="arc.test.spec"):
            checker.apply(runtime)
        assert "Spec Unverified" in caplog.text

    def test_verification_is_logged_at_info_level(
        self, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        runtime = _runtime(store)
        logger = logging.getLogger("arc.test.spec.ok")
        checker = SpecChecker(logger=logger)
        for _ in range(3):
            checker.offer(_payload())
        with caplog.at_level(logging.INFO, logger="arc.test.spec.ok"):
            checker.apply(runtime)
        assert "Spec Verified" in caplog.text


class TestTradingGateCannotBeBypassed:
    def test_enable_trading_refuses_while_the_spec_is_unverified(self, store: Store) -> None:
        """The refusal lives in RuntimeState, not at the call site: a caller that could
        bypass it makes the whole verification step decorative."""
        runtime = _runtime(store)
        gate = runtime.enable_trading()
        assert gate.enabled is False
        assert gate.blocked is True
        assert gate.reason == DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED.value

    def test_enable_trading_refuses_while_the_spec_has_failed(self, store: Store) -> None:
        runtime = _runtime(store)
        runtime.record_spec_status(SettlementSpecStatus.FAILED, "wrong stream")
        assert runtime.enable_trading().enabled is False

    def test_recording_a_verified_status_alone_does_not_enable_trading(
        self, store: Store
    ) -> None:
        """Recording the status and granting permission are separate steps."""
        runtime = _runtime(store)
        runtime.record_spec_status(SettlementSpecStatus.VERIFIED)
        assert runtime.trading_enabled is False
        assert runtime.enable_trading().enabled is True
