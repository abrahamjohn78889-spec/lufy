"""The ExecutionIntent: immutable, self-sufficient, and deterministically named.

The contract under test is that execution can act on an intent alone. Anything
execution would have to look up elsewhere is a value that could have moved between
the decision and the submission.
"""

from __future__ import annotations

import dataclasses
from dataclasses import fields
from decimal import Decimal

import pytest
from decision_fixtures import BASE_PTB, fired_market

from arc.decision.intent import build_intent, intent_id_for
from arc.decision.snapshot import DecisionSnapshot, snapshot_for
from arc.domain.enums import Direction, WindowState
from arc.domain.models import ExecutionIntent
from arc.storage.store import Store
from arc.strategy.protocol import StrategyDecision

DECISION = StrategyDecision(act=True, limit_price=Decimal("0.70"), size=Decimal("35"))


def _snapshot(*, direction: Direction = Direction.UP, offset: int = 3) -> DecisionSnapshot:
    market = fired_market(direction=direction, fired=(offset,))
    snapshot = snapshot_for(market, market.window(offset))
    assert snapshot is not None
    return snapshot


def _intent(*, direction: Direction = Direction.UP, offset: int = 3) -> ExecutionIntent:
    return build_intent(
        _snapshot(direction=direction, offset=offset),
        DECISION,
        strategy_id="arc_twap_locked_buffer",
        created_at=1754400001.5,
    )


class TestImmutability:
    def test_the_intent_is_frozen(self) -> None:
        """A mutable intent could be repriced in place, and the persisted row would
        then disagree with the object execution actually submitted."""
        intent = _intent()
        with pytest.raises(dataclasses.FrozenInstanceError):
            intent.limit_price = Decimal("0.99")  # type: ignore[misc]

    def test_no_field_is_a_mutable_container(self) -> None:
        """A list or dict field would be mutable through the frozen wrapper."""
        for field in fields(ExecutionIntent):
            value = getattr(_intent(), field.name)
            assert not isinstance(value, list | dict | set), field.name

    def test_it_carries_no_reference_to_the_market_or_the_window(self) -> None:
        """A11: the MarketInstance is dropped at close. A reference held here would
        keep a closed market alive and let execution read a TWAP that has moved on."""
        names = {f.name for f in fields(ExecutionIntent)}
        assert names.isdisjoint({"market", "window", "store", "snapshot", "engine"})

    def test_every_field_is_a_plain_immutable_value(self) -> None:
        for field in fields(ExecutionIntent):
            value = getattr(_intent(), field.name)
            assert isinstance(value, str | int | float | Decimal | Direction), field.name


class TestSelfSufficiency:
    def test_it_carries_everything_execution_needs_to_place_the_order(self) -> None:
        intent = _intent()
        assert intent.market_slug
        assert intent.direction is Direction.UP
        assert intent.limit_price > 0
        assert intent.size > 0
        assert intent.close_ts == 1754400000 + 300

    def test_it_carries_the_frozen_window_values_verbatim(self) -> None:
        """Not so execution can re-derive anything — it must not — but so the record
        of a submission states the exact decision inputs without a second lookup."""
        snapshot = _snapshot()
        intent = build_intent(
            snapshot, DECISION, strategy_id="arc_twap_locked_buffer", created_at=0.0
        )
        assert intent.opening_twap == snapshot.opening_twap
        assert intent.ptb == snapshot.ptb == BASE_PTB
        assert intent.buffer == snapshot.buffer
        assert intent.locked_trigger == snapshot.locked_trigger
        assert intent.signal_twap == snapshot.signal_twap

    def test_it_names_the_strategy_that_produced_it(self) -> None:
        assert _intent().strategy_id == "arc_twap_locked_buffer"

    def test_the_close_ts_is_the_markets_own_close(self) -> None:
        assert _intent().close_ts == _snapshot().close_ts


class TestNothingIsRecomputed:
    def test_the_direction_is_copied_not_rederived_from_the_ptb(self) -> None:
        """A12: direction is frozen once at freeze time. Re-deriving it here from
        opening_twap >= ptb would give the right answer today and a different answer
        the moment either value is corrected."""
        down = _intent(direction=Direction.DOWN)
        assert down.direction is Direction.DOWN
        assert down.opening_twap < down.ptb
        up = _intent(direction=Direction.UP)
        assert up.direction is Direction.UP
        assert up.opening_twap >= up.ptb

    def test_the_trigger_is_copied_not_rebuilt_from_the_buffer(self) -> None:
        snapshot = _snapshot()
        intent = build_intent(
            snapshot, DECISION, strategy_id="s", created_at=0.0
        )
        assert intent.locked_trigger == snapshot.locked_trigger

    def test_the_intent_module_contains_no_arithmetic_on_prices(self) -> None:
        """The guard behind the two tests above: if the module cannot do arithmetic
        it cannot re-derive a trigger, however the code is later edited."""
        import ast
        from pathlib import Path

        import arc.decision.intent as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        operators = [n for n in ast.walk(tree) if isinstance(n, ast.BinOp)]
        assert operators == []


class TestTheIdIsDeterministic:
    def test_the_id_is_the_slug_and_the_offset(self) -> None:
        assert intent_id_for("btc-updown-5m-1754400000", 3) == "btc-updown-5m-1754400000:3"

    def test_two_builds_of_the_same_window_share_an_id(self) -> None:
        """Not uuid4, not a counter, not a clock reading. A fresh id on a retry after
        a crash would defeat the UNIQUE constraint and submit the window twice."""
        assert _intent().intent_id == _intent().intent_id

    def test_different_windows_of_one_market_get_different_ids(self) -> None:
        assert _intent(offset=3).intent_id != _intent(offset=5).intent_id

    def test_the_id_matches_the_uniqueness_key_the_database_enforces(self) -> None:
        intent = _intent()
        assert intent.intent_id == f"{intent.market_slug}:{intent.offset_seconds}"


class TestSerialization:
    def test_it_is_byte_identical_for_identical_inputs(self) -> None:
        assert _intent().serialize() == _intent().serialize()

    def test_it_excludes_created_at(self) -> None:
        """A wall-clock reading. Including it would make the determinism assertion a
        statement about the clock rather than about the decision."""
        early = build_intent(_snapshot(), DECISION, strategy_id="s", created_at=1.0)
        late = build_intent(_snapshot(), DECISION, strategy_id="s", created_at=999999.0)
        assert early.serialize() == late.serialize()
        assert early.created_at != late.created_at

    def test_it_compares_the_printed_form_of_every_money_field(self) -> None:
        """Decimal("0.80") == Decimal("0.8") as objects, but a venue receives the
        text. Two intents that print differently must not serialize identically."""
        snapshot = _snapshot()
        eighty = StrategyDecision(act=True, limit_price=Decimal("0.80"), size=Decimal("10"))
        eight = StrategyDecision(act=True, limit_price=Decimal("0.8"), size=Decimal("10"))
        assert eighty.limit_price == eight.limit_price
        first = build_intent(snapshot, eighty, strategy_id="s", created_at=0.0)
        second = build_intent(snapshot, eight, strategy_id="s", created_at=0.0)
        assert first.serialize() != second.serialize()

    def test_a_change_to_any_serialized_field_changes_the_string(self) -> None:
        base = _intent()
        for field in fields(ExecutionIntent):
            if field.name == "created_at":
                continue
            current = getattr(base, field.name)
            if isinstance(current, Decimal):
                changed = current + Decimal("1")
            elif isinstance(current, Direction):
                changed = Direction.DOWN if current is Direction.UP else Direction.UP
            elif isinstance(current, int):
                changed = current + 1
            else:
                changed = f"{current}x"
            other = dataclasses.replace(base, **{field.name: changed})
            assert other.serialize() != base.serialize(), field.name

    def test_the_direction_is_serialized_by_value_not_by_repr(self) -> None:
        """`repr(Direction.UP)` embeds the enum class path, which would change with a
        module rename and break a stored comparison for no trading reason."""
        assert "direction=UP" in _intent().serialize()
        assert "Direction." not in _intent().serialize()


@pytest.fixture
def market_store(store: Store) -> Store:
    """A store whose market row exists.

    The intents table has a foreign key onto markets(slug). Inserting the parent
    row through the ordinary create_market path rather than disabling the constraint
    keeps the round-trip test honest about what production writes.
    """
    store.create_market(fired_market(), 0.0)
    return store


class TestPersistenceRoundTrip:
    def test_a_stored_intent_reloads_verbatim(self, market_store: Store) -> None:
        """A4: the frozen values must reload verbatim, so a restart between the
        decision and the submission submits the same order."""
        intent = _intent()
        assert market_store.save_intent(intent)
        (reloaded,) = market_store.intents_for(intent.market_slug)
        assert reloaded.serialize() == intent.serialize()

    def test_a_second_save_of_the_same_window_is_refused(self, market_store: Store) -> None:
        """A12: exactly one intent per window, arbitrated by SQLite rather than by an
        in-memory set, so the constraint survives a crash."""
        intent = _intent()
        assert market_store.save_intent(intent)
        assert not market_store.save_intent(intent)
        assert len(market_store.intents_for(intent.market_slug)) == 1

    def test_two_windows_of_one_market_both_persist(self, market_store: Store) -> None:
        assert market_store.save_intent(_intent(offset=3))
        assert market_store.save_intent(_intent(offset=5))
        assert len(market_store.intents_for(_intent().market_slug)) == 2

    def test_a_reloaded_intent_is_still_frozen(self, market_store: Store) -> None:
        intent = _intent()
        market_store.save_intent(intent)
        (reloaded,) = market_store.intents_for(intent.market_slug)
        with pytest.raises(dataclasses.FrozenInstanceError):
            reloaded.size = Decimal("1")  # type: ignore[misc]


class TestTheSnapshotItIsBuiltFrom:
    def test_the_snapshot_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            _snapshot().signal_twap = Decimal("1")  # type: ignore[misc]

    def test_no_snapshot_exists_for_an_unfrozen_window(self) -> None:
        """So no downstream code has an `if value is None` branch in which a missing
        trigger could default to zero and fire immediately."""
        market = fired_market(offsets=(3,), fired=())
        market.window(3).state = WindowState.PENDING
        market.window(3).locked_trigger = None
        assert snapshot_for(market, market.window(3)) is None

    def test_every_snapshot_field_is_populated(self) -> None:
        snapshot = _snapshot()
        for field in fields(DecisionSnapshot):
            assert getattr(snapshot, field.name) is not None, field.name

    def test_the_snapshot_records_the_state_it_read(self) -> None:
        assert _snapshot().state is WindowState.FIRED
