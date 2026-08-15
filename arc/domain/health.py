"""The runtime health snapshot, read once per pass.

Lives in the domain layer because every engine above the executors reads it —
the MAJORITY engine gathers it, the runtime builds it, and the risk gates
consume it. No layer owns it exclusively.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from arc.domain.enums import SettlementSpecStatus

__all__ = ["RuntimeHealth"]

_ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    """Process-wide state the risk gates need, read once per decision pass.

    Gathered by the caller into one frozen object rather than reached for gate by
    gate. Nineteen gates each pulling live readings would evaluate nineteen
    slightly different worlds, and the verdict would depend on how long evaluation
    took.
    """

    trading_enabled: bool
    spec_status: SettlementSpecStatus
    # The operator's Start Trading switch. Defaults False for the same reason the
    # risk gate does: a caller that forgets to gather it records the decision and
    # submits nothing, rather than submitting because a field was missing.
    execution_armed: bool = False
    paused: bool = False
    trading_disabled_reason: str = ""
    feed_blocked: bool = False
    feed_age_ms: float | None = None
    clock_drift_critical: bool = False
    clock_drift_ms: float = 0.0
    healthy: bool = True
    detail: str = ""
    open_positions: int = 0
    daily_loss_usd: Decimal = _ZERO
    consecutive_losses: int = 0
    # ── the live-money preconditions (gates 16-19) ───────────────────────────
    # Each defaults to the value that means "nothing is wrong", because that is
    # what is true of every caller that does not have a venue: V1, the inert
    # runtime and every unit test. Gate 2's arming switch defaults the other way
    # on purpose — it is the operator's intent, and absence of intent is not
    # consent — but absence of an orphan is genuinely the absence of an orphan.
    supervisor_ready: bool = True
    supervisor_detail: str = ""
    wallet_connected: bool = True
    wallet_status: str = ""
    orphan_orders: tuple[str, ...] = ()
    # None = no official source published a balance. Never zero as a stand-in:
    # zero is a real, denying figure and "unknown" must not be able to look like
    # an empty account.
    available_balance: Decimal | None = None
    # Which runtime produced this pass. Carried so a denial line says whether the
    # refusal happened in V1 or V2 — the same denial means different things in a
    # paper run and a live one.
    mode: str = ""
    # Bumped by the runtime whenever any field above changes. The dashboard
    # redraws on a change of this number rather than on every frame.
    health_revision: int = 0
