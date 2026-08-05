"""Price To Beat. Fetched, never computed (A1 Rule 1).

Two kinds of test here. The behavioural ones drive resolution through every source
and every gate. The structural one reads ptb.py's own AST and asserts that no
arithmetic producing a price exists in it at all — because a passing behavioural
suite cannot rule out an estimation path that simply was not exercised, and an
estimated PTB is the one error that would produce confident, wrong trades with
nothing in the record showing a number had been invented.
"""

from __future__ import annotations

import ast
import logging
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import CLOSE_TS, OFFSETS, WINDOW_TS

from arc.domain.enums import MarketPhase
from arc.domain.models import MarketInstance
from arc.market.discovery import MarketMetadata
from arc.market.ptb import (
    DEAD_REASON_PTB_UNAVAILABLE,
    SOURCE_METADATA,
    SOURCE_PREVIOUS_CLOSE,
    PreviousClosePtb,
    PreviousClosePtbCache,
    freeze_ptb_for,
    resolve_ptb,
)

# The settled market whose published finalPrice opens WINDOW_TS. Markets are
# contiguous, so its close IS WINDOW_TS.
PREVIOUS_WINDOW_TS = WINDOW_TS - 300


def _metadata(
    ptb_raw: str | None,
    *,
    final_price_raw: str | None = None,
    window_ts: int = WINDOW_TS,
) -> MarketMetadata:
    return MarketMetadata(
        slug=f"btc-updown-5m-{window_ts}",
        condition_id="0xcondition",
        token_ids=("up", "down"),
        venue_close_ts=CLOSE_TS,
        ptb_raw=ptb_raw,
        final_price_raw=final_price_raw,
        active=True,
        closed=False,
        raw={},
    )


def _previous_close(price: str, *, settled_window_ts: int = PREVIOUS_WINDOW_TS) -> PreviousClosePtb:
    """The venue's published finalPrice for a settled market, as the cache holds it."""
    return PreviousClosePtb(
        settled_window_ts=settled_window_ts,
        opens_window_ts=settled_window_ts + 300,
        price=Decimal(price),
        raw=price,
    )


def _market() -> MarketInstance:
    return MarketInstance.create(WINDOW_TS, OFFSETS)


class TestMetadataSource:
    def test_the_official_metadata_value_is_used_verbatim(self) -> None:
        resolution = resolve_ptb(_metadata("120000.50"), window_ts=WINDOW_TS)
        assert resolution.value == Decimal("120000.50")
        assert resolution.source == SOURCE_METADATA
        assert resolution.available is True

    def test_the_exact_decimal_text_survives_conversion(self) -> None:
        """No float in between: trailing zeros and full precision are preserved."""
        resolution = resolve_ptb(_metadata("120000.123456789"), window_ts=WINDOW_TS)
        assert resolution.value == Decimal("120000.123456789")

    def test_metadata_wins_over_an_available_previous_close(self) -> None:
        """L1 before L2, always. The market's own metadata field is the official one."""
        resolution = resolve_ptb(
            _metadata("120000.50"),
            window_ts=WINDOW_TS,
            previous_close=_previous_close("999.99"),
        )
        assert resolution.value == Decimal("120000.50")
        assert resolution.source == SOURCE_METADATA

    def test_an_unparseable_official_value_falls_through_to_l2(self) -> None:
        """A venue-side defect in one field does not invalidate a different published
        official value."""
        resolution = resolve_ptb(
            _metadata("not-a-price"),
            window_ts=WINDOW_TS,
            previous_close=_previous_close("120000.25"),
        )
        assert resolution.source == SOURCE_PREVIOUS_CLOSE
        assert resolution.value == Decimal("120000.25")

    def test_a_non_positive_official_value_is_not_a_price(self) -> None:
        resolution = resolve_ptb(_metadata("0"), window_ts=WINDOW_TS)
        assert resolution.available is False
        assert "not a positive price" in resolution.detail


class TestPreviousClosePtb:
    """The L2 source: the venue's PUBLISHED finalPrice for the previous market.

    Established live on 2026-08-05 across six consecutive settled markets with zero
    mismatches: priceToBeat(M) == finalPrice(M-1), exactly. This is a lookup of an
    official venue value, not an observation of a price and not arithmetic.

    ARC's own boundary observation is deliberately NOT a source. Measured against the
    venue's published number it differed by 6E-12 — genuinely, not as a decoding
    artifact — and close is not official.
    """

    def test_the_published_previous_close_is_the_official_opening_reference(self) -> None:
        resolution = resolve_ptb(
            _metadata(None),
            window_ts=WINDOW_TS,
            previous_close=_previous_close("120000.25"),
        )
        assert resolution.source == SOURCE_PREVIOUS_CLOSE
        assert resolution.value == Decimal("120000.25")

    def test_the_detail_names_the_settled_market_the_value_came_from(self) -> None:
        """An operator reconciling a trade has to be able to find the source market."""
        resolution = resolve_ptb(
            _metadata(None),
            window_ts=WINDOW_TS,
            previous_close=_previous_close("120000.25"),
        )
        assert "120000.25" in resolution.detail
        assert str(PREVIOUS_WINDOW_TS) in resolution.detail

    def test_a_value_for_a_different_window_is_refused(self) -> None:
        """The cached price belongs to exactly one window. Reusing it for a later
        market would carry a two-market-old reference forward, which is the estimation
        A1 forbids."""
        stale = _previous_close("120000.25", settled_window_ts=PREVIOUS_WINDOW_TS - 300)
        resolution = resolve_ptb(_metadata(None), window_ts=WINDOW_TS, previous_close=stale)
        assert resolution.available is False
        assert str(WINDOW_TS) in resolution.detail

    def test_usable_for_is_exact_with_no_tolerance(self) -> None:
        """No tolerance to apply: the venue published this for a specific market."""
        entry = _previous_close("120000.25")
        assert entry.usable_for(WINDOW_TS) is True
        assert entry.usable_for(WINDOW_TS + 1) is False
        assert entry.usable_for(WINDOW_TS - 1) is False

    def test_a_non_contiguous_pairing_is_refused_at_construction(self) -> None:
        """Markets are contiguous (A5). A settled market whose close is not the stated
        opening window means the pairing was computed wrongly, and a wrong pairing
        assigns one market's close price to a different market's opening."""
        with pytest.raises(ValueError, match="does not close at"):
            PreviousClosePtb(
                settled_window_ts=PREVIOUS_WINDOW_TS,
                opens_window_ts=WINDOW_TS + 300,
                price=Decimal("120000.25"),
                raw="120000.25",
            )

    def test_a_non_positive_published_price_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            PreviousClosePtb(
                settled_window_ts=PREVIOUS_WINDOW_TS,
                opens_window_ts=WINDOW_TS,
                price=Decimal("0"),
                raw="0",
            )


class TestPreviousClosePtbCache:
    """The cache across consecutive markets — the property the whole design rests on."""

    def test_an_unpublished_final_price_caches_nothing(self) -> None:
        """finalPrice is null for a market's ENTIRE life. That is the normal state, so
        it must not raise and must not populate the cache with anything."""
        cache = PreviousClosePtbCache()
        assert cache.offer(_metadata(None), settled_window_ts=PREVIOUS_WINDOW_TS) is None
        assert cache.latest is None
        assert cache.for_window(WINDOW_TS) is None

    def test_a_published_final_price_is_cached_for_the_next_window(self) -> None:
        cache = PreviousClosePtbCache()
        entry = cache.offer(
            _metadata(None, final_price_raw="64063.6718254685"),
            settled_window_ts=PREVIOUS_WINDOW_TS,
        )
        assert entry is not None
        assert entry.opens_window_ts == WINDOW_TS
        held = cache.for_window(WINDOW_TS)
        assert held is not None
        assert held.price == Decimal("64063.6718254685")

    def test_the_cached_value_resolves_the_next_market_ptb(self) -> None:
        """End to end: cache M-1's published close, then resolve M's PTB from it with
        no PTB field of M's own."""
        cache = PreviousClosePtbCache()
        cache.offer(
            _metadata(None, final_price_raw="64063.6718254685"),
            settled_window_ts=PREVIOUS_WINDOW_TS,
        )
        resolution = resolve_ptb(
            _metadata(None),
            window_ts=WINDOW_TS,
            previous_close=cache.for_window(WINDOW_TS),
        )
        assert resolution.source == SOURCE_PREVIOUS_CLOSE
        assert resolution.value == Decimal("64063.6718254685")

    def test_the_cache_advances_across_consecutive_markets(self) -> None:
        """Six consecutive markets, each resolving from the previous one's published
        close. These are the exact values measured live on 2026-08-05, replayed."""
        observed = [
            (1785914100, "64063.6718254685"),
            (1785914400, "64000.675713625"),
            (1785914700, "64017.96609754"),
            (1785915000, "64014.4764719921"),
            (1785915300, "63992.604802785"),
            (1785915600, "63945.155662425"),
        ]
        cache = PreviousClosePtbCache()
        resolved: list[tuple[int, Decimal]] = []
        for settled_ts, final_price in observed:
            cache.offer(
                _metadata(None, final_price_raw=final_price, window_ts=settled_ts),
                settled_window_ts=settled_ts,
            )
            opens = settled_ts + 300
            resolution = resolve_ptb(
                _metadata(None, window_ts=opens),
                window_ts=opens,
                previous_close=cache.for_window(opens),
            )
            assert resolution.source == SOURCE_PREVIOUS_CLOSE, opens
            assert resolution.value is not None
            resolved.append((opens, resolution.value))

        assert [ts for ts, _ in resolved] == [ts + 300 for ts, _ in observed]
        assert [str(v) for _, v in resolved] == [price for _, price in observed]

    def test_a_stale_cache_leaves_the_next_market_unresolved(self) -> None:
        """The cache does not carry a value forward. If M-1 never published, M's PTB is
        unavailable and M is not traded — it is not given M-2's close price."""
        cache = PreviousClosePtbCache()
        cache.offer(
            _metadata(None, final_price_raw="2.0"),
            settled_window_ts=PREVIOUS_WINDOW_TS - 300,
        )
        assert cache.for_window(WINDOW_TS) is None
        resolution = resolve_ptb(
            _metadata(None), window_ts=WINDOW_TS, previous_close=cache.for_window(WINDOW_TS)
        )
        assert resolution.available is False

    def test_a_newer_entry_replaces_an_older_one(self) -> None:
        cache = PreviousClosePtbCache()
        cache.offer(
            _metadata(None, final_price_raw="1.0"),
            settled_window_ts=PREVIOUS_WINDOW_TS - 300,
        )
        cache.offer(_metadata(None, final_price_raw="2.0"), settled_window_ts=PREVIOUS_WINDOW_TS)
        latest = cache.latest
        assert latest is not None
        assert latest.settled_window_ts == PREVIOUS_WINDOW_TS
        assert latest.price == Decimal("2.0")

    def test_a_late_arriving_older_entry_never_replaces_a_newer_one(self) -> None:
        """Metadata fetches are not ordered. A slow response for M-2 landing after
        M-1's would otherwise hand the next market a two-market-old reference."""
        cache = PreviousClosePtbCache()
        cache.offer(_metadata(None, final_price_raw="2.0"), settled_window_ts=PREVIOUS_WINDOW_TS)
        assert (
            cache.offer(
                _metadata(None, final_price_raw="1.0"),
                settled_window_ts=PREVIOUS_WINDOW_TS - 300,
            )
            is None
        )
        latest = cache.latest
        assert latest is not None
        assert latest.price == Decimal("2.0")

    def test_re_offering_the_same_market_is_idempotent(self) -> None:
        """The runtime polls until the value appears, so the same market is offered
        repeatedly once it has settled."""
        cache = PreviousClosePtbCache()
        metadata = _metadata(None, final_price_raw="2.0")
        first = cache.offer(metadata, settled_window_ts=PREVIOUS_WINDOW_TS)
        second = cache.offer(metadata, settled_window_ts=PREVIOUS_WINDOW_TS)
        assert first == second
        assert cache.latest == first

    def test_only_the_latest_entry_is_retained(self) -> None:
        """Unbounded retention across a 24/7 run would hold values that can never be
        read again: each superseded entry belongs to a market already frozen or dead."""
        cache = PreviousClosePtbCache()
        for k in range(20):
            cache.offer(
                _metadata(None, final_price_raw=f"{60000 + k}.5"),
                settled_window_ts=PREVIOUS_WINDOW_TS + 300 * k,
            )
        latest = cache.latest
        assert latest is not None
        assert latest.settled_window_ts == PREVIOUS_WINDOW_TS + 300 * 19
        assert PreviousClosePtbCache.__slots__ == ("_latest",)

    def test_a_malformed_published_price_caches_nothing(self) -> None:
        cache = PreviousClosePtbCache()
        assert (
            cache.offer(
                _metadata(None, final_price_raw="n/a"), settled_window_ts=PREVIOUS_WINDOW_TS
            )
            is None
        )
        assert (
            cache.offer(
                _metadata(None, final_price_raw="0"), settled_window_ts=PREVIOUS_WINDOW_TS
            )
            is None
        )
        assert cache.latest is None

    def test_the_exact_published_digits_survive(self) -> None:
        """The venue sends 13 significant decimals. A float would round them, and the
        rounded value would compare unequal to the venue's own settlement number."""
        cache = PreviousClosePtbCache()
        entry = cache.offer(
            _metadata(None, final_price_raw="64104.560649297964"),
            settled_window_ts=PREVIOUS_WINDOW_TS,
        )
        assert entry is not None
        assert entry.price == Decimal("64104.560649297964")
        assert str(entry.price) == "64104.560649297964"

    def test_two_caches_share_no_state(self) -> None:
        """Instance state, never module state (A11)."""
        first = PreviousClosePtbCache()
        second = PreviousClosePtbCache()
        first.offer(_metadata(None, final_price_raw="2.0"), settled_window_ts=PREVIOUS_WINDOW_TS)
        assert second.latest is None


class TestUnavailable:
    def test_no_metadata_and_no_cached_close_yields_no_value(self) -> None:
        resolution = resolve_ptb(_metadata(None), window_ts=WINDOW_TS)
        assert resolution.value is None
        assert resolution.available is False
        assert "finalPrice not published yet" in resolution.detail

    def test_the_detail_names_which_condition_failed(self) -> None:
        """The operator has to be able to tell a missing field from a broken feed."""
        resolution = resolve_ptb(_metadata(None), window_ts=WINDOW_TS)
        assert "metadata carried no PTB field" in resolution.detail

    def test_resolution_has_no_side_effects_on_the_market(self) -> None:
        """resolve_ptb never marks anything dead; the caller does. Kept pure so every
        branch is reachable without constructing a market."""
        market = _market()
        resolve_ptb(_metadata(None), window_ts=WINDOW_TS)
        assert market.phase is MarketPhase.DISCOVERED
        assert market.ptb is None


class TestFreezing:
    def test_freezing_sets_the_ptb_and_reports_success(self) -> None:
        market = _market()
        resolution = resolve_ptb(_metadata("120000.50"), window_ts=WINDOW_TS)
        assert freeze_ptb_for(market, resolution) is True
        assert market.ptb == Decimal("120000.50")

    def test_unavailability_marks_the_market_dead_and_logs_the_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        market = _market()
        logger = logging.getLogger("arc.test.ptb")
        resolution = resolve_ptb(_metadata(None), window_ts=WINDOW_TS)

        with caplog.at_level(logging.ERROR, logger="arc.test.ptb"):
            assert freeze_ptb_for(market, resolution, logger=logger) is False

        assert market.phase is MarketPhase.DEAD
        assert market.dead_reason == DEAD_REASON_PTB_UNAVAILABLE
        assert market.ptb is None
        assert "PTB Unavailable" in caplog.text
        # The detail rides in extra["arc_detail"], which the formatter renders as the
        # right-hand column; caplog.text carries only the message.
        details = [getattr(r, "arc_detail", "") for r in caplog.records]
        assert any("no trading this market" in d for d in details)

    def test_a_dead_market_keeps_accepting_observations_for_the_record(self) -> None:
        """DEAD means never traded, not never recorded — but the accumulator stops,
        because a market with no official PTB has no trigger to compute against."""
        market = _market()
        freeze_ptb_for(market, resolve_ptb(_metadata(None), window_ts=WINDOW_TS))
        assert market.accepts_observations() is False

    def test_freezing_twice_raises_rather_than_refreshing(self) -> None:
        """A second call means some path believes it may refresh the PTB (A12)."""
        market = _market()
        resolution = resolve_ptb(_metadata("120000.50"), window_ts=WINDOW_TS)
        freeze_ptb_for(market, resolution)
        with pytest.raises(ValueError, match="already frozen"):
            freeze_ptb_for(market, resolution)


class TestNoEstimationPath:
    """Structural: assert by source inspection that ptb.py cannot compute a price.

    This reads the module's AST rather than mutating it. A behavioural suite proves
    what the exercised paths do; it cannot prove the absence of an unexercised
    estimation path, and absence is the actual requirement.
    """

    @pytest.fixture
    def ptb_module(self, source_root: Path) -> ast.Module:
        path = source_root / "arc" / "market" / "ptb.py"
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_no_arithmetic_operator_appears_on_a_price(self, ptb_module: ast.Module) -> None:
        """No mean, no midpoint, no interpolation, no carry-forward.

        Only the tolerance comparison is permitted to multiply, and that operates on
        a timestamp difference, never on a price.
        """
        offenders: list[str] = []
        for node in ast.walk(ptb_module):
            if not isinstance(node, ast.BinOp):
                continue
            source = ast.unparse(node)
            if "price" in source or "value" in source:
                offenders.append(f"line {node.lineno}: {source}")
        assert not offenders, (
            "arithmetic on a price in ptb.py — a computed PTB is forbidden (A1 Rule 1):\n  "
            + "\n  ".join(offenders)
        )

    def test_no_averaging_or_interpolation_function_is_called(
        self, ptb_module: ast.Module
    ) -> None:
        forbidden = {
            "mean",
            "fmean",
            "average",
            "median",
            "interpolate",
            "midpoint",
            "estimate",
            "approximate",
            "extrapolate",
            "sum",
        }
        offenders: list[str] = []
        for node in ast.walk(ptb_module):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func).split(".")[-1]
                if name in forbidden:
                    offenders.append(f"line {node.lineno}: {name}()")
        assert not offenders, (
            "an averaging or estimating call in ptb.py — PTB is fetched, never "
            "calculated (A1 Rule 1):\n  " + "\n  ".join(offenders)
        )

    def test_the_only_conversion_applied_to_the_official_value_is_to_decimal(
        self, ptb_module: ast.Module
    ) -> None:
        """to_decimal is a unit reading of exact text, not a computation.

        Asserting the conversion is present as well as exclusive: reading the official
        value through float() would round it before it was ever compared.
        """
        calls = {
            ast.unparse(node.func).split(".")[-1]
            for node in ast.walk(ptb_module)
            if isinstance(node, ast.Call)
        }
        assert "to_decimal" in calls
        assert "float" not in calls, "float() in ptb.py would round the official value"

    def test_the_module_imports_no_arithmetic_helpers(self, ptb_module: ast.Module) -> None:
        imported: set[str] = set()
        for node in ast.walk(ptb_module):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "statistics" not in imported
        assert "math" not in imported
        assert "numpy" not in imported
