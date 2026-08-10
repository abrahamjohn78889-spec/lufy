"""post_only_would_cross: recorded, never acted on.

The venue refuses a passive order that would have taken liquidity. The order is
dead; the DECISION is not, and nothing in the execution layer is allowed to
rescue the submission by changing it. That is the whole contract, and almost
every test here is negative: the price did not move, the buffer did not move,
the PTB did not move, the direction did not move, the trigger did not move, no
second intent appeared, no second order was placed.

The one positive requirement is that the failure is legible — a dedicated
reason code in the database rather than the venue's prose, its own log event,
and its own operator-facing wording — because a rejection that is invisible is
indistinguishable from an order that was never attempted.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest
from execution_fixtures import (
    LIMIT_PRICE,
    bucket,
    intent_for,
    make_market,
    store_at,
    submitter,
)

from arc.domain.enums import (
    POST_ONLY_WOULD_CROSS_REASON,
    REJECTION_REASON_DISPLAY,
    Direction,
    MarketPhase,
    OrderState,
)
from arc.domain.models import ExecutionIntent, Order
from arc.errors import ArcError, ConnectionLostError, PostOnlyWouldCrossError
from arc.execution.orders import LEGAL_ORDER_TRANSITIONS
from arc.execution.reprice import RepricePolicy, Repricer
from arc.execution.retry import Disposition, classify, rejection_reason
from arc.execution.v1_paper import PaperExecutor
from arc.storage.store import Store

NOW = 1754400297.0


class CrossingVenue(PaperExecutor):
    """The paper adapter, except that the venue refuses every post-only order.

    Subclassed rather than mocked so everything the submission path does around
    the failing call — the write-before-act row, the FSM transition, the
    persistence — is the real production code.
    """

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[Order] = []

    async def place(self, order: Order) -> str:
        self.attempts.append(order)
        raise PostOnlyWouldCrossError(
            f"post-only order {order.order_id} would have crossed at {order.price}: "
            "would immediately match the best ask"
        )


def _submit(store: Store, venue: PaperExecutor, intent: ExecutionIntent) -> tuple[Order, ...]:
    return asyncio.run(
        submitter(store, venue).submit(
            intent, count=1, phase=MarketPhase.ACTIVE, now=NOW
        )
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    store = store_at(tmp_path)
    make_market(store)
    return store


class TestItIsTerminalAndNeverRetried:
    def test_the_venue_is_called_exactly_once(self, store: Store) -> None:
        """The failure that this forbids: a retry loop that eventually gets filled
        at a price the risk gates never approved."""
        venue = CrossingVenue()
        _submit(store, venue, intent_for())
        assert len(venue.attempts) == 1

    def test_it_classifies_as_a_definite_failure(self) -> None:
        assert classify(PostOnlyWouldCrossError("x")) is Disposition.FAIL

    def test_it_is_not_treated_as_a_retryable_transient(self) -> None:
        assert classify(PostOnlyWouldCrossError("x")) is not Disposition.RETRY_NOW

    def test_it_is_not_treated_as_an_unknown_outcome(self) -> None:
        """A definite refusal, unlike a dropped connection: the venue answered.
        Marking it INDETERMINATE would leave reconciliation hunting an order that
        was never accepted."""
        assert classify(PostOnlyWouldCrossError("x")) is not Disposition.INDETERMINATE

    def test_the_order_reaches_a_terminal_state(self, store: Store) -> None:
        (order,) = _submit(store, CrossingVenue(), intent_for())
        assert order.state is OrderState.REJECTED

    def test_no_replacement_order_is_created(self, store: Store) -> None:
        """No successor generation, no second index — one submission, one row."""
        _submit(store, CrossingVenue(), intent_for())
        rows = store.orders_for(intent_for().market_slug)
        assert len(rows) == 1
        assert rows[0].order_id.endswith(":0")


class TestTheDecisionIsUntouched:
    """Execution failure must never rewrite or reinterpret strategy output."""

    def test_every_frozen_field_survives_the_rejection(self, store: Store) -> None:
        intent = intent_for()
        snapshot = (
            intent.limit_price,
            intent.buffer,
            intent.ptb,
            intent.signal_twap,
            intent.opening_twap,
            intent.locked_trigger,
            intent.direction,
            intent.size,
        )
        _submit(store, CrossingVenue(), intent)
        assert (
            intent.limit_price,
            intent.buffer,
            intent.ptb,
            intent.signal_twap,
            intent.opening_twap,
            intent.locked_trigger,
            intent.direction,
            intent.size,
        ) == snapshot

    def test_the_intent_is_immutable_by_construction(self) -> None:
        """Not merely unmodified in practice — unmodifiable. The strongest form of
        this guarantee is the one the type system enforces."""
        intent = intent_for()
        with pytest.raises(FrozenInstanceError):
            intent.limit_price = Decimal("0.99")  # type: ignore[misc]

    def test_the_rejected_order_still_carries_the_approved_price(
        self, store: Store
    ) -> None:
        """The row records what was ATTEMPTED. A row showing a different price
        would be evidence of a reprice that must not have happened."""
        (order,) = _submit(store, CrossingVenue(), intent_for())
        assert order.price == LIMIT_PRICE
        assert store.orders_for(order.market_slug)[0].price == LIMIT_PRICE

    def test_the_direction_is_not_flipped(self, store: Store) -> None:
        (order,) = _submit(store, CrossingVenue(), intent_for(direction=Direction.DOWN))
        assert order.direction is Direction.DOWN

    def test_the_size_is_not_reduced(self, store: Store) -> None:
        """A smaller order might not cross. It is still not this layer's call."""
        (order,) = _submit(store, CrossingVenue(), intent_for(size=Decimal("35")))
        assert order.size == Decimal("35")

    def test_no_second_intent_is_recorded(self, store: Store) -> None:
        intent = intent_for()
        _submit(store, CrossingVenue(), intent)
        assert not store.has_intent(intent.market_slug, intent.offset_seconds)


class TestItIsRecorded:
    def test_the_dedicated_reason_is_persisted(self, store: Store) -> None:
        """Matched exactly by the dashboard, so it cannot be broken by the venue
        rewording its own message."""
        (order,) = _submit(store, CrossingVenue(), intent_for())
        stored = store.orders_for(order.market_slug)[0]
        assert stored.rejection_reason == POST_ONLY_WOULD_CROSS_REASON

    def test_it_survives_a_restart(self, tmp_path: Path) -> None:
        """The operator reads this after the fact, which is always after some
        restart or other."""
        store = store_at(tmp_path)
        make_market(store)
        (order,) = _submit(store, CrossingVenue(), intent_for())
        store.close()

        reopened = store_at(tmp_path)
        stored = reopened.orders_for(order.market_slug)[0]
        assert stored.state is OrderState.REJECTED
        assert stored.rejection_reason == POST_ONLY_WOULD_CROSS_REASON
        reopened.close()

    def test_the_reason_mapper_returns_the_code_not_the_prose(self) -> None:
        exc = PostOnlyWouldCrossError("would immediately match the best ask")
        assert rejection_reason(exc) == POST_ONLY_WOULD_CROSS_REASON

    def test_every_other_failure_keeps_its_own_message(self) -> None:
        """The dedicated code is for the one condition with a defined policy.
        Substituting a code for the venue's text elsewhere would throw away the
        only detail an operator has about a one-off failure."""
        assert rejection_reason(ArcError("insufficient balance")) == "insufficient balance"
        assert rejection_reason(ConnectionLostError("socket closed")) == "socket closed"

    def test_it_has_its_own_log_event(self, store: Store, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            _submit(store, CrossingVenue(), intent_for())
        assert any("Post-Only Would Cross" in r.getMessage() for r in caplog.records)

    def test_the_log_line_carries_the_order_and_the_price(
        self, store: Store, caplog
    ) -> None:
        with caplog.at_level(logging.WARNING):
            (order,) = _submit(store, CrossingVenue(), intent_for())
        detail = next(
            str(getattr(r, "arc_detail", ""))
            for r in caplog.records
            if "Post-Only" in r.getMessage()
        )
        assert order.order_id in detail
        assert "0.70" in detail

    def test_the_dashboard_has_wording_for_it(self) -> None:
        """A13: the operator sees why, not a raw enum name."""
        assert POST_ONLY_WOULD_CROSS_REASON in REJECTION_REASON_DISPLAY
        assert REJECTION_REASON_DISPLAY[POST_ONLY_WOULD_CROSS_REASON]

    def test_the_wording_says_it_was_not_repriced(self) -> None:
        """The operator's first question on seeing this is "did the bot retry at a
        worse price?". The label answers it without them having to read the code."""
        assert "not repriced" in REJECTION_REASON_DISPLAY[POST_ONLY_WOULD_CROSS_REASON].lower()


class TestTheLifecycleContinues:
    def test_the_remaining_ladder_is_still_submitted(self, tmp_path: Path) -> None:
        """One split crossing does not abandon the others. They are independent
        orders at the same approved price; the venue refused one of them."""
        store = store_at(tmp_path)
        make_market(store)

        class OneCrossing(PaperExecutor):
            def __init__(self) -> None:
                super().__init__()
                self.seen = 0

            async def place(self, order: Order) -> str:
                self.seen += 1
                if self.seen == 1:
                    raise PostOnlyWouldCrossError("would cross")
                return await super().place(order)

        venue = OneCrossing()
        orders = asyncio.run(
            submitter(store, venue).submit(
                intent_for(size=Decimal("30")),
                count=3,
                phase=MarketPhase.ACTIVE,
                now=NOW,
            )
        )
        assert len(orders) == 3
        assert orders[0].state is OrderState.REJECTED
        assert [o.state for o in orders[1:]] == [OrderState.SUBMITTED] * 2
        store.close()

    def test_a_rejected_submission_does_not_raise_out_of_submit(
        self, store: Store
    ) -> None:
        """It is an outcome, not an exception. Letting it propagate would take the
        market's whole processing pass down with it and stop the monitoring the
        contract requires to continue."""
        orders = _submit(store, CrossingVenue(), intent_for())
        assert len(orders) == 1

    def test_the_market_is_still_usable_afterwards(self, store: Store) -> None:
        """A later window trades normally. The rejection is scoped to its own
        submission and does not poison the market instance."""
        _submit(store, CrossingVenue(), intent_for(offset_seconds=3))
        (later,) = _submit(store, PaperExecutor(), intent_for(offset_seconds=5))
        assert later.state is OrderState.SUBMITTED


class TestNoRepricingPolicyExists:
    """Until a specification says otherwise, the code must not contain one."""

    def test_the_repricer_never_sees_a_rejected_order(self, tmp_path: Path) -> None:
        """maybe_reprice acts only on live orders, so a crossed submission cannot
        be swept up into the reprice path by a caller that loops over the market."""
        store = store_at(tmp_path)
        make_market(store)
        (order,) = _submit(store, CrossingVenue(), intent_for())

        repricer = Repricer(
            store,
            PaperExecutor(),
            RepricePolicy(
                band_min=Decimal("0.55"), band_max=Decimal("0.85"), tick=Decimal("0.01")
            ),
            bucket=bucket(),
        )
        result = asyncio.run(repricer.maybe_reprice(order, NOW))
        assert result is order
        assert result.state is OrderState.REJECTED
        store.close()

    def test_rejected_is_a_dead_end_in_the_state_machine(self) -> None:
        """No transition out of REJECTED exists, so no future code path can revive
        a crossed order without changing the FSM deliberately."""
        assert LEGAL_ORDER_TRANSITIONS[OrderState.REJECTED] == ()
