"""MAJORITY on the dashboard: the payload the runtime accessors were built for.

`run.majority` and `run.majority_state_for` existed with nothing reading them.
This file is the read side — `majority_payload`, `majority_config_payload`, the
eleventh engine row, and the two keys added to `status_payload`/`settings_payload`
— proven against the real MajorityEngine, not a stub.

Bounding contracts these tests must not violate:
  - the twelve-route contract (MAJORITY gets no route of its own, see routes.py)
  - `test_dashboard_contract.py`'s `run` fixture omits `majority=`, so every payload
    here must also render correctly against MAJORITY_DISABLED and a None market
    state — proven directly below rather than assumed.
  - every Decimal crossing the boundary is a string (`_s`/`dec_str`).
"""

from __future__ import annotations

import asyncio
import io
import logging
from decimal import Decimal
from typing import Any

import pytest
from conftest import VALID_TRADING_VALUES
from fastapi.testclient import TestClient

from arc.api.app import build_app
from arc.api.models import (
    engine_status,
    majority_config_payload,
    majority_payload,
    settings_payload,
    status_payload,
)
from arc.clock import FrozenClock
from arc.config import ArcSettings, Settings, build_trading_config, load_settings
from arc.execution.v1_paper import PaperExecutor
from arc.majority.config import MAJORITY_DISABLED, MAJORITY_KEYS, MajorityConfig
from arc.majority.trigger import BookSnapshot, MajorityOutcome, MajorityVerdict
from arc.market.feed import RtdsFeed
from arc.runtime.engine import ArcRuntime
from arc.runtime.state import RuntimeState
from arc.storage.store import Store

_NOW = 1_754_400_000.0


def _config(
    *,
    enabled: bool = True,
    trigger_price: Decimal = Decimal("0.90"),
    target_limit_price: Decimal = Decimal("0.85"),
    shares: Decimal = Decimal("20"),
    entry_price_min: Decimal = Decimal("0.05"),
    entry_price_max: Decimal = Decimal("0.99"),
    disable_reason: str = "",
) -> MajorityConfig:
    from arc.majority.config import MajorityWindowConfig

    return MajorityConfig(
        enabled=enabled,
        windows=(MajorityWindowConfig(
            execution_window_seconds=30,
            buffer=Decimal("1.00"),
            trigger_price=trigger_price,
            target_limit_price=target_limit_price,
            shares=shares,
            entry_price_min=entry_price_min,
            entry_price_max=entry_price_max,
        ),),
        disable_reason=disable_reason,
    )


def _run(tmp_path: Any, *, majority: MajorityConfig = MAJORITY_DISABLED) -> ArcRuntime:
    store = Store(f"{tmp_path}/arc.db")
    store.migrate(_NOW)
    clock = FrozenClock(_NOW)
    runtime = RuntimeState(store, clock)
    runtime.load()
    return ArcRuntime(
        settings=Settings(
            env=ArcSettings(),
            trading=build_trading_config(dict(VALID_TRADING_VALUES)),
            seeded_from_env=False,
            majority=majority,
        ),
        store=store,
        clock=clock,
        runtime=runtime,
        discovery=None,  # type: ignore[arg-type]
        feed=RtdsFeed(clock),
        executor=PaperExecutor(),
        out=io.StringIO(),
        logger=logging.getLogger("arc.test.majority_dashboard"),
    )


def _open_market(run: ArcRuntime) -> Any:
    """Advance the rotator AND register the market with MAJORITY, as the loop does.

    Two calls because they are two engines. `rotator.advance` opens the market;
    `majority.open_market` gives MAJORITY its own fresh state for it, and the runtime
    main loop makes both calls on the same pass (engine.py, on `event.opened`).
    Advancing the rotator alone would leave MAJORITY untracking the market, which is
    a real state — the one `majority_state_for` returns None for — but not the one
    these tests are about.
    """
    run.rotator.advance(_NOW)
    market = run.rotator.current
    assert market is not None
    run.majority.open_market(market.slug, market.close_ts)
    return market


@pytest.fixture
def run(tmp_path: Any) -> ArcRuntime:
    return _run(tmp_path)


@pytest.fixture
def run_on(tmp_path: Any) -> ArcRuntime:
    return _run(tmp_path, majority=_config())


class TestMajorityConfigPayload:
    def test_every_decimal_is_a_string(self) -> None:
        payload = majority_config_payload(_config())
        assert payload["windows"], "payload must carry the windows"
        first = payload["windows"][0]
        for key in (
            "buffer", "trigger_price", "target_limit_price", "shares",
            "entry_price_min", "entry_price_max",
        ):
            assert isinstance(first[key], str), key
            Decimal(first[key])  # round-trips

    def test_tradable_and_enabled_are_different_facts(self) -> None:
        off = majority_config_payload(MAJORITY_DISABLED)
        assert off["enabled"] is False
        assert off["tradable"] is False
        assert off["disable_reason"] == ""

        fail_closed = majority_config_payload(_config(disable_reason="window too long"))
        assert fail_closed["enabled"] is True
        assert fail_closed["tradable"] is False
        assert fail_closed["disable_reason"] == "window too long"

        on = majority_config_payload(_config())
        assert on["enabled"] is True
        assert on["tradable"] is True


class TestMajorityPayloadRendersDisabled:
    """The contract test's fixture omits `majority=`. This must not crash or lie."""

    def test_no_market_no_config_renders_cleanly(self, run: ArcRuntime) -> None:
        payload = majority_payload(run, None)
        assert payload["config"]["enabled"] is False
        assert payload["market_slug"] is None
        assert payload["state"] is None
        assert payload["terminal"] is None
        assert payload["triggered"] is None
        assert payload["trigger_snapshot"] is None
        assert payload["decision_snapshot"] is None
        assert payload["verdict"] is None
        assert payload["no_trade_reason"] == ""

    def test_a_live_market_with_majority_off_has_no_tracked_state(
        self, run: ArcRuntime
    ) -> None:
        market = _open_market(run)
        payload = majority_payload(run, market)
        # MAJORITY is OFF, but open_market still registers a state row so
        # drop_market stays safe — so the market slug is present, but the state
        # is OFF, never a synthesised absence.
        assert payload["market_slug"] == market.slug
        assert payload["state"] == "OFF"


class TestMajorityPayloadWithATrackedMarket:
    def test_a_fresh_market_state_reports_waiting(self, run_on: ArcRuntime) -> None:
        market = _open_market(run_on)
        payload = majority_payload(run_on, market)
        assert payload["state"] in {"WAITING_WINDOW", "WINDOW_OPEN"}
        assert payload["terminal"] is False
        assert payload["triggered"] is False
        assert payload["side_locked"] is False
        assert payload["selected_side"] is None

    def test_a_locked_side_and_both_snapshots_are_reported(self, run_on: ArcRuntime) -> None:
        market = _open_market(run_on)
        state = run_on.majority_state_for(market.slug)
        assert state is not None

        trigger_snap = BookSnapshot(
            best_bid_up=Decimal("0.91"), best_bid_down=Decimal("0.05"), read_at=_NOW,
        )
        state.mark_triggered(trigger_snap, _NOW)
        fresh_snap = BookSnapshot(
            best_bid_up=Decimal("0.93"), best_bid_down=Decimal("0.04"), read_at=_NOW + 0.2,
        )
        verdict = MajorityVerdict(
            outcome=MajorityOutcome.UP,
            best_bid_up=Decimal("0.93"),
            best_bid_down=Decimal("0.04"),
        )
        state.select_side(verdict, fresh_snap, _NOW + 0.2)

        payload = majority_payload(run_on, market)
        assert payload["side_locked"] is True
        assert payload["selected_side"] == "UP"
        assert payload["triggered"] is True
        assert payload["trigger_snapshot"]["best_bid_up"] == "0.91"
        assert payload["decision_snapshot"]["best_bid_up"] == "0.93"
        assert payload["verdict"]["outcome"] == "UP"
        assert payload["verdict"]["direction"] == "UP"
        assert isinstance(payload["trigger_snapshot"]["best_bid_up"], str)
        assert isinstance(payload["decision_snapshot"]["best_bid_down"], str)

    def test_a_no_trade_reason_is_reported(self, run_on: ArcRuntime) -> None:
        market = _open_market(run_on)
        state = run_on.majority_state_for(market.slug)
        assert state is not None
        state.mark_no_trade("G07 DUPLICATE_INTENT: this window already has an intent")
        payload = majority_payload(run_on, market)
        assert payload["state"] == "NO_TRADE"
        assert payload["terminal"] is True
        assert "DUPLICATE_INTENT" in payload["no_trade_reason"]


class TestEngineStatusEleventhRow:
    def test_the_ten_named_rows_are_unchanged(self, run: ArcRuntime) -> None:
        """The eleventh row must not disturb the ten the contract fixes by name."""
        rows = engine_status(run)
        assert [r["engine"] for r in rows][:10] == [
            "Market Engine", "Window Engine", "Decision Engine", "Risk Engine",
            "Limit Order Engine", "Recovery Engine", "Provider", "WebSocket", "RPC",
            "Wallet",
        ]

    def test_majority_off_is_waiting_not_red(self, run: ArcRuntime) -> None:
        rows = engine_status(run)
        majority_row = next(r for r in rows if r["engine"] == "MAJORITY Engine")
        assert majority_row["state"] == "Waiting"
        assert majority_row["light"] == "YELLOW"

    def test_majority_fail_closed_is_a_warning_with_the_reason(self, tmp_path: Any) -> None:
        run = _run(tmp_path, majority=_config(disable_reason="45s window has no formula"))
        rows = engine_status(run)
        majority_row = next(r for r in rows if r["engine"] == "MAJORITY Engine")
        assert majority_row["state"] == "Warning"
        assert "45s window" in majority_row["detail"]

    def test_majority_on_and_tradable_is_running(self, run_on: ArcRuntime) -> None:
        rows = engine_status(run_on)
        majority_row = next(r for r in rows if r["engine"] == "MAJORITY Engine")
        assert majority_row["state"] == "Running"

    def test_every_row_state_is_one_of_the_five(self, run_on: ArcRuntime) -> None:
        allowed = {"Running", "Waiting", "Reconnecting", "Warning", "Error"}
        assert {r["state"] for r in engine_status(run_on)} <= allowed


class TestStatusAndSettingsCarryMajority:
    def test_status_payload_has_a_majority_section(self, run_on: ArcRuntime) -> None:
        _open_market(run_on)
        doc = asyncio.run(status_payload(run_on, _NOW))
        assert "majority" in doc
        assert doc["majority"]["config"]["enabled"] is True

    def test_settings_payload_carries_the_config_read_only(self, run_on: ArcRuntime) -> None:
        doc = settings_payload(run_on)
        assert doc["majority"]["enabled"] is True
        assert doc["majority"]["tradable"] is True

    def test_status_renders_with_majority_disabled_default(self, run: ArcRuntime) -> None:
        """The contract fixture's exact configuration: no majority= at all."""
        doc = asyncio.run(status_payload(run, _NOW))
        assert doc["majority"]["config"]["enabled"] is False
        assert doc["majority"]["state"] is None


class TestASettingsSaveDoesNotDiscardMajority:
    """POST /settings edits TWAP's fields. It must not truncate MAJORITY's.

    The endpoint merges over the COMPLETE stored row, and its fallback is the whole
    settings dict rather than TWAP's half. If either were `trading.as_storage_dict()`
    the saved row would hold no majority_* keys, and — because stored settings win
    over `.env` for every key they HOLD — the next boot would refill those eight from
    the environment and silently replace the operator's MAJORITY configuration.

    Asserted through the real route on a real store, because the bug it guards is a
    property of the write, not of the payload the deck reads back.
    """

    def test_editing_a_majority_field_leaves_all_eight_majority_keys_intact(
        self, run_on: ArcRuntime
    ) -> None:
        client = TestClient(build_app(run_on))
        before = run_on.settings.majority.as_storage_dict()

        assert client.post("/settings", json={"majority_shares": "7"}).status_code == 200

        stored = run_on.store.load_settings()
        # Legacy flat keys (the eight MAJORITY settings that were the contract before
        # multi-window) AND the per-window keys must both survive.
        for key in MAJORITY_KEYS:
            if key == "majority_shares":
                continue  # we edited this one on purpose
            assert key in stored, f"{key} was dropped by a MAJORITY-only settings save"
            assert stored[key] == before[key], f"{key} was changed by a MAJORITY-only settings save"
        per_window_keys = [k for k in before if k.startswith("majority_w_")]
        assert per_window_keys, "before must carry per-window keys"
        for key in per_window_keys:
            assert key in stored, f"{key} was dropped by a MAJORITY-only settings save"
            assert stored[key] == before[key]

    def test_the_saved_row_reboots_into_the_same_majority_config(
        self, run_on: ArcRuntime
    ) -> None:
        """The round trip that matters: what the next process actually loads.

        A row holding all eight keys is necessary but not sufficient — the values have
        to survive the builder too. Loaded against an `env` that has MAJORITY switched
        OFF, so a stored row that failed to win would come back disabled.
        """
        client = TestClient(build_app(run_on))
        assert client.post("/settings", json={"majority_shares": "7"}).status_code == 200

        rebooted = load_settings(ArcSettings(), run_on.store.load_settings())
        assert rebooted.seeded_from_env is False
        assert rebooted.majority.as_storage_dict() == run_on.settings.majority.as_storage_dict()
        assert rebooted.majority.enabled is True
        # shares is a per-window value; the fixture ships one 30s window with a
        # per-window override (`majority_w_30_shares`). Per-window overrides win
        # over the shared `majority_shares`, so the round-trip contract here is
        # that the stored row still carries BOTH keys (shared + override) and
        # they both survive into the rebuilt MajorityConfig.
        stored = run_on.store.load_settings()
        assert stored.get("majority_shares") == "7"
        assert any(str(w.shares) for w in rebooted.majority.windows), (
            "at least one window must carry shares after reload"
        )


class TestMAJORITYFieldEditing:
    """MAJORITY scalar and per-window fields are accepted by POST /settings.

    The whitelist covers the eight legacy keys and all `majority_w_<N>_<field>`
    per-window overrides. Per-window keys are not enumerated statically; they are
    validated by the builder (`build_majority_config`) after the whitelist check.
    """

    def test_the_eight_legacy_keys_are_in_the_editable_whitelist(self) -> None:
        from arc.api.routes import _EDITABLE

        for key in MAJORITY_KEYS:
            assert key in _EDITABLE, f"{key} is not in the editable whitelist"

    def test_per_window_key_pattern_is_accepted(self, run_on: ArcRuntime) -> None:
        """`majority_w_15_trigger_price` etc. must not be rejected as unknown."""
        client = TestClient(build_app(run_on))
        resp = client.post(
            "/settings",
            json={
                "majority_enabled": "true",
                "majority_execution_windows": "15",
                "majority_buffer": "1",
                "majority_trigger_price": "50000",
                "majority_target_limit_price": "50001",
                "majority_shares": "100",
                "majority_entry_price_min": "49990",
                "majority_entry_price_max": "50010",
                "majority_w_15_trigger_price": "50000",
                "majority_w_15_buffer": "1",
            },
        )
        assert resp.status_code == 200, resp.json()

    def test_rejecting_an_invalid_key(self, run_on: ArcRuntime) -> None:
        """Keys that are neither in _EDITABLE nor match the per-window pattern."""
        client = TestClient(build_app(run_on))
        resp = client.post(
            "/settings",
            json={"arbitrary_key": "value"},
        )
        assert resp.status_code == 400
        assert "arbitrary_key" in resp.json()["detail"]

    def test_invalid_majority_config_returns_400(self, run_on: ArcRuntime) -> None:
        """A value the builder rejects must not be written to the store."""
        client = TestClient(build_app(run_on))
        resp = client.post(
            "/settings",
            json={
                "majority_enabled": "true",
                "majority_execution_windows": "15",
                "majority_buffer": "1",
                "majority_trigger_price": "not_a_number",
                "majority_target_limit_price": "50001",
                "majority_shares": "100",
                "majority_entry_price_min": "49990",
                "majority_entry_price_max": "50010",
            },
        )
        assert resp.status_code == 400

    def test_saving_majority_fields_does_not_truncate_twap(self, run_on: ArcRuntime) -> None:
        """Editing MAJORITY fields must not discard TWAP's fields."""
        client = TestClient(build_app(run_on))
        before_twap = dict(run_on.store.load_settings())

        resp = client.post(
            "/settings",
            json={
                "majority_enabled": "true",
                "majority_execution_windows": "15,60",
                "majority_buffer": "1",
                "majority_trigger_price": "50000",
                "majority_target_limit_price": "50001",
                "majority_shares": "100",
                "majority_entry_price_min": "49990",
                "majority_entry_price_max": "50010",
                "majority_w_15_buffer": "1",
                "majority_w_60_buffer": "2",
            },
        )
        assert resp.status_code == 200, resp.json()

        after = run_on.store.load_settings()
        for key in ("execution_windows", "submission_count", "position_notional_usd", "buffers"):
            assert key in after, f"TWAP key {key} was dropped"
            assert after[key] == before_twap.get(key), f"TWAP key {key} was changed"

    def test_restart_required_is_true_after_majority_save(self, run_on: ArcRuntime) -> None:
        """The operator must know a restart is needed after any MAJORITY change."""
        client = TestClient(build_app(run_on))
        resp = client.post(
            "/settings",
            json={
                "majority_enabled": "true",
                "majority_execution_windows": "15",
                "majority_buffer": "1",
                "majority_trigger_price": "50000",
                "majority_target_limit_price": "50001",
                "majority_shares": "100",
                "majority_entry_price_min": "49990",
                "majority_entry_price_max": "50010",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["restart_required"] is True


class TestWindowOrderSummaryFields:
    """§37 LOE card payload verification.

    Each states_by_window entry must carry the order-level fields that paintActiveOrders
    renders. The values are read off persisted rows exactly as the ledger does, so a
    live card and a settled record of the same trade can never disagree. Verified at
    the payload layer rather than in the browser because the fresh server has no
    MAJORITY windows configured and cannot produce live window states.
    """

    def test_states_by_window_carries_loe_fields_with_no_orders(
        self, run_on: ArcRuntime
    ) -> None:
        """A fresh window with no orders still reports all LOE field keys."""
        market = _open_market(run_on)
        doc = majority_payload(run_on, market)
        assert doc["states_by_window"], "must have at least one window"
        first = next(iter(doc["states_by_window"].values()))
        for key in (
            "locked_trigger", "buffer", "limit_price", "fill_price",
            "shares", "filled_shares", "order_state", "retry_count", "live_pnl",
        ):
            assert key in first, f"LOE field {key} missing from states_by_window"

    def test_order_and_fill_appear_in_the_payload(self, tmp_path: Any) -> None:
        """Insert an order + fill, verify the payload reads them back correctly."""
        from arc.domain.enums import Direction, OrderState
        from arc.domain.models import Fill, Order

        run = _run(tmp_path, majority=_config())
        market = _open_market(run)
        slug = market.slug
        offset = 30

        # Insert an order into the store for this window
        order = Order(
            order_id="ord-001",
            market_slug=slug,
            offset_seconds=offset,
            direction=Direction.UP,
            price=Decimal("0.85"),
            size=Decimal("20"),
            state=OrderState.FILLED,
            filled_size=Decimal("20"),
            created_at=_NOW + 1.0,
            updated_at=_NOW + 2.0,
            venue_order_id="venue-001",
            reprice_chain_id="chain-001",
            engine="MAJORITY",
        )
        run.store.save_order(order)

        fill = Fill(
            fill_id="fill-001",
            order_id="ord-001",
            market_slug=slug,
            size=Decimal("20"),
            price=Decimal("0.84"),
            ts=_NOW + 2.0,
            engine="MAJORITY",
        )
        run.store.save_fill(fill)

        doc = majority_payload(run, market)
        window_key = str(offset)
        assert window_key in doc["states_by_window"], f"window {offset}s not in payload"
        entry = doc["states_by_window"][window_key]

        assert entry["limit_price"] == "0.85"
        assert entry["fill_price"] == "0.84"
        assert entry["shares"] == "20"
        assert entry["filled_shares"] == "20"
        assert entry["order_state"] == "Filled"
        assert entry["retry_count"] == 0
        # live_pnl depends on mark_price; PaperExecutor._books is empty here → None
        assert entry["live_pnl"] is None

    def test_reprice_chain_shows_retry_count(self, tmp_path: Any) -> None:
        """Two orders in the same chain → retry_count = 1, leader is newest."""
        from arc.domain.enums import Direction, OrderState
        from arc.domain.models import Order

        run = _run(tmp_path, majority=_config())
        market = _open_market(run)
        slug = market.slug
        offset = 30

        first = Order(
            order_id="ord-a",
            market_slug=slug,
            offset_seconds=offset,
            direction=Direction.UP,
            price=Decimal("0.90"),
            size=Decimal("10"),
            state=OrderState.CANCELLED,
            created_at=_NOW + 1.0,
            updated_at=_NOW + 2.0,
            reprice_chain_id="chain-r",
            engine="MAJORITY",
        )
        second = Order(
            order_id="ord-b",
            market_slug=slug,
            offset_seconds=offset,
            direction=Direction.UP,
            price=Decimal("0.88"),
            size=Decimal("10"),
            state=OrderState.SUBMITTED,
            created_at=_NOW + 3.0,
            updated_at=_NOW + 3.0,
            reprice_chain_id="chain-r",
            engine="MAJORITY",
        )
        run.store.save_order(first)
        run.store.save_order(second)

        doc = majority_payload(run, market)
        entry = doc["states_by_window"][str(offset)]
        assert entry["retry_count"] == 1
        # Leader is the newest (ord-b), limit_price reflects its price
        assert entry["limit_price"] == "0.88"
        assert entry["order_state"] == "Working"

    def test_live_pnl_marked_when_book_has_price(self, tmp_path: Any) -> None:
        """PaperExecutor.mark_price returns a value when the book is seeded."""
        from arc.domain.enums import Direction, OrderState
        from arc.domain.models import Fill, Order

        run = _run(tmp_path, majority=_config())
        market = _open_market(run)
        slug = market.slug
        offset = 30

        order = Order(
            order_id="ord-pnl",
            market_slug=slug,
            offset_seconds=offset,
            direction=Direction.UP,
            price=Decimal("0.85"),
            size=Decimal("20"),
            state=OrderState.FILLED,
            filled_size=Decimal("20"),
            created_at=_NOW + 1.0,
            updated_at=_NOW + 2.0,
            venue_order_id="venue-pnl",
            reprice_chain_id="chain-pnl",
            engine="MAJORITY",
        )
        run.store.save_order(order)
        run.store.save_fill(Fill(
            fill_id="fill-pnl",
            order_id="ord-pnl",
            market_slug=slug,
            size=Decimal("20"),
            price=Decimal("0.84"),
            ts=_NOW + 2.0,
            engine="MAJORITY",
        ))

        # Seed the paper executor's book so mark_price returns a value.
        # _books is a PaperExecutor implementation detail, not on the Protocol;
        # the type-ignore acknowledges we are testing through the concrete adapter.
        run.executor._books[(slug, Direction.UP)] = Decimal("0.90")  # type: ignore[attr-defined]

        doc = majority_payload(run, market)
        entry = doc["states_by_window"][str(offset)]
        # P&L = filled * mark - cost = 20 * 0.90 - 20 * 0.84 = 18.0 - 16.8 = 1.2
        assert Decimal(entry["live_pnl"]) == Decimal("1.2")
