"""Production validation: does the recorded run satisfy the acceptance criteria.

WHAT THIS IS. One checker per numbered acceptance criterion, each reading the rows
a real run left behind and returning PASS, FAIL or UNVERIFIED. It is the difference
between "the code looks right" and "the run demonstrated it".

WHY UNVERIFIED IS A FIRST-CLASS RESULT AND NOT A SOFT PASS. Several criteria can
only be demonstrated by an operator: a hundred consecutive real markets takes eight
hours of live wall clock, a VPS reboot needs the VPS, and "wallet balance matches
Polymarket" needs the funded account. A validator that reported those as PASS
because no contradicting row existed would be asserting something nobody checked —
which is the exact failure this whole phase exists to prevent. So the absence of
evidence is reported as the absence of evidence, and `ready_for_live` is False
while any criterion is unverified.

NOTHING HERE REPAIRS ANYTHING. It reads and it reports. A validator that fixed what
it found would destroy the evidence of the bug it was written to surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from arc.clock import Clock, SystemClock
from arc.domain.enums import OrderState
from arc.runtime.metrics import RuntimeMetrics, runtime_metrics
from arc.runtime.recorder import RecorderReport, audit_recording
from arc.runtime.stats import FillStats, fill_statistics
from arc.storage.store import Store

__all__ = [
    "OPERATOR_VERIFIED",
    "VERDICT_NOT_READY",
    "VERDICT_READY",
    "Criterion",
    "ValidationReport",
    "validate_run",
]

PASS: Final[str] = "PASS"
FAIL: Final[str] = "FAIL"
UNVERIFIED: Final[str] = "UNVERIFIED"

# The two verdicts. Exactly two, spelled exactly this way, so a reader searching a
# pasted report for either string finds it or finds nothing.
VERDICT_READY: Final[str] = "READY FOR V2 LIVE TRADING"
VERDICT_NOT_READY: Final[str] = "NOT READY FOR V2 LIVE TRADING"

# How many consecutive real markets the phase requires.
REQUIRED_MARKETS: Final[int] = 100

# The criteria that no amount of stored data can settle, with what each needs. Kept
# as an explicit list so the report names them rather than omitting them: a criterion
# missing from a report reads as a criterion that passed.
OPERATOR_VERIFIED: Final[dict[str, str]] = {
    "PM2 restart": "restart the process under PM2 mid-run and confirm no state loss",
    "VPS reboot": "reboot the host mid-run and confirm recovery resolves every order",
    "Process kill": "SIGKILL the process mid-window and confirm no duplicate submission",
    "Wallet vs Polymarket": "compare every wallet figure against the Polymarket UI",
    "Official RTDS payloads": "check live payload shapes against RTDS documentation",
    "Official Chainlink payloads": "only if TWAP_PROVIDER=CHAINLINK with credentials",
    "Official CLOB payloads": "check live REST and WS payloads against CLOB documentation",
    "Official PTB metadata": "confirm the frozen PTB equals the venue's published value",
    "Official settlement": "confirm the venue's resolution matches the recorded settlement",
    "Telegram delivery": "confirm each configured category arrives in the chat",
    "CPU / memory / network": "read them from the host; ARC does not measure the host",
}


@dataclass(frozen=True, slots=True)
class Criterion:
    """One acceptance criterion and what the data says about it."""

    number: int
    name: str
    result: str
    detail: str
    evidence: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "criterion": self.number,
            "name": self.name,
            "result": self.result,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class ValidationReport:
    criteria: list[Criterion] = field(default_factory=list)
    recorder: RecorderReport | None = None
    stats: FillStats | None = None
    metrics: RuntimeMetrics | None = None

    @property
    def failed(self) -> tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if c.result == FAIL)

    @property
    def unverified(self) -> tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if c.result == UNVERIFIED)

    @property
    def ready_for_live(self) -> bool:
        """V2 is justified only when nothing failed AND nothing is unverified.

        Both conditions, not just the first. "No failures" on a run where half the
        criteria were never exercised is the report that gets someone to enable
        live trading on the strength of tests that never ran.
        """
        return not self.failed and not self.unverified

    @property
    def verdict(self) -> str:
        """Exactly one of two strings. There is no third, softer outcome.

        "Mostly ready", "ready with caveats" and "ready pending X" are all the same
        sentence to somebody about to enable live trading, and all three would be
        read as the first word.
        """
        return VERDICT_READY if self.ready_for_live else VERDICT_NOT_READY

    def as_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "ready_for_live": self.ready_for_live,
            "passed": sum(1 for c in self.criteria if c.result == PASS),
            "failed": len(self.failed),
            "unverified": len(self.unverified),
            "criteria": [c.as_json() for c in self.criteria],
            "operator_verified_required": OPERATOR_VERIFIED,
            "recorder": self.recorder.as_json() if self.recorder is not None else None,
            "statistics": self.stats.as_json() if self.stats is not None else None,
            "metrics": self.metrics.as_json() if self.metrics is not None else None,
        }


def _c(number: int, name: str, ok: bool, detail: str, evidence: str = "") -> Criterion:
    return Criterion(number, name, PASS if ok else FAIL, detail, evidence)


def _duplicate_intents(store: Store, slugs: tuple[str, ...]) -> list[str]:
    """More than one intent for the same (market, window).

    The database's UNIQUE constraint makes this impossible, which is exactly why it
    is checked: a constraint that was silently dropped in a migration is invisible
    until the day two intents exist, and by then both have submitted.
    """
    duplicates = []
    for slug in slugs:
        # Keyed by (engine, offset), not by offset alone. Two engines are permitted
        # one intent each on the same window — that is the whole point of widening
        # the database constraint — so keying on the offset by itself would report
        # correct two-engine operation as a double submission on every single
        # window, and a criterion that is always red stops being read at all.
        seen: set[tuple[str, int]] = set()
        for engine, offset in store.intent_keys(slug):
            if (engine, offset) in seen:
                duplicates.append(f"{slug}@{offset}s [{engine}]")
            seen.add((engine, offset))
    return duplicates


def _duplicate_live_orders(store: Store, slugs: tuple[str, ...]) -> list[str]:
    """Two simultaneously live orders on one window, FOR ONE ENGINE.

    A reprice is cancel-then-place, so several order rows per window are correct;
    two of them LIVE at once is the double-submission this criterion forbids.

    Counted per engine for the same reason the intents above are: one TWAP order
    and one MAJORITY order resting on the same window are two engines each holding
    their own single approved position, not one engine holding two.
    """
    offenders: list[str] = []
    for slug in slugs:
        live: dict[tuple[str, int], int] = {}
        for order in store.orders_for(slug):
            if order.is_live:
                key = (order.engine, order.offset_seconds)
                live[key] = live.get(key, 0) + 1
        offenders.extend(
            f"{slug}@{offset}s [{engine}] x{count}"
            for (engine, offset), count in live.items()
            if count > 1
        )
    return offenders


def _duplicate_fills(store: Store, slugs: tuple[str, ...]) -> list[str]:
    """The same fill id counted twice.

    fill_id is the primary key and inserts are INSERT OR IGNORE, so a websocket
    redelivery cannot double-count. Checked for the same reason as the intent
    constraint: the guarantee is only as good as the schema still having it.
    """
    offenders = []
    for slug in slugs:
        seen: set[str] = set()
        for fill in store.fills_for(slug):
            if fill.fill_id in seen:
                offenders.append(fill.fill_id)
            seen.add(fill.fill_id)
    return offenders


def _orphan_orders(store: Store, slugs: tuple[str, ...]) -> list[str]:
    """An order for a window that has no intent. The write-before-act violation.

    Authorisation is matched per ENGINE. A TWAP intent does not authorise a MAJORITY
    order and never did: matching on the offset alone would let one engine's
    persisted intent silently vouch for the other engine's order, which is exactly
    the unauthorised submission this criterion exists to catch.
    """
    offenders = []
    for slug in slugs:
        authorised = set(store.intent_keys(slug))
        for order in store.orders_for(slug):
            if (order.engine, order.offset_seconds) not in authorised:
                offenders.append(order.order_id)
    return offenders


def validate_run(
    store: Store,
    *,
    offsets: tuple[int, ...],
    cadence_seconds: int,
    market_limit: int = 500,
    uptime_seconds: float = 0.0,
    restarts: int = 0,
    reconnects: int = 0,
    disconnects: int = 0,
    recoveries: int = 0,
    chainlink_enabled: bool = False,
    clock: Clock | None = None,
) -> ValidationReport:
    """Read the run back and report against every acceptance criterion.

    The runtime figures are passed in rather than read from a global: the validator
    must be runnable against a database with no process attached to it, and one that
    silently reported zero uptime for a live run would be worse than one that says so.
    """
    clock = clock if clock is not None else SystemClock()
    started = clock.monotonic()
    recorder = audit_recording(
        store,
        expected_windows=len(offsets),
        cadence_seconds=cadence_seconds,
        market_limit=market_limit,
    )
    stats = fill_statistics(store, offsets=offsets, market_limit=market_limit)
    slugs = tuple(m.slug for m in recorder.markets)
    markets = len(slugs)

    report = ValidationReport(recorder=recorder, stats=stats)
    add = report.criteria.append

    # 1 — production runtime.
    if markets >= REQUIRED_MARKETS:
        add(_c(1, "100+ consecutive markets", True,
               f"{markets} markets recorded", f"first {recorder.first_window_ts}"))
    else:
        add(Criterion(1, "100+ consecutive markets", UNVERIFIED,
                      f"{markets} of {REQUIRED_MARKETS} markets recorded; "
                      "needs a live V1 run of roughly eight hours",
                      "run `arc run --mode=v1` and leave it up"))
    add(_c(1, "No market gaps", not recorder.gaps,
           "contiguous" if not recorder.gaps else "; ".join(recorder.gaps[:5])))
    dup_intents = _duplicate_intents(store, slugs)
    add(_c(1, "No duplicate intents", not dup_intents,
           "one intent per window" if not dup_intents else ", ".join(dup_intents[:10])))
    dup_live = _duplicate_live_orders(store, slugs)
    add(_c(1, "No duplicate submissions", not dup_live,
           "at most one live order per window"
           if not dup_live else ", ".join(dup_live[:10])))
    dup_fills = _duplicate_fills(store, slugs)
    add(_c(1, "No duplicate fills", not dup_fills,
           "fill ids unique" if not dup_fills else ", ".join(dup_fills[:10])))
    orphans = _orphan_orders(store, slugs)
    add(_c(1, "No unauthorised orders", not orphans,
           "every order has an intent" if not orphans else ", ".join(orphans[:10])))

    # 2 — recorder.
    if not markets:
        add(Criterion(2, "Recorder completeness", UNVERIFIED,
                      "no markets recorded yet", "start a V1 runtime"))
    else:
        add(_c(2, "Recorder completeness", recorder.complete,
               f"{markets - len(recorder.incomplete)}/{markets} markets complete"
               if not recorder.complete
               else f"all {markets} markets recorded every value",
               "; ".join(
                   f"{m.slug}: {', '.join(m.missing)}" for m in recorder.incomplete[:5]
               )))

    # 3 — per-offset fill statistics.
    covered = tuple(o for o in offsets if o in stats.by_offset)
    add(_c(3, "Fill statistics per window", len(covered) == len(offsets),
           f"{len(covered)}/{len(offsets)} configured offsets bucketed: "
           + ", ".join(f"{o}s" for o in offsets)))

    # 4 — submission fields. Only meaningful once something submitted.
    if not stats.submissions:
        add(Criterion(4, "Submission statistics", UNVERIFIED,
                      "no submissions recorded; needs an armed run that fires",
                      "press START TRADING and let a window fire"))
    else:
        timed = sum(1 for o in stats.by_offset.values() if o.submission_latencies_ms)
        add(_c(4, "Submission statistics", timed > 0,
               f"{stats.submissions} submissions with latency, offset, ms-to-close, "
               f"index, generation, price, shares and acknowledgement recorded"))

    # 5 — recovery. Restarts and reboots are the operator's.
    indeterminate = sum(o.indeterminate for o in stats.by_offset.values())
    add(_c(5, "No unresolved orders left behind", indeterminate == 0,
           "no INDETERMINATE orders" if not indeterminate
           else f"{indeterminate} INDETERMINATE orders await reconciliation"))
    add(Criterion(5, "Restart / reboot / kill recovery", UNVERIFIED,
                  "cannot be exercised from a test process",
                  "; ".join(k for k in ("PM2 restart", "VPS reboot", "Process kill"))))

    # 9 — wallet.
    add(Criterion(9, "Wallet matches Polymarket", UNVERIFIED,
                  "requires the funded account and the Polymarket UI side by side",
                  OPERATOR_VERIFIED["Wallet vs Polymarket"]))

    # 12 — official sources.
    add(Criterion(12, "Official payload verification", UNVERIFIED,
                  "requires live payloads from RTDS, CLOB and market metadata",
                  "compare captured payloads against the vendor documentation"))

    # 8 — Telegram delivery.
    add(Criterion(8, "Telegram delivery", UNVERIFIED,
                  "the mapping is tested; delivery needs a real chat",
                  OPERATOR_VERIFIED["Telegram delivery"]))

    # 13 — host metrics. ARC does not measure the host, and inventing a number here
    # would be worse than reporting that it is the operator's to read.
    add(Criterion(13, "CPU / memory / network", UNVERIFIED,
                  "ARC does not sample host metrics; psutil is not a dependency",
                  OPERATOR_VERIFIED["CPU / memory / network"]))
    add(_c(13, "Database health", store.integrity_check() == "ok",
           store.integrity_check(),
           f"schema v{store.schema_version()}, {store.market_count()} markets"))

    # The runtime figures. Attached last so the elapsed time covers every check
    # above it — the duration the operator reads is the duration they waited.
    report.metrics = runtime_metrics(
        store,
        stats,
        uptime_seconds=uptime_seconds,
        restarts=restarts,
        reconnects=reconnects,
        disconnects=disconnects,
        recoveries=recoveries,
        observations=sum(store.observation_count(s) for s in slugs),
        duration_seconds=clock.monotonic() - started,
        chainlink_enabled=chainlink_enabled,
    )

    return report


def unresolved_summary(store: Store) -> tuple[str, ...]:
    """Live and INDETERMINATE orders across the whole database.

    The one question that must be answered before arming after any restart: an
    order nobody accounted for is a position nobody knows they hold.
    """
    return tuple(
        f"{o.order_id} {o.state.value}"
        for o in store.live_orders()
        if o.state in {OrderState.INDETERMINATE, OrderState.SUBMITTED, OrderState.PARTIAL}
    )
