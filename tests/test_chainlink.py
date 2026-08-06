"""CHAINLINK DATA STREAMS: the authentication and decode paths.

Every failure this file guards produces a symptom that looks like something else:

  string-to-sign  a newline-joined signature is a well-formed HMAC of the wrong
                  string. The handshake returns 401, which reads as a bad key —
                  so the operator rotates a credential that was never wrong.
  body hash       omitted for GET, the same opaque 401.
  decimals        the wrong scale is not a rejected sample; it is every sample
                  wrong by the same factor, so the deviation check (which compares
                  each sample to the last) passes on all of them.
  schema version  a v4 body read with the v3 layout yields plausible numbers in
                  the wrong fields, and the first one is the price.
  isolation       a Chainlink selection that leaves RTDS running is a bot trading
                  a price source the dashboard does not name.

NOT RUN AGAINST LIVE CHAINLINK. There are no credentials and no stream entitlement
in this environment. These tests pin the documented wire format; they do not prove
the endpoint accepts it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from eth_abi.abi import encode as abi_encode

from arc.clock import FrozenClock
from arc.errors import ConfigInvariantError, FeedError
from arc.market.chainlink import (
    CHAINLINK_WS_PATH,
    ChainlinkFeed,
    auth_headers,
    build_chainlink_feed,
    decode_full_report,
    string_to_sign,
)

_KEY = "11111111-2222-3333-4444-555555555555"
_SECRET = "a-shared-secret"
_FEED = "0x000359843a543ee2fe414dc14c7e7920ef10f4372990b79d6361cdc0dd1ba782"
_TS = 1_754_400_000_000


def _clock() -> FrozenClock:
    return FrozenClock(1_754_400_000.0)


def _full_report(price: int, *, version: int = 3, observations_ts: int = 1_754_400_000) -> bytes:
    """Build a report blob the way the documented envelope describes it.

    The schema version is not a separate field: it is the FIRST TWO BYTES of the
    feedId, which is itself the first word of the report body. That is why the
    decoder reads blob[:2] and then decodes the body from offset zero — there is
    nothing to skip. _FEED already begins 0x0003 for that reason.
    """
    feed_id = version.to_bytes(2, "big") + bytes.fromhex(_FEED[2:])[2:]
    body = abi_encode(
        [
            "bytes32",
            "uint32",
            "uint32",
            "uint192",
            "uint192",
            "uint32",
            "int192",
            "int192",
            "int192",
        ],
        [
            feed_id,
            observations_ts - 1,
            observations_ts,
            0,
            0,
            observations_ts + 60,
            price,
            price,
            price,
        ],
    )
    return abi_encode(["bytes32[3]", "bytes"], [[b"\x00" * 32] * 3, body])


class TestStringToSign:
    def test_elements_are_joined_with_single_spaces(self) -> None:
        """The documentation is explicit: single space, not newlines."""
        message = string_to_sign("GET", "/api/v1/ws?feedIDs=0x1", b"", _KEY, _TS)
        assert "\n" not in message
        assert message.count(" ") == 4

    def test_the_order_is_method_path_bodyhash_key_timestamp(self) -> None:
        method, path, body_hash, key, ts = string_to_sign(
            "GET", "/api/v1/ws?feedIDs=0x1", b"", _KEY, _TS
        ).split(" ")
        assert method == "GET"
        assert path == "/api/v1/ws?feedIDs=0x1"
        assert body_hash == hashlib.sha256(b"").hexdigest()
        assert key == _KEY
        assert ts == str(_TS)

    def test_a_get_still_carries_the_hash_of_the_empty_string(self) -> None:
        """Not an empty field, not omitted. Both produce the same opaque 401."""
        _, _, body_hash, _, _ = string_to_sign("GET", "/x", b"", _KEY, _TS).split(" ")
        assert body_hash == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_the_signed_path_includes_the_query_string(self) -> None:
        """Signing the bare path is a valid HMAC of the wrong request."""
        message = string_to_sign("GET", f"{CHAINLINK_WS_PATH}?feedIDs={_FEED}", b"", _KEY, _TS)
        assert f"?feedIDs={_FEED}" in message


class TestAuthHeaders:
    def test_the_three_documented_headers_are_present_and_named_exactly(self) -> None:
        headers = auth_headers(_KEY, _SECRET, "/api/v1/ws?feedIDs=0x1", _TS)
        assert set(headers) == {
            "Authorization",
            "X-Authorization-Timestamp",
            "X-Authorization-Signature-SHA256",
        }

    def test_the_signature_is_hex_hmac_sha256_not_base64(self) -> None:
        """A documented pitfall: base64 is the wrong encoding of a correct HMAC."""
        path = "/api/v1/ws?feedIDs=0x1"
        expected = hmac.new(
            _SECRET.encode(),
            string_to_sign("GET", path, b"", _KEY, _TS).encode(),
            hashlib.sha256,
        ).hexdigest()
        assert auth_headers(_KEY, _SECRET, path, _TS)["X-Authorization-Signature-SHA256"] == expected
        assert len(expected) == 64

    def test_the_timestamp_header_matches_the_signed_timestamp(self) -> None:
        """A header and a signature disagreeing by one millisecond is a 401 that
        looks exactly like a clock-skew problem."""
        headers = auth_headers(_KEY, _SECRET, "/x", _TS)
        assert headers["X-Authorization-Timestamp"] == str(_TS)

    def test_the_secret_is_never_the_authorization_header(self) -> None:
        headers = auth_headers(_KEY, _SECRET, "/x", _TS)
        assert headers["Authorization"] == _KEY
        assert _SECRET not in json.dumps(headers)


class TestDecodeFullReport:
    def test_a_v3_report_round_trips(self) -> None:
        decoded = decode_full_report(_full_report(64_195_856_404_915_870_000_000))
        assert decoded["price"] == 64_195_856_404_915_870_000_000
        assert decoded["feedId"] == _FEED

    def test_hex_text_with_and_without_the_0x_prefix_both_decode(self) -> None:
        raw = _full_report(1_000)
        assert decode_full_report("0x" + raw.hex())["price"] == 1_000
        assert decode_full_report(raw.hex())["price"] == 1_000

    def test_an_unsupported_schema_version_is_refused(self) -> None:
        """Not best-effort decoded. A v4 body read as v3 puts a plausible number
        in the price field."""
        with pytest.raises(FeedError) as exc:
            decode_full_report(_full_report(1_000, version=4))
        assert "v4" in str(exc.value)

    def test_a_non_hex_payload_is_refused(self) -> None:
        with pytest.raises(FeedError):
            decode_full_report("not-hex-at-all")

    def test_the_price_is_returned_unscaled(self) -> None:
        """Scaling is the caller's, because the scale is per-stream configuration.
        A decoder that applied a default would be the 10^10 error itself."""
        assert decode_full_report(_full_report(12345))["price"] == 12345


class TestConfigurationIsRefusedByName:
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"api_key": ""}, "ARC_CHAINLINK_API_KEY"),
            ({"api_secret": ""}, "ARC_CHAINLINK_API_SECRET"),
            ({"feed_id": ""}, "ARC_CHAINLINK_FEED_ID"),
            ({"decimals": 0}, "ARC_CHAINLINK_DECIMALS"),
        ],
    )
    def test_each_missing_item_is_named(self, kwargs: dict[str, Any], expected: str) -> None:
        base: dict[str, Any] = {
            "api_key": _KEY,
            "api_secret": _SECRET,
            "feed_id": _FEED,
            "decimals": 18,
            "symbol": "BTC/USD",
            "base_url": "wss://ws.dataengine.chain.link",
        }
        with pytest.raises(ConfigInvariantError) as exc:
            build_chainlink_feed(_clock(), **{**base, **kwargs})
        assert expected in str(exc.value)


class TestTranslation:
    def _feed(self, decimals: int) -> ChainlinkFeed:
        return ChainlinkFeed(
            _clock(),
            api_key=_KEY,
            api_secret=_SECRET,
            feed_id=_FEED,
            decimals=decimals,
            symbol="BTC/USD",
        )

    def test_a_report_becomes_an_rtds_shaped_tick(self) -> None:
        """The frame shape is identical to RTDS so nothing downstream of the
        provider can tell which one is live (A21)."""
        blob = _full_report(64_195_856_404_915_870_000_000)
        frame = json.dumps({"report": {"feedID": _FEED, "fullReport": "0x" + blob.hex()}})
        out = self._feed(18)._translate(frame)
        assert out is not None
        tick = json.loads(out)
        assert tick["symbol"] == "BTC/USD"
        assert tick["full_accuracy_value"] == "64195856404915870000000"

    def test_the_price_is_exact_integer_text_never_a_float(self) -> None:
        """validation.py refuses float prices by design. A translation that emitted
        a JSON number would make every Chainlink observation rejected, and the bot
        would accumulate no signal TWAP at all while the feed looked healthy."""
        blob = _full_report(1)
        frame = json.dumps({"report": {"feedID": _FEED, "fullReport": blob.hex()}})
        out = self._feed(18)._translate(frame)
        assert out is not None
        assert isinstance(json.loads(out)["full_accuracy_value"], str)

    def test_an_eight_decimal_stream_is_scaled_to_eighteen(self) -> None:
        """full_accuracy_value is defined as 18-decimal fixed point. An 8-decimal
        stream passed through unscaled reports BTC at 1/10^10 of its price."""
        blob = _full_report(6_419_585_664)  # 64195.85664 at 8 decimals
        frame = json.dumps({"report": {"feedID": _FEED, "fullReport": blob.hex()}})
        out = self._feed(8)._translate(frame)
        assert out is not None
        assert json.loads(out)["full_accuracy_value"] == str(6_419_585_664 * 10**10)

    def test_a_frame_with_no_report_is_dropped_not_raised(self) -> None:
        """One malformed report must not tear down a live connection."""
        assert self._feed(18)._translate('{"something": "else"}') is None
        assert self._feed(18)._translate("not json") is None

    def test_an_undecodable_report_is_dropped_not_raised(self) -> None:
        frame = json.dumps({"report": {"feedID": _FEED, "fullReport": "0xdeadbeef"}})
        assert self._feed(18)._translate(frame) is None


class TestEndpoint:
    def test_the_stream_ids_go_in_the_query_string(self) -> None:
        """There is no subscribe frame in this protocol. A client that waits to
        send one connects successfully and receives nothing."""
        feed = ChainlinkFeed(
            _clock(),
            api_key=_KEY,
            api_secret=_SECRET,
            feed_id=_FEED,
            decimals=18,
            symbol="BTC/USD",
        )
        assert feed.url == f"wss://ws.dataengine.chain.link{CHAINLINK_WS_PATH}?feedIDs={_FEED}"

    def test_the_default_origin_is_mainnet(self) -> None:
        feed = ChainlinkFeed(
            _clock(), api_key=_KEY, api_secret=_SECRET, feed_id="0x1", decimals=18, symbol="B"
        )
        assert feed.url.startswith("wss://ws.dataengine.chain.link")
