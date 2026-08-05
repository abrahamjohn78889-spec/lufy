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
    BoundaryReference,
    freeze_ptb_for,
    resolve_ptb,
)


def _metadata(ptb_raw: str | None) -> MarketMetadata:
    return MarketMetadata(
        slug=f"btc-updown-5m-{WINDOW_TS}",
        condition_id="0xcondition",
        token_ids=("up", "down"),
        venue_close_ts=CLOSE_TS,
        ptb_raw=ptb_raw,
        active=True,
        closed=False,
        raw={},
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

    def test_metadata_wins_over_an_available_boundary_reference(self) -> None:
        """L1 before L2, always. The metadata value is the official one."""
        boundary = BoundaryReference(
            boundary_ts=WINDOW_TS,
            price=Decimal("999.99"),
            observed_ts=float(WINDOW_TS),
            continuous=True,
        )
        resolution = resolve_ptb(_metadata("120000.50"), window_ts=WINDOW_TS, boundary=boundary)
        assert resolution.value == Decimal("120000.50")
        assert resolution.source == SOURCE_METADATA

    def test_an_unparseable_official_value_falls_through_to_l2(self) -> None:
        """A venue-side defect in the field does not invalidate an observed boundary."""
        boundary = BoundaryReference(
            boundary_ts=WINDOW_TS,
            price=Decimal("120000.25"),
            observed_ts=float(WINDOW_TS),
            continuous=True,
        )
        resolution = resolve_ptb(_metadata("not-a-price"), window_ts=WINDOW_TS, boundary=boundary)
        assert resolution.source == SOURCE_PREVIOUS_CLOSE
        assert resolution.value == Decimal("120000.25")

    def test_a_non_positive_official_value_is_not_a_price(self) -> None:
        resolution = resolve_ptb(_metadata("0"), window_ts=WINDOW_TS)
        assert resolution.available is False
        assert "not a positive price" in resolution.detail


class TestBoundarySource:
    def test_the_boundary_reference_is_official_when_the_connection_held(self) -> None:
        """Markets are contiguous, so N's close instant IS N+1's opening reference."""
        boundary = BoundaryReference(
            boundary_ts=WINDOW_TS,
            price=Decimal("120000.25"),
            observed_ts=float(WINDOW_TS),
            continuous=True,
        )
        resolution = resolve_ptb(_metadata(None), window_ts=WINDOW_TS, boundary=boundary)
        assert resolution.source == SOURCE_PREVIOUS_CLOSE
        assert resolution.value == Decimal("120000.25")

    def test_a_discontinuous_boundary_is_refused(self) -> None:
        """Across a reconnect the process did not observe the boundary — using what it
        holds would be exactly the estimation A1 forbids."""
        boundary = BoundaryReference(
            boundary_ts=WINDOW_TS,
            price=Decimal("120000.25"),
            observed_ts=float(WINDOW_TS),
            continuous=False,
        )
        resolution = resolve_ptb(_metadata(None), window_ts=WINDOW_TS, boundary=boundary)
        assert resolution.available is False
        assert "not continuous" in resolution.detail

    def test_a_reference_for_a_different_boundary_is_refused(self) -> None:
        boundary = BoundaryReference(
            boundary_ts=WINDOW_TS - 300,
            price=Decimal("120000.25"),
            observed_ts=float(WINDOW_TS - 300),
            continuous=True,
        )
        resolution = resolve_ptb(_metadata(None), window_ts=WINDOW_TS, boundary=boundary)
        assert resolution.available is False
        assert f"not {WINDOW_TS}" in resolution.detail

    def test_a_reference_observed_near_but_not_at_the_boundary_is_refused(self) -> None:
        """A nearby price is not the boundary price; substituting it is an estimate."""
        boundary = BoundaryReference(
            boundary_ts=WINDOW_TS,
            price=Decimal("120000.25"),
            observed_ts=float(WINDOW_TS) + 4.0,
            continuous=True,
        )
        assert boundary.usable_for(WINDOW_TS) is False
        resolution = resolve_ptb(_metadata(None), window_ts=WINDOW_TS, boundary=boundary)
        assert resolution.available is False
        assert "not observed at the boundary instant" in resolution.detail

    def test_a_reference_inside_the_tolerance_is_accepted(self) -> None:
        boundary = BoundaryReference(
            boundary_ts=WINDOW_TS,
            price=Decimal("120000.25"),
            observed_ts=float(WINDOW_TS) + 1.0,
            continuous=True,
        )
        assert boundary.usable_for(WINDOW_TS) is True

    def test_a_non_positive_boundary_price_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            BoundaryReference(
                boundary_ts=WINDOW_TS,
                price=Decimal("0"),
                observed_ts=float(WINDOW_TS),
                continuous=True,
            )


class TestUnavailable:
    def test_no_metadata_and_no_boundary_yields_no_value(self) -> None:
        resolution = resolve_ptb(_metadata(None), window_ts=WINDOW_TS)
        assert resolution.value is None
        assert resolution.available is False
        assert "no boundary reference held" in resolution.detail

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
