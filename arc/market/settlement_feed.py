"""The venue's 30-second settlement TWAP. OBSERVATIONAL ONLY.

Nothing in this module feeds a decision, in this phase or any later one (A6). Three
quantities are never conflated:

    signal_twap      ARC's own 300s cumulative mean — the STRATEGY INPUT
    settlement_twap  the VENUE's Chainlink 30s TWAP over the settlement window —
                     the OUTCOME quantity, recorded so U1 and U4 can be answered
                     from real data instead of guessed
    ptb              the immutable official opening reference

What is recorded here is the second one. It is written to storage and READ BY
NOTHING. That is deliberate and is asserted by the test suite: the moment a
strategy reads the venue's settlement mean, ARC is trading on the answer instead of
on its signal, and every backtest built on it is meaningless.

TRAP 2 is enforced here, at startup, by asserting `windowSeconds == 30` in the
payload. The feed IDs changed at mainnet launch, and the TWAP stream is NOT
`0x0003…75b8` / `BTC/USD-RefPrice-DS-Premium-Global-003`. A reference stream carries
no `windowSeconds` field at all, so the assertion fails loudly rather than quietly
recording reference prices as if they were the settlement mean. The check reads the
payload's own declared field; it never infers the window from how often messages
arrive (TRAP 1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from arc.domain.models import Observation
from arc.domain.timing import SETTLEMENT_WINDOW_SECONDS, TWAP_SETTLEMENT_EFFECTIVE_TS
from arc.errors import FeedError, ObservationRejectedError
from arc.logging_setup import log_event
from arc.market.validation import parse_payload

__all__ = [
    "EXPECTED_WINDOW_SECONDS",
    "SettlementTwapCollector",
    "SettlementWindowAssertionError",
    "assert_settlement_window",
]

# 30, taken from the timing module rather than restated, so a change to the venue's
# settlement window cannot leave two different constants disagreeing.
EXPECTED_WINDOW_SECONDS: Final[int] = SETTLEMENT_WINDOW_SECONDS

# Rendered once, for the refusal message only. A correct pre-switchover refusal and a
# genuine wrong-stream fault are the same exception with the same consequence, and
# without this note they read identically in the log — so an operator debugging a
# silent bot cannot tell "waiting for the venue" from "connected to the wrong feed".
_TWAP_EFFECTIVE_TEXT: Final[str] = (
    datetime.fromtimestamp(TWAP_SETTLEMENT_EFFECTIVE_TS, UTC).strftime("%Y-%m-%d %H:%M UTC")
)


class SettlementWindowAssertionError(FeedError):
    """The stream is not the 30-second TWAP stream (TRAP 2).

    Operational, not fatal: the process must still start, still serve its dashboard
    and still accumulate its signal TWAP. What it must not do is trade, and that is
    decided by the spec check, not by this exception.
    """


def assert_settlement_window(payload: object) -> int:
    """Assert the payload declares a 30-second window. Returns the declared value.

    Raises when the field is ABSENT as well as when it disagrees. Absence is the
    reference-stream signature, and treating it as "unknown, probably fine" is
    exactly how reference prices end up recorded as settlement means.
    """
    if not isinstance(payload, dict):
        raise SettlementWindowAssertionError(
            f"settlement payload is not an object: {type(payload)}"
        )

    raw = payload.get("windowSeconds", payload.get("window_seconds"))
    if raw is None:
        raise SettlementWindowAssertionError(
            "settlement payload carries no windowSeconds field — this is a reference "
            "stream, not the 30-second TWAP stream (TRAP 2). Note: before "
            f"{_TWAP_EFFECTIVE_TEXT} the venue has not switched crypto up/down markets "
            "to TWAP settlement and NO stream carries this field, so this is also the "
            "expected pre-switchover reading; either way the stream is not a "
            "30-second TWAP and trading stays disabled"
        )
    try:
        declared = int(float(raw))
    except (TypeError, ValueError) as exc:
        raise SettlementWindowAssertionError(
            f"windowSeconds {raw!r} is not a number"
        ) from exc

    if declared != EXPECTED_WINDOW_SECONDS:
        raise SettlementWindowAssertionError(
            f"stream declares windowSeconds={declared}, expected {EXPECTED_WINDOW_SECONDS}; "
            "this is the wrong feed for 5-minute market settlement (TRAP 2)"
        )
    return declared


@dataclass(slots=True)
class SettlementTwapCollector:
    """Accumulates the venue's settlement observations for ONE market.

    Exact sum and count, divided on read, for the same reason as the signal
    accumulator (hazard H1): the incremental mean form rounds at every step.

    Per-market instance, created fresh and dropped at close. No reset path (A11).
    """

    market_slug: str
    close_ts: int
    running_sum: Decimal = Decimal(0)
    observation_count: int = 0
    rejected_count: int = 0
    window_asserted: bool = False

    @property
    def window_start(self) -> int:
        """The first instant of the settlement averaging window.

        `[close_ts - 30, close_ts]` is this build's reading, and it is UNVERIFIED
        (A8/U1): the venue does not document whether the window ends at close or
        straddles it. Both readings are recorded observationally, which is what will
        let U1 be answered from data. Nothing decides on this value.
        """
        return self.close_ts - EXPECTED_WINDOW_SECONDS

    def in_window(self, ts: float) -> bool:
        return self.window_start <= ts <= self.close_ts

    @property
    def settlement_twap(self) -> Decimal | None:
        """The venue's mean over the window. None when nothing was collected."""
        if self.observation_count == 0:
            return None
        return self.running_sum / self.observation_count

    def offer(self, observation: Observation) -> bool:
        """Fold an observation in if it falls inside the window. Returns acceptance.

        Observations outside the window are DROPPED, not clamped to the edge. A
        clamped sample would shift the recorded settlement mean and make the U1
        comparison — the entire reason this data exists — answer the wrong question.
        """
        if not self.in_window(observation.ts):
            return False
        self.running_sum += observation.price
        self.observation_count += 1
        return True

    def offer_payload(
        self,
        payload: object,
        *,
        expected_symbol: str,
        logger: logging.Logger | None = None,
    ) -> bool:
        """Assert the window, parse, then offer. Any failure counts as a rejection.

        The window assertion runs on the FIRST payload and then not again: it is a
        property of the stream, not of a message, and re-asserting per message would
        turn one malformed frame into a stream-level failure.
        """
        try:
            if not self.window_asserted:
                assert_settlement_window(payload)
                self.window_asserted = True
                log_event(
                    logging.INFO,
                    "Settlement Window",
                    f"stream declares windowSeconds={EXPECTED_WINDOW_SECONDS}",
                    logger=logger,
                )
            observation = parse_payload(payload, expected_symbol=expected_symbol)
        except (SettlementWindowAssertionError, ObservationRejectedError) as exc:
            self.rejected_count += 1
            log_event(
                logging.WARNING,
                "Settlement Rejected",
                f"{self.market_slug}  {exc}",
                logger=logger,
            )
            return False
        return self.offer(observation)
