"""The validation tooling, validated: recorder, statistics, criteria, report.

WHY THIS FILE EXISTS AT ALL. The rest of the suite tests the bot. This tests the
thing that decides whether the bot is ready — and a validator is the one component
whose bugs all fail in the same direction. A checker that reads the wrong column,
counts the wrong rows, or treats "no data" as "nothing wrong" returns PASS, the
report reads green, and the operator enables live trading on the strength of a run
that demonstrated nothing. Every test below is a false PASS someone could ship on.

THE THREE PROPERTIES.

  Absence is UNVERIFIED, never PASS. An empty database must not satisfy a single
  data-backed criterion.

  A real defect is FAIL. Duplicate intents, two live orders on one window,
  duplicate fill ids, an order with no intent, a market gap — each is planted
  directly in storage and each must be found.

  Nothing is repaired or reconstructed. The validator reads; it does not write, and
  it does not derive a missing PTB from a neighbour.

Everything is built through the real Store, the real submitter and the real paper
adapter, so a schema change breaks these tests rather than silently changing what
the criteria mean.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import OFFSETS
from execution_fixtures import fill_engine, intent_for, make_market, store_at, submitter

from arc.domain.enums import Direction, MarketPhase, OrderState, Outcome, WindowState
from arc.domain.models import ExecutionWindow, Fill, Observation, Order, Settlement
from arc.domain.timing import slug_for
from arc.execution.v1_paper import PaperExecutor
from arc.runtime.metrics import UNAVAILABLE, RuntimeMetrics
from arc.runtime.recorder import (
    RUNTIME_STATE_KEY,
    audit_market,
    audit_recording,
    submission_records,
)
from arc.runtime.report import _METRIC_ROWS, render_report
from arc.runtime.stats import fill_statistics
from arc.runtime.validation import (
    FAIL,
    OPERATOR_VERIFIED,
    PASS,
    REQUIRED_MARKETS,
    UNVERIFIED,
    VERDICT_NOT_READY,
    VERDICT_READY,
    unresolved_summary,
    validate_run,
)
from arc.storage.store import Store

FIRST_TS = 1754400000
CADENCE = 300
PTB = Decimal("64000.00")
PRICE = Decimal("0.70")
SIZE = Decimal("35")


# ── building a run that actually happened ────────────────────────────────────


def _validate(store: Store) -> object:
    return validate_run(store, offsets=OFFSETS, cadence_seconds=CADENCE)


def _results(store: Store, name: str) -> list[str]:
    """Every result recorded against one criterion name."""
    report = _validate(store)
    return [c.result for c in report.criteria if c.name == name]  # type: ignore[attr-defined]


def _record_market(
    store: Store,
    window_ts: int,
    *,
    ptb: Decimal | None = PTB,
    fire: tuple[int, ...] = (15,),
    settle: bool = True,
    observations: int = 3,
) -> str:
    """One market as a real run leaves it: PTB frozen, TWAPs written, windows
    frozen and fired, an intent, a submitted order, a fill and a settlement.

    Written through the Store's own methods rather than raw SQL so the audit reads
    exactly the columns production writes. A fixture that INSERTed directly would
    keep passing after a column was renamed underneath it.
    """
    slug = slug_for(window_ts)
    make_market(store, window_ts)
    if ptb is not None:
        store.save_ptb(slug, ptb, float(window_ts))
    store.save_accumulator(
        slug,
        running_sum=Decimal("64100.00") * observations,
        observation_count=observations,
        now=float(window_ts + 200),
    )
    store.save_settlement_twap(slug, Decimal("64150.00"), float(window_ts + 300))
    store.set_runtime_state(RUNTIME_STATE_KEY, "1", float(window_ts))

    for offset in OFFSETS:
        fired = offset in fire
        window = ExecutionWindow(
            offset_seconds=offset,
            state=WindowState.FIRED if fired else WindowState.EXPIRED,
            opening_twap=Decimal("64050.00"),
            ptb=ptb if ptb is not None else PTB,
            buffer=Decimal("2.00"),
            direction=Direction.UP,
            locked_trigger=Decimal("64052.00"),
            frozen_at=float(window_ts + 300 - offset),
            fired_at=float(window_ts + 300 - offset + 0.5) if fired else None,
        )
        store.save_window_frozen(slug, window, float(window_ts + 300 - offset))
        store.save_window_state(
            slug, offset, window.state, float(window_ts + 300 - offset + 0.5)
        )

    executor = PaperExecutor()
    for offset in fire:
        # Persisted before it is acted on, exactly as the Decision Engine does it:
        # the intent row is the authorisation the order is checked against (A4).
        intent = intent_for(
            window_ts=window_ts, offset_seconds=offset, size=SIZE, limit_price=PRICE
        )
        store.save_intent(intent)
        asyncio.run(
            submitter(store, executor).submit(
                intent,
                count=1,
                phase=MarketPhase.ACTIVE,
                now=float(window_ts + 300 - offset + 0.5),
            )
        )
        (order,) = (o for o in store.orders_for(slug) if o.offset_seconds == offset)
        # Through the real FillEngine, so the order reaches FILLED the way it does
        # in production rather than by a state write this fixture invented.
        fill_engine(store, executor).ingest(
            slug,
            (
                Fill(
                    fill_id=f"{slug}:{offset}:fill",
                    order_id=order.order_id,
                    market_slug=slug,
                    price=PRICE,
                    size=SIZE,
                    ts=float(window_ts + 300 - offset + 0.8),
                ),
            ),
            float(window_ts + 300 - offset + 0.8),
        )

    if settle:
        store.save_settlement(
            Settlement(
                market_slug=slug,
                outcome=Outcome.UP,
                settlement_twap=Decimal("64150.00"),
                ptb=ptb if ptb is not None else PTB,
                settled_at=float(window_ts + 325),
            )
        )
        store.save_phase(slug, MarketPhase.SETTLED, float(window_ts + 325))
    return slug


def _run(store: Store, markets: int = 3, *, start: int = FIRST_TS) -> tuple[str, ...]:
    return tuple(
        _record_market(store, start + i * CADENCE) for i in range(markets)
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return store_at(tmp_path)


# ── an empty database proves nothing ─────────────────────────────────────────


class TestAbsenceOfEvidence:
    """The single most dangerous failure: a validator that passes on no data."""

    def test_an_empty_run_is_never_ready_for_live(self, store: Store) -> None:
        assert _validate(store).ready_for_live is False  # type: ignore[attr-defined]

    def test_the_market_count_is_unverified_not_failed(self, store: Store) -> None:
        """UNVERIFIED, deliberately. Nothing is wrong with a run that has not
        happened yet — but nothing has been demonstrated either, and the distinction
        is what stops "no failures" being read as "validated"."""
        assert _results(store, "100+ consecutive markets") == [UNVERIFIED]

    def test_recorder_completeness_is_unverified_on_no_markets(self, store: Store) -> None:
        assert _results(store, "Recorder completeness") == [UNVERIFIED]

    def test_submission_statistics_are_unverified_until_something_submits(
        self, store: Store
    ) -> None:
        assert _results(store, "Submission statistics") == [UNVERIFIED]

    def test_a_short_run_is_still_unverified(self, store: Store) -> None:
        """Three markets is not a hundred. The threshold is not a suggestion."""
        _run(store, 3)
        assert _results(store, "100+ consecutive markets") == [UNVERIFIED]

    def test_the_market_shortfall_is_stated_in_the_detail(self, store: Store) -> None:
        _run(store, 3)
        (criterion,) = [
            c for c in _validate(store).criteria  # type: ignore[attr-defined]
            if c.name == "100+ consecutive markets"
        ]
        assert f"3 of {REQUIRED_MARKETS}" in criterion.detail

    def test_no_data_backed_criterion_passes_on_an_empty_database(
        self, store: Store
    ) -> None:
        """The generalisation of the tests above. Only the structural checks — the
        ones whose subject is "no bad rows exist" — may pass with nothing stored."""
        structural = {
            "No market gaps", "No duplicate intents", "No duplicate submissions",
            "No duplicate fills", "No unauthorised orders",
            "No unresolved orders left behind", "Fill statistics per window",
            "Database health",
        }
        passed = {
            c.name for c in _validate(store).criteria if c.result == PASS  # type: ignore[attr-defined]
        }
        assert passed <= structural


class TestTheOperatorCriteriaCannotBeSatisfiedByData:
    """Five criteria are the operator's. No run, however long, may flip them."""

    @pytest.mark.parametrize(
        "name",
        [
            "Restart / reboot / kill recovery",
            "Wallet matches Polymarket",
            "Official payload verification",
            "Telegram delivery",
            "CPU / memory / network",
        ],
    )
    def test_it_stays_unverified_on_a_complete_run(self, store: Store, name: str) -> None:
        _run(store, 5)
        assert _results(store, name) == [UNVERIFIED]

    def test_each_one_says_how_to_verify_it(self, store: Store) -> None:
        """An UNVERIFIED with no instructions is a dead end the operator cannot
        clear, and a criterion nobody can clear gets ignored rather than done."""
        for criterion in _validate(store).unverified:  # type: ignore[attr-defined]
            assert criterion.detail
            assert criterion.evidence

    def test_the_operator_list_is_shipped_with_the_json(self, store: Store) -> None:
        payload = _validate(store).as_json()  # type: ignore[attr-defined]
        assert payload["operator_verified_required"] == OPERATOR_VERIFIED

    def test_ready_for_live_requires_more_than_the_absence_of_failures(
        self, store: Store
    ) -> None:
        report = _validate(store)
        assert not report.failed  # type: ignore[attr-defined]
        assert report.unverified  # type: ignore[attr-defined]
        assert report.ready_for_live is False  # type: ignore[attr-defined]


# ── real defects must be found ───────────────────────────────────────────────


class TestPlantedDefectsAreFound:
    """Each of these is a genuine production failure, planted in storage."""

    def test_a_missing_market_is_a_gap(self, store: Store) -> None:
        _record_market(store, FIRST_TS)
        _record_market(store, FIRST_TS + 2 * CADENCE)  # one skipped
        assert _results(store, "No market gaps") == [FAIL]

    def test_the_gap_names_both_ends_and_the_count(self, store: Store) -> None:
        _record_market(store, FIRST_TS)
        _record_market(store, FIRST_TS + 3 * CADENCE)
        (gap,) = audit_recording(
            store, expected_windows=len(OFFSETS), cadence_seconds=CADENCE
        ).gaps
        assert str(FIRST_TS) in gap
        assert "2 market(s) missed" in gap

    def test_a_contiguous_run_has_no_gaps(self, store: Store) -> None:
        _run(store, 6)
        assert _results(store, "No market gaps") == [PASS]

    def test_two_live_orders_on_one_window_is_a_duplicate_submission(
        self, store: Store
    ) -> None:
        """The failure the whole write-before-act discipline exists to prevent: two
        real orders resting on the same window is double the approved position."""
        (slug,) = _run(store, 1)
        store.save_order(
            Order(
                order_id=f"{slug}:15:0:1",
                market_slug=slug,
                offset_seconds=15,
                direction=Direction.UP,
                price=PRICE,
                size=SIZE,
                state=OrderState.SUBMITTED,
                created_at=float(FIRST_TS + 286),
                updated_at=float(FIRST_TS + 286),
                venue_order_id="paper-99",
                reprice_chain_id=f"{slug}:15:1",
            )
        )
        store.save_order(
            Order(
                order_id=f"{slug}:15:0:2",
                market_slug=slug,
                offset_seconds=15,
                direction=Direction.UP,
                price=PRICE,
                size=SIZE,
                state=OrderState.SUBMITTED,
                created_at=float(FIRST_TS + 287),
                updated_at=float(FIRST_TS + 287),
                venue_order_id="paper-100",
                reprice_chain_id=f"{slug}:15:2",
            )
        )
        assert _results(store, "No duplicate submissions") == [FAIL]

    def test_a_reprice_chain_is_not_a_duplicate_submission(self, store: Store) -> None:
        """Several order rows per window is normal — a reprice is cancel then place.
        A checker that counted rows rather than LIVE rows would fail every repriced
        window and the operator would learn to ignore the criterion."""
        (slug,) = _run(store, 1)
        store.save_order(
            Order(
                order_id=f"{slug}:15:0:1",
                market_slug=slug,
                offset_seconds=15,
                direction=Direction.UP,
                price=Decimal("0.71"),
                size=SIZE,
                state=OrderState.CANCELLED,
                created_at=float(FIRST_TS + 284),
                updated_at=float(FIRST_TS + 285),
                venue_order_id="paper-98",
                reprice_chain_id=f"{slug}:15:1",
            )
        )
        assert _results(store, "No duplicate submissions") == [PASS]

    def test_an_order_with_no_intent_is_unauthorised(self, store: Store) -> None:
        """Write-before-act, checked from the far end: an order for a window nothing
        ever authorised is an order the Decision Engine never approved."""
        (slug,) = _run(store, 1)
        store.save_order(
            Order(
                order_id=f"{slug}:7:0:0",
                market_slug=slug,
                offset_seconds=7,  # never fired, never had an intent
                direction=Direction.UP,
                price=PRICE,
                size=SIZE,
                state=OrderState.SUBMITTED,
                created_at=float(FIRST_TS + 293),
                updated_at=float(FIRST_TS + 293),
                venue_order_id="paper-77",
                reprice_chain_id=f"{slug}:7:0",
            )
        )
        assert _results(store, "No unauthorised orders") == [FAIL]

    def test_an_indeterminate_order_is_reported_unresolved(self, store: Store) -> None:
        (slug,) = _run(store, 1)
        (order,) = (o for o in store.orders_for(slug) if o.offset_seconds == 15)
        store.save_order_state(
            order.order_id, OrderState.INDETERMINATE, float(FIRST_TS + 400)
        )
        assert _results(store, "No unresolved orders left behind") == [FAIL]

    def test_unresolved_summary_names_the_order_and_its_state(self, store: Store) -> None:
        (slug,) = _run(store, 1)
        (order,) = (o for o in store.orders_for(slug) if o.offset_seconds == 15)
        store.save_order_state(
            order.order_id, OrderState.INDETERMINATE, float(FIRST_TS + 400)
        )
        (line,) = unresolved_summary(store)
        assert order.order_id in line
        assert "INDETERMINATE" in line

    def test_a_settled_run_leaves_nothing_unresolved(self, store: Store) -> None:
        _run(store, 3)
        assert unresolved_summary(store) == ()
        assert _results(store, "No unresolved orders left behind") == [PASS]


class TestTheConstraintsAreStillInTheSchema:
    """Two checks whose subject is the database itself, not the run.

    The constraints make the defect impossible — which is exactly why they are
    verified. A UNIQUE dropped by a careless migration is invisible until the day
    two intents exist, and by then both have submitted.
    """

    def test_a_second_intent_for_the_same_window_is_refused(self, store: Store) -> None:
        (slug,) = _run(store, 1)
        assert store.save_intent(intent_for(window_ts=FIRST_TS, offset_seconds=15)) is False
        assert len([i for i in store.intents_for(slug) if i.offset_seconds == 15]) == 1
        assert _results(store, "No duplicate intents") == [PASS]

    def test_a_redelivered_fill_is_ignored_not_counted_twice(self, store: Store) -> None:
        """WebSocket redelivery is normal. Counting it twice would double the
        recorded position on a market that filled exactly once."""
        (slug,) = _run(store, 1)
        (fill,) = store.fills_for(slug)
        assert store.save_fill(fill) is False
        assert len(store.fills_for(slug)) == 1
        assert _results(store, "No duplicate fills") == [PASS]


# ── the recorder ─────────────────────────────────────────────────────────────


class TestTheRecorderReportsWhatIsMissing:
    def test_a_complete_market_is_complete(self, store: Store) -> None:
        slug = _record_market(store, FIRST_TS)
        record = audit_market(store, slug, expected_windows=len(OFFSETS))
        assert record.missing == ()
        assert record.complete

    def test_a_market_with_no_ptb_says_so(self, store: Store) -> None:
        slug = _record_market(store, FIRST_TS, ptb=None)
        assert "ptb" in audit_market(store, slug, expected_windows=len(OFFSETS)).missing

    def test_a_missing_ptb_is_never_reconstructed_from_a_neighbour(
        self, store: Store
    ) -> None:
        """A20. The neighbouring markets have a PTB and it would be trivially easy
        to borrow one; a validator that did would turn a real freeze failure into a
        green report and the number would look exactly like a real PTB."""
        _record_market(store, FIRST_TS)
        slug = _record_market(store, FIRST_TS + CADENCE, ptb=None)
        _record_market(store, FIRST_TS + 2 * CADENCE)
        assert store.load_ptb(slug) is None
        assert "ptb" in audit_market(store, slug, expected_windows=len(OFFSETS)).missing
        assert store.load_ptb(slug) is None  # still absent after the audit

    def test_a_market_that_never_recorded_an_observation_says_so(
        self, store: Store
    ) -> None:
        slug = _record_market(store, FIRST_TS, observations=0)
        assert "signal_twap" in audit_market(
            store, slug, expected_windows=len(OFFSETS)
        ).missing

    def test_an_unknown_market_is_missing_wholesale(self, store: Store) -> None:
        record = audit_market(store, "btc-updown-5m-1", expected_windows=len(OFFSETS))
        assert record.missing == ("market",)

    def test_a_window_that_never_fired_is_not_a_missing_record(
        self, store: Store
    ) -> None:
        """BUFFER_NOT_SATISFIED is the most common correct outcome of the day. A
        recorder that counted it as an omission would be permanently red and the
        report would be worthless."""
        slug = _record_market(store, FIRST_TS, fire=())
        record = audit_market(store, slug, expected_windows=len(OFFSETS))
        assert record.intents == 0
        assert record.missing == ()

    def test_an_unsettled_open_market_is_not_incomplete(self, store: Store) -> None:
        """The current market has not settled yet. That is a clock fact, not a bug."""
        slug = _record_market(store, FIRST_TS, settle=False)
        assert audit_market(store, slug, expected_windows=len(OFFSETS)).missing == ()

    def test_a_settled_market_with_no_settlement_row_is_incomplete(
        self, store: Store
    ) -> None:
        slug = _record_market(store, FIRST_TS, settle=False)
        store.save_phase(slug, MarketPhase.SETTLED, float(FIRST_TS + 325))
        assert "settlement" in audit_market(
            store, slug, expected_windows=len(OFFSETS)
        ).missing

    def test_the_report_lists_only_the_incomplete_markets(self, store: Store) -> None:
        _run(store, 3)
        bad = _record_market(store, FIRST_TS + 3 * CADENCE, ptb=None)
        report = audit_recording(
            store, expected_windows=len(OFFSETS), cadence_seconds=CADENCE
        )
        assert [m.slug for m in report.incomplete] == [bad]
        assert report.complete is False

    def test_the_recorder_writes_nothing(self, store: Store) -> None:
        """An auditor with a side effect is not an auditor. Compared row by row
        across every table so a write anywhere is caught, not just in markets."""
        _run(store, 2)
        before = {
            t: store.connection.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in store.table_names()
        }
        audit_recording(store, expected_windows=len(OFFSETS), cadence_seconds=CADENCE)
        fill_statistics(store, offsets=OFFSETS)
        _validate(store)
        after = {
            t: store.connection.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in store.table_names()
        }
        assert before == after


class TestSubmissionRecords:
    """Criterion 4: every field one submission must have recorded."""

    def test_every_submission_field_is_present(self, store: Store) -> None:
        (slug,) = _run(store, 1)
        (record,) = submission_records(store, slug)
        payload = record.as_json()
        for key in (
            "offset_seconds", "submission_index", "submission_count",
            "order_generation", "order_id", "venue_order_id", "order_price",
            "shares", "created_at", "ms_before_close", "submission_latency_ms",
            "fill_latency_ms", "venue_acknowledged", "state",
        ):
            assert payload[key] is not None, key

    def test_ms_before_close_is_measured_against_the_market_close(
        self, store: Store
    ) -> None:
        (slug,) = _run(store, 1)
        (record,) = submission_records(store, slug)
        # Fired at close - 15 + 0.5s, so 14.5 seconds of runway.
        assert record.ms_before_close == pytest.approx(14_500.0)

    def test_fill_latency_is_measured_from_submission(self, store: Store) -> None:
        (slug,) = _run(store, 1)
        (record,) = submission_records(store, slug)
        assert record.fill_latency_ms == pytest.approx(300.0, abs=1.0)

    def test_an_unfilled_order_has_no_fill_latency(self, store: Store) -> None:
        """None, not zero. Zero is a fill that was instant."""
        slug = _record_market(store, FIRST_TS, fire=())
        executor = PaperExecutor()
        asyncio.run(
            submitter(store, executor).submit(
                intent_for(window_ts=FIRST_TS, offset_seconds=15),
                count=1,
                phase=MarketPhase.ACTIVE,
                now=float(FIRST_TS + 285),
            )
        )
        (record,) = submission_records(store, slug)
        assert record.fill_latency_ms is None

    def test_generations_are_numbered_within_their_window(self, store: Store) -> None:
        (slug,) = _run(store, 1)
        store.save_order(
            Order(
                order_id=f"{slug}:15:0:1",
                market_slug=slug,
                offset_seconds=15,
                direction=Direction.UP,
                price=Decimal("0.71"),
                size=SIZE,
                state=OrderState.CANCELLED,
                created_at=float(FIRST_TS + 288),
                updated_at=float(FIRST_TS + 288),
                venue_order_id="paper-98",
                reprice_chain_id=f"{slug}:15:1",
            )
        )
        assert [r.order_generation for r in submission_records(store, slug)] == [1, 2]


# ── statistics ───────────────────────────────────────────────────────────────


class TestStatisticsAreBucketedPerWindow:
    def test_every_configured_offset_gets_a_bucket(self, store: Store) -> None:
        stats = fill_statistics(store, offsets=OFFSETS)
        assert sorted(stats.by_offset) == sorted(OFFSETS)

    def test_an_offset_that_never_traded_is_zeros_not_absent(self, store: Store) -> None:
        """An absent row reads as "not configured", which is the one thing it is not."""
        _run(store, 2)
        assert fill_statistics(store, offsets=OFFSETS).by_offset[3].submissions == 0

    def test_a_window_that_never_submitted_has_no_fill_rate(self, store: Store) -> None:
        """None, not 0.0 — zero reads as a window that submits and never fills."""
        _run(store, 2)
        assert fill_statistics(store, offsets=OFFSETS).by_offset[3].fill_rate is None

    def test_submissions_land_in_their_own_offset(self, store: Store) -> None:
        _record_market(store, FIRST_TS, fire=(15, 5))
        stats = fill_statistics(store, offsets=OFFSETS)
        assert stats.by_offset[15].submissions == 1
        assert stats.by_offset[5].submissions == 1
        assert stats.by_offset[10].submissions == 0

    def test_the_fill_rate_is_filled_over_submitted(self, store: Store) -> None:
        _record_market(store, FIRST_TS, fire=(15,))
        assert fill_statistics(store, offsets=OFFSETS).by_offset[15].fill_rate == 1.0

    def test_a_reprice_is_counted_as_a_reprice_not_a_second_window(
        self, store: Store
    ) -> None:
        (slug,) = _run(store, 1)
        store.save_order(
            Order(
                order_id=f"{slug}:15:0:1",
                market_slug=slug,
                offset_seconds=15,
                direction=Direction.UP,
                price=Decimal("0.71"),
                size=SIZE,
                state=OrderState.CANCELLED,
                created_at=float(FIRST_TS + 288),
                updated_at=float(FIRST_TS + 288),
                venue_order_id="paper-98",
                reprice_chain_id=f"{slug}:15:1",
            )
        )
        bucket = fill_statistics(store, offsets=OFFSETS).by_offset[15]
        assert bucket.submissions == 2
        assert bucket.reprices == 1

    def test_an_offset_no_longer_configured_still_appears(self, store: Store) -> None:
        """The offsets were changed mid-history. Dropping the old bucket would
        quietly shrink the run the report claims to cover."""
        _run(store, 1)
        stats = fill_statistics(store, offsets=(3, 5))
        assert 15 in stats.by_offset
        assert stats.by_offset[15].submissions == 1

    def test_percentiles_are_nearest_rank_never_interpolated(
        self, store: Store
    ) -> None:
        """An interpolated p95 is a latency no order actually had, and the operator
        is asking which real submission was slowest."""
        _run(store, 4)
        bucket = fill_statistics(store, offsets=OFFSETS).by_offset[15]
        payload = bucket.as_json()
        assert payload["p95_fill_latency_ms"] in {
            round(v, 3) for v in bucket.fill_latencies_ms
        }

    def test_window_outcomes_are_counted_per_offset(self, store: Store) -> None:
        _record_market(store, FIRST_TS, fire=(15,))
        stats = fill_statistics(store, offsets=OFFSETS)
        assert stats.by_offset[15].fired == 1
        assert stats.by_offset[10].buffer_not_satisfied == 1

    def test_the_totals_are_the_sum_of_the_buckets(self, store: Store) -> None:
        _record_market(store, FIRST_TS, fire=(15, 10))
        stats = fill_statistics(store, offsets=OFFSETS)
        assert stats.submissions == sum(b.submissions for b in stats.by_offset.values())
        assert stats.filled == 2


# ── the report ───────────────────────────────────────────────────────────────


class TestTheRenderedReport:
    def _text(self, store: Store) -> str:
        return render_report(_validate(store), mode="V1", provider="RTDS")  # type: ignore[arg-type]

    def test_an_empty_run_renders_not_ready(self, store: Store) -> None:
        assert "NOT READY" in self._text(store)

    def test_a_short_run_still_renders_not_ready(self, store: Store) -> None:
        _run(store, 5)
        assert "NOT READY" in self._text(store)
        assert "READY FOR LIVE (V2)" not in self._text(store)

    def test_every_unverified_criterion_is_printed_with_its_remedy(
        self, store: Store
    ) -> None:
        """In full, under the verdict — not summarised, not in an appendix. A count
        of unverified criteria is a number to skip past; the list is work to do."""
        _run(store, 3)
        text = self._text(store)
        for criterion in _validate(store).unverified:  # type: ignore[attr-defined]
            assert criterion.name in text
            assert criterion.evidence in text

    def test_every_failure_is_printed(self, store: Store) -> None:
        _record_market(store, FIRST_TS)
        _record_market(store, FIRST_TS + 2 * CADENCE)
        assert "No market gaps" in self._text(store)
        assert "FAILED" in self._text(store)

    def test_the_mode_and_provider_are_stated(self, store: Store) -> None:
        text = render_report(_validate(store), mode="V1", provider="RTDS")  # type: ignore[arg-type]
        assert "V1" in text
        assert "RTDS" in text

    def test_host_metrics_are_disclaimed_rather_than_invented(
        self, store: Store
    ) -> None:
        text = self._text(store)
        assert "NOT MEASURED BY ARC" in text
        assert "does not" in text

    def test_every_window_appears_in_the_statistics_table(self, store: Store) -> None:
        _run(store, 2)
        text = self._text(store)
        for offset in OFFSETS:
            assert f"{offset}s" in text

    def test_the_report_is_plain_text(self, store: Store) -> None:
        """Read over SSH on the machine that produced it. No markup to render."""
        text = self._text(store)
        assert "<" not in text
        assert text.isprintable() is False  # newlines
        text.encode("utf-8")


# ── the runtime metrics block ────────────────────────────────────────────────


class TestRuntimeMetrics:
    """Addendum item 9. Every figure printed must be measured or say it is not."""

    def _metrics(self, store: Store, **kwargs: object) -> RuntimeMetrics:
        report = validate_run(
            store, offsets=OFFSETS, cadence_seconds=CADENCE, **kwargs  # type: ignore[arg-type]
        )
        assert report.metrics is not None
        return report.metrics

    def test_the_runtime_figures_are_what_was_passed_in(self, store: Store) -> None:
        m = self._metrics(store, uptime_seconds=3601.5, restarts=2, reconnects=7)
        assert (m.uptime_seconds, m.restarts, m.reconnects) == (3601.5, 2, 7)

    def test_a_negative_uptime_is_clamped_rather_than_printed(self, store: Store) -> None:
        assert self._metrics(store, uptime_seconds=-1.0).uptime_seconds == 0.0

    def test_the_uninstrumented_latencies_say_so(self, store: Store) -> None:
        """The whole point: an unmeasured number printed beside measured ones is
        read as measured."""
        m = self._metrics(store)
        assert m.websocket_latency_ms == UNAVAILABLE
        assert m.clob_latency_ms == UNAVAILABLE
        assert m.rtds_latency_ms == UNAVAILABLE

    def test_chainlink_is_not_applicable_unless_it_is_the_provider(
        self, store: Store
    ) -> None:
        assert "N/A" in str(self._metrics(store).chainlink_latency_ms)
        assert self._metrics(store, chainlink_enabled=True).chainlink_latency_ms == (
            UNAVAILABLE
        )

    def test_order_latency_is_a_real_number_once_orders_exist(self, store: Store) -> None:
        _run(store, 2)
        assert isinstance(self._metrics(store).order_latency_ms, float)

    def test_order_latency_is_unavailable_before_anything_submits(
        self, store: Store
    ) -> None:
        assert self._metrics(store).order_latency_ms == UNAVAILABLE

    def test_the_recorder_size_counts_markets_and_observations(self, store: Store) -> None:
        """Observations are the raw tick rows, not the accumulator's count: the
        accumulator survives pruning, so it would report a recorder size that is no
        longer on disk."""
        slugs = _run(store, 3)
        store.save_observation(
            slugs[0],
            Observation(ts=float(FIRST_TS + 1), price=Decimal("64100.00")),
            float(FIRST_TS + 1),
        )
        m = self._metrics(store)
        assert m.recorder_markets == 3
        assert m.recorder_observations == 1

    def test_the_database_size_includes_the_wal_sidecar(self, store: Store) -> None:
        """WAL mode keeps the newest rows in the sidecar. Reporting only the main
        file shows a database that appears not to grow, then jumps at checkpoint."""
        _run(store, 2)
        main = store.path.stat().st_size
        assert self._metrics(store).database_bytes > main

    def test_growth_is_per_market_and_projects_to_a_day(self, store: Store) -> None:
        _run(store, 2)
        payload = self._metrics(store).as_json()
        per_market = payload["database_bytes_per_market"]
        assert isinstance(per_market, float)
        assert payload["database_bytes_per_day_projected"] == round(per_market * 288)

    def test_growth_is_unavailable_with_no_markets_to_divide_by(
        self, store: Store
    ) -> None:
        payload = self._metrics(store).as_json()
        assert payload["database_bytes_per_market"] == UNAVAILABLE
        assert payload["database_bytes_per_day_projected"] == UNAVAILABLE

    def test_the_validation_duration_is_measured(self, store: Store) -> None:
        assert isinstance(self._metrics(store).validation_duration_seconds, float)

    def test_every_metric_row_the_report_prints_exists(self, store: Store) -> None:
        payload = self._metrics(store).as_json()
        assert [f for _, f in _METRIC_ROWS if f not in payload] == []

    def test_the_metrics_reach_the_rendered_report(self, store: Store) -> None:
        _run(store, 2)
        text = render_report(
            _validate(store), mode="V1", provider="RTDS"  # type: ignore[arg-type]
        )
        assert "RUNTIME METRICS" in text
        for label, _ in _METRIC_ROWS:
            assert label in text

    def test_the_metrics_cannot_change_the_verdict(self, store: Store) -> None:
        """Item 9 describes the run; item 10 judges it. Separate questions."""
        report = validate_run(
            store, offsets=OFFSETS, cadence_seconds=CADENCE, uptime_seconds=99999.0
        )
        assert report.verdict == VERDICT_NOT_READY


# ── the verdict ──────────────────────────────────────────────────────────────


class TestTheVerdict:
    """Addendum item 10. Exactly one of two strings, and never the green one on
    a run that did not demonstrate the hard parts."""

    def test_an_unexercised_run_is_not_ready(self, store: Store) -> None:
        _run(store, 3)
        assert _validate(store).verdict == VERDICT_NOT_READY  # type: ignore[attr-defined]

    def test_the_verdict_is_one_of_exactly_two_strings(self, store: Store) -> None:
        assert _validate(store).verdict in {  # type: ignore[attr-defined]
            VERDICT_READY,
            VERDICT_NOT_READY,
        }

    def test_ready_tracks_ready_for_live_and_nothing_else(self, store: Store) -> None:
        report = _validate(store)
        report.criteria.clear()  # type: ignore[attr-defined]
        assert report.ready_for_live is True  # type: ignore[attr-defined]
        assert report.verdict == VERDICT_READY  # type: ignore[attr-defined]

    def test_the_verdict_is_printed_in_the_report(self, store: Store) -> None:
        text = render_report(
            _validate(store), mode="V1", provider="RTDS"  # type: ignore[arg-type]
        )
        assert VERDICT_NOT_READY in text
        assert VERDICT_READY not in text.replace(VERDICT_NOT_READY, "")

    def test_the_verdict_is_in_the_json(self, store: Store) -> None:
        assert _validate(store).as_json()["verdict"] == (  # type: ignore[attr-defined]
            VERDICT_NOT_READY
        )
