"""`trading_enabled` and why. Persisted, so a restart cannot re-enable trading.

The flag defaults to DISABLED and is only ever enabled by something that has
actually verified a precondition. That direction matters: if the default were
enabled, then every new failure mode nobody has thought of yet arrives as a
trading bot, and only the failures somebody remembered to wire a disable into
would stop it.

It is persisted for the same reason. A process that disabled trading because the
settlement spec could not be verified, then crashed and came back up, must not
come back up trading. The reason string survives with the flag so the operator
sees the original cause rather than a bare disabled state with no explanation.

This module does not enforce anything. Enforcement lives at the order-submission
boundary inside the Risk Engine (A8), which is the single place an order can leave
the process, so a caller who forgets to consult this flag still cannot submit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from arc.clock import Clock
from arc.domain.enums import DenialReason, SettlementSpecStatus
from arc.storage.store import Store

__all__ = [
    "KEY_SPEC_STATUS",
    "KEY_TRADING_ENABLED",
    "KEY_TRADING_REASON",
    "RuntimeState",
    "TradingGate",
]

KEY_TRADING_ENABLED: Final[str] = "trading_enabled"
KEY_TRADING_REASON: Final[str] = "trading_disabled_reason"
KEY_SPEC_STATUS: Final[str] = "settlement_spec_status"

# Stored as text, not 0/1: a human reading the runtime_state table with sqlite3
# should not have to remember which integer means live trading.
_TRUE: Final[str] = "true"
_FALSE: Final[str] = "false"


@dataclass(frozen=True, slots=True)
class TradingGate:
    """A snapshot of whether trading is permitted, and why not."""

    enabled: bool
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return not self.enabled


class RuntimeState:
    """Reads and writes the runtime flags through the storage boundary.

    Every mutation writes to SQLite before the in-memory value changes. If the
    write fails the flag does not move, so the process never believes trading is
    enabled on the strength of a state that was not durably recorded.
    """

    __slots__ = ("_clock", "_enabled", "_reason", "_spec_status", "_store")

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        # Disabled until something verifies otherwise. See the module docstring.
        self._enabled = False
        self._reason = DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED.value
        self._spec_status = SettlementSpecStatus.UNVERIFIED

    # ── load ─────────────────────────────────────────────────────────────────

    def load(self) -> TradingGate:
        """Restore the persisted flags. Absent rows leave the safe defaults.

        A database with no runtime_state rows is a first run, and a first run has
        verified nothing, so the defaults set in __init__ are exactly right.
        """
        stored_enabled = self._store.get_runtime_state(KEY_TRADING_ENABLED)
        if stored_enabled is not None:
            self._enabled = stored_enabled == _TRUE

        stored_reason = self._store.get_runtime_state(KEY_TRADING_REASON)
        if stored_reason is not None:
            self._reason = stored_reason
        elif self._enabled:
            self._reason = ""

        stored_status = self._store.get_runtime_state(KEY_SPEC_STATUS)
        if stored_status is not None:
            try:
                self._spec_status = SettlementSpecStatus(stored_status)
            except ValueError:
                # An unrecognised status is not a status. Treat it as unverified
                # rather than as anything that might permit trading.
                self._spec_status = SettlementSpecStatus.UNVERIFIED
                self._enabled = False
                self._reason = DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED.value

        return self.gate

    # ── read ─────────────────────────────────────────────────────────────────

    @property
    def trading_enabled(self) -> bool:
        return self._enabled

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def spec_status(self) -> SettlementSpecStatus:
        return self._spec_status

    @property
    def gate(self) -> TradingGate:
        return TradingGate(enabled=self._enabled, reason=self._reason)

    # ── write ────────────────────────────────────────────────────────────────

    def enable_trading(self) -> TradingGate:
        """Permit trading. Refuses while the settlement spec is not VERIFIED.

        The refusal is here rather than at the call site because there is exactly
        one condition under which this build is allowed to trade, and a caller that
        could bypass it makes the whole verification step decorative.
        """
        if self._spec_status is not SettlementSpecStatus.VERIFIED:
            return self.disable_trading(DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED.value)
        self._persist(enabled=True, reason="")
        return self.gate

    def disable_trading(self, reason: str) -> TradingGate:
        """Block trading and record why. Idempotent; a later reason overwrites."""
        if not reason:
            raise ValueError("disabling trading requires a reason; a blank one explains nothing")
        self._persist(enabled=False, reason=reason)
        return self.gate

    def record_spec_status(self, status: SettlementSpecStatus, reason: str = "") -> TradingGate:
        """Record the settlement-spec verification outcome.

        Anything other than VERIFIED disables trading in the same call, so there is
        no window in which a FAILED verification has been recorded but the flag has
        not yet caught up.
        """
        now = self._clock.now()
        self._store.set_runtime_state(KEY_SPEC_STATUS, status.value, now)
        self._spec_status = status

        if status is SettlementSpecStatus.VERIFIED:
            return self.gate
        return self.disable_trading(
            reason or DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED.value
        )

    def _persist(self, *, enabled: bool, reason: str) -> None:
        now = self._clock.now()
        self._store.set_runtime_state(KEY_TRADING_ENABLED, _TRUE if enabled else _FALSE, now)
        self._store.set_runtime_state(KEY_TRADING_REASON, reason, now)
        self._enabled = enabled
        self._reason = reason
