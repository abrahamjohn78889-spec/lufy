"""Automatic settlement-spec verification. Startup step 5.

Four things about the venue's settlement are UNDOCUMENTED (A8), and every one of
them changes what a correct bot does:

    U1  does the settlement window sit [close_ts-30, close_ts] or straddle close?
    U2  what is the exact feed ID of the 30-second BTC/USD TWAP stream?
    U3  is PTB still a snapshot at window_ts, or is it now itself a 30s TWAP?
    U4  does the settled comparison use >= or >?

None can be answered from documentation, so this module answers what it can from
the live stream and records the rest as unresolved.

THE PROCESS ALWAYS STARTS. Verification failure does not exit, does not raise past
the caller, and does not degrade the dashboard. It sets `trading_enabled = False`
with reason TRADING_DISABLED_SPEC_UNVERIFIED, and everything else keeps running:
feeds live, TWAP accumulating, windows opening, decisions evaluated and RECORDED but
never submitted. Enforcement is at the order-submission boundary inside the Risk
Engine, not here.

That shape is the point. A process that refused to boot on an unverified spec would
also refuse to collect the very data that resolves the spec, so the unknown could
never be closed. A process that booted and traded anyway would be trading a
settlement model nobody has checked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Final

from arc.domain.enums import SettlementSpecStatus
from arc.domain.models import Observation
from arc.domain.timing import SETTLEMENT_WINDOW_SECONDS
from arc.logging_setup import log_event
from arc.market.settlement_feed import (
    SettlementWindowAssertionError,
    assert_settlement_window,
)
from arc.runtime.state import RuntimeState

__all__ = [
    "U1_WINDOW_PLACEMENT",
    "U2_FEED_ID",
    "U3_PTB_FORM",
    "U4_COMPARISON",
    "UNRESOLVED",
    "SpecCheckResult",
    "SpecChecker",
]

U1_WINDOW_PLACEMENT: Final[str] = "U1_SETTLEMENT_WINDOW_PLACEMENT"
U2_FEED_ID: Final[str] = "U2_TWAP_FEED_ID"
U3_PTB_FORM: Final[str] = "U3_PTB_FORM"
U4_COMPARISON: Final[str] = "U4_SETTLED_COMPARISON"

UNRESOLVED: Final[str] = "UNRESOLVED"

# How many payloads to inspect before deciding. One is enough to assert the window
# and read the feed ID; a handful guards against a single odd frame at connect.
_SAMPLE_TARGET: Final[int] = 3


@dataclass(slots=True)
class SpecCheckResult:
    """What verification established, and what it could not.

    `status` is VERIFIED only when the stream identity is confirmed — that is the
    one thing that can be checked without waiting for a market to settle. U1, U3 and
    U4 need settled markets to compare against and are expected to read UNRESOLVED
    on a first run; they do not block, because blocking on them would prevent the
    observational collection that resolves them.
    """

    status: SettlementSpecStatus = SettlementSpecStatus.UNVERIFIED
    reason: str = ""
    findings: dict[str, str] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status is SettlementSpecStatus.VERIFIED

    def unresolved(self) -> tuple[str, ...]:
        return tuple(
            key for key in (U1_WINDOW_PLACEMENT, U2_FEED_ID, U3_PTB_FORM, U4_COMPARISON)
            if self.findings.get(key, UNRESOLVED) == UNRESOLVED
        )


class SpecChecker:
    """Verifies the settlement spec from live payloads, then records the outcome.

    Holds no connection. Payloads are handed to it, so the same check runs
    identically against a live relay and against a recorded frame in a test — which
    matters because the failure path here is the one that decides whether the bot
    trades at all.
    """

    __slots__ = ("_logger", "_result", "_samples")

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger
        self._samples = 0
        self._result = SpecCheckResult(
            findings={
                U1_WINDOW_PLACEMENT: UNRESOLVED,
                U2_FEED_ID: UNRESOLVED,
                U3_PTB_FORM: UNRESOLVED,
                U4_COMPARISON: UNRESOLVED,
            }
        )

    @property
    def result(self) -> SpecCheckResult:
        return self._result

    @property
    def samples_seen(self) -> int:
        return self._samples

    def offer(self, payload: object) -> SpecCheckResult:
        """Inspect one settlement payload. Returns the running result.

        The window assertion is what can actually fail here (TRAP 2). A stream with
        no `windowSeconds`, or one declaring a different length, is the wrong stream,
        and recording its prices as settlement means would produce a plausible wrong
        model of how the venue settles.
        """
        try:
            declared = assert_settlement_window(payload)
        except SettlementWindowAssertionError as exc:
            self._result = SpecCheckResult(
                status=SettlementSpecStatus.FAILED,
                reason=str(exc),
                findings=dict(self._result.findings),
            )
            return self._result

        self._samples += 1
        findings = dict(self._result.findings)

        # U1: the declared length is confirmed; the PLACEMENT of the window relative
        # to close still is not. Recording the confirmed half explicitly so the
        # remaining unknown is not mistaken for a total unknown.
        findings[U1_WINDOW_PLACEMENT] = (
            f"length={declared}s confirmed; placement relative to close still {UNRESOLVED}"
        )

        # U2: the exact feed ID, read from the payload rather than assumed. Recording
        # whatever the stream reports is how the post-mainnet ID gets pinned down.
        if isinstance(payload, dict):
            feed_id = payload.get("feedId", payload.get("feed_id"))
            if isinstance(feed_id, str) and feed_id.strip():
                findings[U2_FEED_ID] = feed_id.strip()

        if self._samples >= _SAMPLE_TARGET:
            self._result = SpecCheckResult(
                status=SettlementSpecStatus.VERIFIED,
                reason="",
                findings=findings,
            )
        else:
            self._result = SpecCheckResult(
                status=SettlementSpecStatus.UNVERIFIED,
                reason=f"only {self._samples} of {_SAMPLE_TARGET} samples inspected",
                findings=findings,
            )
        return self._result

    def record_settled_market(
        self,
        *,
        settlement_twap: Observation | None,
        ptb: Observation | None,
    ) -> SpecCheckResult:
        """Record observations from a settled market against U3 and U4.

        Left as observation rather than inference. Distinguishing `>=` from `>` needs
        a market that settled with the two values exactly equal, which is rare; a
        guess from a market that was not exactly equal would look like evidence and
        would be wrong. The same applies to U3: one market cannot tell a snapshot PTB
        from a 30s TWAP PTB unless the two happen to differ.
        """
        findings = dict(self._result.findings)
        if settlement_twap is not None and ptb is not None:
            if settlement_twap.price == ptb.price:
                findings[U4_COMPARISON] = (
                    f"exact tie observed at {ptb.price}; venue outcome decides >= vs >"
                )
            if ptb.window_seconds == SETTLEMENT_WINDOW_SECONDS:
                findings[U3_PTB_FORM] = (
                    f"PTB payload declared windowSeconds={SETTLEMENT_WINDOW_SECONDS}"
                )
            elif ptb.window_seconds is None:
                findings[U3_PTB_FORM] = (
                    "PTB payload declared no window — consistent with a snapshot"
                )
        self._result = SpecCheckResult(
            status=self._result.status,
            reason=self._result.reason,
            findings=findings,
        )
        return self._result

    def apply(self, runtime: RuntimeState) -> SpecCheckResult:
        """Persist the outcome and set trading_enabled accordingly.

        On anything short of VERIFIED this disables trading and returns normally. It
        does not raise: the caller is startup step 5, and a raise there would take
        down a process that is required to keep serving its dashboard and keep
        accumulating its TWAP (A8).
        """
        result = self._result
        if result.verified:
            runtime.record_spec_status(SettlementSpecStatus.VERIFIED)
            runtime.enable_trading()
            log_event(
                logging.INFO,
                "Spec Verified",
                "settlement spec verified — trading enabled",
                logger=self._logger,
            )
        else:
            runtime.record_spec_status(result.status, result.reason)
            log_event(
                logging.ERROR,
                "Spec Unverified",
                f"trading disabled — {result.reason or 'settlement spec not verified'}",
                logger=self._logger,
            )
        return result
