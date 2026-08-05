"""The Decision Engine: the ONLY place an ExecutionIntent is created.

The pipeline, in order, and nothing skips a step:

    Completed Window -> Validate Window -> Apply Risk Gates -> Create Intent
                     -> Persist Intent -> Return Intent

What this layer does NOT do, deliberately:

    it never submits, modifies or cancels an order
    it never touches a wallet, a key or a credential
    it never calls the venue or any price provider
    it never mutates a window's frozen values or any runtime flag
    it never recomputes PTB, signal TWAP, direction, buffer or locked trigger

That last one is the important one. Every one of those five values arrives already
frozen on the window, and re-deriving any of them here would produce numbers that
disagree with what was persisted at freeze time — after which the process keeps
running, looks entirely healthy, and trades a trigger nobody configured (A4/A12).

There is no clock gate anywhere in this package. The lead-time gate is repealed
entirely (A10/D1); the only execution boundary is the market phase.
"""

from __future__ import annotations

from arc.decision.engine import DecisionEngine, DecisionOutcome, WindowDecision
from arc.decision.intent import build_intent, intent_id_for
from arc.decision.quota import QuotaLedger, QuotaSnapshot
from arc.decision.reasons import SkipReason
from arc.decision.snapshot import DecisionSnapshot, snapshot_for
from arc.decision.strategy import context_for

__all__ = [
    "DecisionEngine",
    "DecisionOutcome",
    "DecisionSnapshot",
    "QuotaLedger",
    "QuotaSnapshot",
    "SkipReason",
    "WindowDecision",
    "build_intent",
    "context_for",
    "intent_id_for",
    "snapshot_for",
]
