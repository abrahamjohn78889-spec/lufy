"""Market discovery: slug math, metadata parsing, and the divergence rule.

The tests that matter here are the ones about DISAGREEMENT. Slug arithmetic is easy
to get right and easy to test; what decides whether the bot cancels at the correct
instant is what happens when the venue's close_ts differs from the computed one, and
that path only exists because the venue is the authority.
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from conftest import CLOSE_TS, WINDOW_TS

from arc.errors import FeedError
from arc.market.discovery import (
    GAMMA_MARKETS_URL,
    MarketDiscovery,
    decode_json,
    next_slug_math,
    parse_market_metadata,
    slug_math,
)


class _Response:
    """Enough of httpx.Response for MarketDiscovery. Nothing more."""

    def __init__(self, body: object, *, raise_error: Exception | None = None) -> None:
        self._body = body
        self._raise_error = raise_error
        self.json_kwargs: list[dict[str, Any]] = []

    def raise_for_status(self) -> None:
        if self._raise_error is not None:
            raise self._raise_error

    def json(self, **kwargs: Any) -> object:
        """Mirrors httpx.Response.json, which forwards kwargs to json.loads.

        The kwargs are recorded rather than ignored: the production call site passes
        `parse_float=Decimal`, and a double that silently dropped it would let a
        regression to the stdlib default pass every test in this file while rounding
        the official PTB in production.
        """
        self.json_kwargs.append(dict(kwargs))
        if isinstance(self._body, ValueError):
            raise self._body
        return self._body


class _Client:
    """A stand-in for httpx.AsyncClient that replays scripted responses.

    Injected rather than monkeypatched so every branch — a venue that disagrees, a
    venue that returns nothing, a transport failure — is reachable with no socket.
    """

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[tuple[str, dict[str, str]]] = []
        # Kept so a test can assert how the body was decoded, not just what it held.
        self.responses: list[_Response] = []

    async def get(self, url: str, *, params: dict[str, str], timeout: float) -> _Response:
        self.requests.append((url, dict(params)))
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        response = _Response(outcome)
        self.responses.append(response)
        return response


def _discovery(*outcomes: object) -> tuple[MarketDiscovery, _Client]:
    client = _Client(*outcomes)
    return MarketDiscovery(cast(httpx.AsyncClient, client)), client


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "conditionId": "0xcondition",
        "clobTokenIds": '["tok-up", "tok-down"]',
        "closeTime": CLOSE_TS,
        "priceToBeat": "120000.50",
        "active": True,
        "closed": False,
    }
    base.update(overrides)
    return base


class TestOfficialPtbLocation:
    """Where the official PTB actually lives, verified against the live endpoint.

    Established on 2026-08-05 against gamma-api: `priceToBeat` is NOT a top-level
    market field. A flat scan of all 70+ keys finds nothing, and a regex over the key
    names for beat/strike/reference/opening matches nothing either. The value sits on
    the market's event:

        markets[0].events[0].eventMetadata = {"finalPrice": …, "priceToBeat": …}

    A lookup that searched only the top level would find no PTB on any market ever
    and route every one down the fail-closed DEAD path — which on the dashboard reads
    exactly like the venue being unreachable, so the bot would look correctly cautious
    while being permanently unable to trade.
    """

    def test_the_ptb_is_read_from_the_event_metadata(self) -> None:
        payload = _payload(
            priceToBeat=None,
            events=[{"eventMetadata": {"finalPrice": "64260.55", "priceToBeat": "64276.69"}}],
        )
        assert parse_market_metadata(payload, slug="s").ptb_raw == "64276.69"

    def test_a_top_level_field_wins_when_the_venue_ever_promotes_one(self) -> None:
        payload = _payload(
            priceToBeat="1",
            events=[{"eventMetadata": {"priceToBeat": "2"}}],
        )
        assert parse_market_metadata(payload, slug="s").ptb_raw == "1"

    def test_a_live_market_with_null_event_metadata_yields_no_ptb(self) -> None:
        """Exactly what the live endpoint returns before a market settles. It must
        reach the fail-closed path, never a fabricated value."""
        payload = _payload(priceToBeat=None, events=[{"eventMetadata": None}])
        assert parse_market_metadata(payload, slug="s").ptb_raw is None

    def test_an_absent_events_list_yields_no_ptb(self) -> None:
        assert parse_market_metadata(_payload(priceToBeat=None), slug="s").ptb_raw is None

    def test_an_empty_events_list_yields_no_ptb(self) -> None:
        payload = _payload(priceToBeat=None, events=[])
        assert parse_market_metadata(payload, slug="s").ptb_raw is None

    def test_a_non_object_event_yields_no_ptb(self) -> None:
        payload = _payload(priceToBeat=None, events=["not-an-object"])
        assert parse_market_metadata(payload, slug="s").ptb_raw is None

    def test_a_decimal_ptb_survives_as_exact_text(self) -> None:
        """The venue sends priceToBeat as a bare JSON number. Decoded with
        parse_float=Decimal it arrives exact, and must not be re-rounded here."""
        payload = _payload(
            priceToBeat=None,
            events=[{"eventMetadata": {"priceToBeat": Decimal("64276.69623037441")}}],
        )
        assert parse_market_metadata(payload, slug="s").ptb_raw == "64276.69623037441"

    def test_a_decimal_ptb_is_not_rendered_in_exponent_form(self) -> None:
        """`str(Decimal("1E+5"))` is "1E+5", which to_decimal would still read but
        which no longer looks like the price the venue quoted."""
        payload = _payload(
            priceToBeat=None,
            events=[{"eventMetadata": {"priceToBeat": Decimal("1E+5")}}],
        )
        assert parse_market_metadata(payload, slug="s").ptb_raw == "100000"


class TestExactNumberDecoding:
    def test_the_body_is_decoded_with_parse_float_decimal(self) -> None:
        """The one guard that keeps the official PTB out of binary floating point.
        Without it the value is a C double before any ARC code can see it, and A1's
        "never estimate the PTB" has already been violated at the transport layer."""
        discovery, client = _discovery([_payload()])
        asyncio.run(discovery.fetch_metadata("btc-updown-5m-1"))
        assert client.responses[0].json_kwargs == [{"parse_float": Decimal}]

    def test_decode_json_keeps_numbers_exact(self) -> None:
        decoded = decode_json('{"priceToBeat": 64276.69623037441}')
        assert decoded["priceToBeat"] == Decimal("64276.69623037441")

    def test_the_stdlib_default_would_have_lost_digits(self) -> None:
        """States the failure numerically, so the guard above cannot be removed as
        cosmetic: the float round-trip does not reproduce the venue's digits."""
        text = "64276.69623037441123"
        assert str(json.loads(f'[{text}]')[0]) != text
        assert format(decode_json(f"[{text}]")[0], "f") == text


class TestSlugMath:
    def test_slug_math_matches_the_a5_formula(self) -> None:
        math = slug_math(float(WINDOW_TS) + 137.9)
        assert math.window_ts == WINDOW_TS
        assert math.close_ts == WINDOW_TS + 300
        assert math.slug == f"btc-updown-5m-{WINDOW_TS}"

    def test_slug_math_never_rounds_up_to_the_next_window(self) -> None:
        """One millisecond before close still belongs to the current market."""
        math = slug_math(float(CLOSE_TS) - 0.001)
        assert math.window_ts == WINDOW_TS

    def test_the_boundary_instant_belongs_to_the_next_market(self) -> None:
        math = slug_math(float(CLOSE_TS))
        assert math.window_ts == CLOSE_TS

    def test_markets_are_contiguous(self) -> None:
        """A5: the next window opens exactly when this one closes — no gap."""
        current = slug_math(float(WINDOW_TS))
        upcoming = next_slug_math(current.window_ts)
        assert upcoming.window_ts == current.close_ts
        assert upcoming.close_ts == current.close_ts + 300


class TestMetadataParsing:
    def test_parses_a_full_payload(self) -> None:
        metadata = parse_market_metadata(_payload(), slug="s")
        assert metadata.condition_id == "0xcondition"
        assert metadata.token_ids == ("tok-up", "tok-down")
        assert metadata.venue_close_ts == CLOSE_TS
        assert metadata.ptb_raw == "120000.50"
        assert metadata.active is True
        assert metadata.closed is False

    def test_missing_condition_id_is_a_hard_failure(self) -> None:
        """A market with no condition_id cannot be settled against, so no record."""
        with pytest.raises(FeedError, match="conditionId"):
            parse_market_metadata(_payload(conditionId=None), slug="s")

    def test_a_non_object_payload_is_rejected(self) -> None:
        with pytest.raises(FeedError, match="not an object"):
            parse_market_metadata(["not", "an", "object"], slug="s")

    def test_absent_ptb_is_preserved_as_none_not_defaulted(self) -> None:
        """None must reach the fail-closed path in ptb.py rather than being filled in."""
        metadata = parse_market_metadata(_payload(priceToBeat=None), slug="s")
        assert metadata.ptb_raw is None

    def test_a_float_ptb_is_refused_rather_than_stringified(self) -> None:
        """str(120000.05) preserves binary rounding while looking exact (A1)."""
        metadata = parse_market_metadata(_payload(priceToBeat=120000.05), slug="s")
        assert metadata.ptb_raw is None

    def test_ptb_text_is_carried_through_unchanged(self) -> None:
        """No normalisation: the exact characters the venue sent."""
        metadata = parse_market_metadata(_payload(priceToBeat="  120000.5000  "), slug="s")
        assert metadata.ptb_raw == "120000.5000"

    def test_absent_close_time_is_preserved_as_none(self) -> None:
        """Filling it with local arithmetic would make the divergence check vacuous."""
        metadata = parse_market_metadata(_payload(closeTime=None), slug="s")
        assert metadata.venue_close_ts is None

    def test_millisecond_close_time_is_read_as_seconds(self) -> None:
        metadata = parse_market_metadata(_payload(closeTime=CLOSE_TS * 1000), slug="s")
        assert metadata.venue_close_ts == CLOSE_TS

    def test_token_ids_arrive_as_a_json_string_or_a_list(self) -> None:
        as_list = parse_market_metadata(_payload(clobTokenIds=["a", "b"]), slug="s")
        as_objects = parse_market_metadata(
            _payload(clobTokenIds=[{"token_id": "a"}, {"token_id": "b"}]), slug="s"
        )
        assert as_list.token_ids == ("a", "b")
        assert as_objects.token_ids == ("a", "b")

    def test_unparseable_token_json_yields_no_tokens_rather_than_raising(self) -> None:
        metadata = parse_market_metadata(_payload(clobTokenIds="[not json"), slug="s")
        assert metadata.token_ids == ()

    def test_string_booleans_are_read(self) -> None:
        metadata = parse_market_metadata(_payload(active="false", closed="true"), slug="s")
        assert metadata.active is False
        assert metadata.closed is True


class TestFetchMetadata:
    def test_fetches_by_slug_against_the_gamma_endpoint(self) -> None:
        discovery, client = _discovery([_payload()])
        metadata = asyncio.run(discovery.fetch_metadata("btc-updown-5m-1"))
        assert metadata.condition_id == "0xcondition"
        assert client.requests == [(GAMMA_MARKETS_URL, {"slug": "btc-updown-5m-1"})]

    def test_reads_a_data_wrapped_body(self) -> None:
        discovery, _ = _discovery({"data": [_payload()]})
        assert asyncio.run(discovery.fetch_metadata("s")).condition_id == "0xcondition"

    def test_reads_a_bare_object_body(self) -> None:
        discovery, _ = _discovery(_payload())
        assert asyncio.run(discovery.fetch_metadata("s")).condition_id == "0xcondition"

    def test_an_empty_result_is_a_feed_error(self) -> None:
        discovery, _ = _discovery([])
        with pytest.raises(FeedError, match="no market"):
            asyncio.run(discovery.fetch_metadata("s"))

    def test_a_transport_failure_becomes_a_feed_error_not_a_fatal_one(self) -> None:
        """Operational: a brief outage must leave the process and its data alive (A8)."""
        discovery, _ = _discovery(httpx.ConnectError("refused"))
        with pytest.raises(FeedError, match="failed"):
            asyncio.run(discovery.fetch_metadata("s"))

    def test_invalid_json_becomes_a_feed_error(self) -> None:
        client = _Client()

        async def _get(url: str, *, params: dict[str, str], timeout: float) -> _Response:
            return _Response(ValueError("bad json"))

        client.get = _get  # type: ignore[method-assign]
        discovery = MarketDiscovery(cast(httpx.AsyncClient, client))
        with pytest.raises(FeedError, match="not valid JSON"):
            asyncio.run(discovery.fetch_metadata("s"))


class TestDivergence:
    def test_the_venue_close_ts_wins_and_the_divergence_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The venue settles the market, so the venue's clock decides when it closes."""
        venue_close = CLOSE_TS + 7
        discovery, _ = _discovery([_payload(closeTime=venue_close)])
        logger = logging.getLogger("arc.test.divergence")
        discovery = MarketDiscovery(
            cast(httpx.AsyncClient, _Client([_payload(closeTime=venue_close)])), logger=logger
        )

        with caplog.at_level(logging.WARNING, logger="arc.test.divergence"):
            found = asyncio.run(discovery.discover(float(WINDOW_TS)))

        assert found.close_ts == venue_close
        assert found.computed_close_ts == CLOSE_TS
        assert found.diverged is True
        assert "SLUG_MATH_DIVERGENCE" in caplog.text

    def test_the_market_is_not_skipped_when_the_venue_disagrees(self) -> None:
        """Divergence is a warning, not a refusal — the market is still discovered."""
        discovery, _ = _discovery([_payload(closeTime=CLOSE_TS + 7)])
        found = asyncio.run(discovery.discover(float(WINDOW_TS)))
        assert found.slug == f"btc-updown-5m-{WINDOW_TS}"
        assert found.metadata.condition_id == "0xcondition"

    def test_agreement_reports_no_divergence(self) -> None:
        discovery, _ = _discovery([_payload()])
        found = asyncio.run(discovery.discover(float(WINDOW_TS)))
        assert found.diverged is False
        assert found.close_ts == CLOSE_TS

    def test_an_absent_venue_close_ts_falls_back_to_the_computed_value(self) -> None:
        """Absent is not disagreement; local arithmetic stands and nothing is logged."""
        discovery, _ = _discovery([_payload(closeTime=None)])
        found = asyncio.run(discovery.discover(float(WINDOW_TS)))
        assert found.close_ts == CLOSE_TS
        assert found.diverged is False

    def test_prefetch_next_asks_for_the_contiguous_following_market(self) -> None:
        """N+1's metadata is in hand before the boundary, so its PTB freezes instantly."""
        discovery, client = _discovery([_payload()])
        found = asyncio.run(discovery.prefetch_next(WINDOW_TS))
        assert found.window_ts == CLOSE_TS
        assert client.requests[0][1] == {"slug": f"btc-updown-5m-{CLOSE_TS}"}
