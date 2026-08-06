"""Wallet Panel: official fields or UNAVAILABLE, and ledger math from storage.

Q3 forbids fabricated, estimated or heuristically derived wallet values. These
tests pin the two failures that would violate it: a venue-sourced field appearing
as a number in V1 (where no venue account exists), and a failed venue read
reporting zero instead of DISCONNECTED.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from arc.clock import FrozenClock
from arc.domain.enums import Mode, Outcome
from arc.domain.models import MarketInstance, Settlement
from arc.execution.wallet import LiveWallet, PaperWallet, build_wallet, ledger_stats
from arc.storage.store import Store

_NOW = 1_760_000_000.0


@pytest.fixture
def store(tmp_path: object) -> Store:
    db = Store(f"{tmp_path}/arc.db")  # type: ignore[str-bytes-safe]
    db.migrate(_NOW)
    return db


def _settle(store: Store, slug: str, pnl: str, at: float) -> None:
    # A distinct window_ts per row: markets.window_ts is UNIQUE.
    window_ts = int(at)
    store.create_market(
        MarketInstance(slug=slug, window_ts=window_ts, close_ts=window_ts + 300), at
    )
    store.save_settlement(
        Settlement(
            market_slug=slug,
            outcome=Outcome.UP,
            settlement_twap=Decimal("100"),
            ptb=Decimal("100"),
            settled_at=at,
            pnl=Decimal(pnl),
        )
    )


class TestLedgerStats:
    def test_empty_history_is_zero_not_absent(self, store: Store) -> None:
        stats = ledger_stats(store, _NOW, _NOW)
        assert stats.realized_lifetime == Decimal("0")
        assert stats.markets_settled == 0

    def test_today_excludes_older_than_a_day(self, store: Store) -> None:
        """A daily figure that included yesterday would understate a recovery day."""
        _settle(store, "old", "-50", _NOW - 90_000)
        _settle(store, "new", "10", _NOW - 100)
        stats = ledger_stats(store, _NOW, _NOW - 200)
        assert stats.realized_today == Decimal("10")
        assert stats.realized_lifetime == Decimal("-40")
        assert stats.realized_run == Decimal("10")

    def test_streak_is_the_leading_run_only(self, store: Store) -> None:
        _settle(store, "a", "-5", _NOW - 300)
        _settle(store, "b", "7", _NOW - 200)
        _settle(store, "c", "3", _NOW - 100)
        stats = ledger_stats(store, _NOW, _NOW - 400)
        assert stats.winning_streak == 2
        assert stats.losing_streak == 0
        assert stats.largest_win == Decimal("7")
        assert stats.largest_loss == Decimal("-5")

    def test_a_flat_settlement_breaks_no_streak(self, store: Store) -> None:
        """A market with no fill is not a loss; counting it would trip the loss limit."""
        _settle(store, "a", "-5", _NOW - 300)
        _settle(store, "flat", "0", _NOW - 100)
        stats = ledger_stats(store, _NOW, _NOW - 400)
        assert stats.losing_streak == 1
        assert stats.wins == 0
        assert stats.losses == 1


class TestPaperWalletReportsNoVenueValue:
    def test_every_venue_field_is_unavailable(self, store: Store) -> None:
        """Paper PnL presented as a balance would size real V2 positions."""
        snap = asyncio.run(PaperWallet(store).snapshot(_NOW, run_start=_NOW))
        assert snap.available_balance is None
        assert snap.buying_power is None
        assert snap.total_account_value is None
        assert snap.current_exposure is None
        assert snap.unrealized_pnl is None
        assert snap.credentialed is False

    def test_ledger_fields_are_still_reported(self, store: Store) -> None:
        _settle(store, "a", "12", _NOW - 10)
        snap = asyncio.run(PaperWallet(store).snapshot(_NOW, run_start=_NOW - 100))
        assert snap.ledger.realized_run == Decimal("12")


class _BrokenClient:
    """Every official read fails. Nothing else about the panel may change."""

    wallet = "0xabc"
    credentials = object()

    class environment:
        name = "POLYGON"

    async def get_balance_allowance(self, *, asset_type: str) -> object:
        raise RuntimeError("venue down")

    async def get_portfolio_values(self, *, user: str) -> object:
        raise RuntimeError("venue down")

    def list_positions(self, *, user: str) -> object:
        raise RuntimeError("venue down")


class TestFailedVenueReadIsNotZero:
    def test_status_is_disconnected_and_values_are_none(self, store: Store) -> None:
        wallet = LiveWallet(_BrokenClient(), store)  # type: ignore[arg-type]
        snap = asyncio.run(wallet.snapshot(_NOW, run_start=_NOW))
        assert snap.status == "DISCONNECTED"
        assert snap.available_balance is None
        assert snap.total_account_value is None
        assert snap.current_exposure is None


class TestBuilderNeverFakesLiveState:
    def test_v2_without_a_client_falls_back_to_paper(self, store: Store) -> None:
        """A V2 boot whose client failed must not display a fabricated balance."""
        assert isinstance(build_wallet(Mode.V2, store, None), PaperWallet)

    def test_v1_is_always_paper(self, store: Store) -> None:
        assert isinstance(build_wallet(Mode.V1, store, None), PaperWallet)


def test_frozen_clock_is_available_for_snapshot_timing() -> None:
    """Guards the import used by callers that pass clock.now() into snapshot()."""
    assert FrozenClock(_NOW).now() == _NOW
