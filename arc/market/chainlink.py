"""Chainlink Data Streams websocket. The second TWAP provider.

Every detail below is taken from the official Data Streams documentation and
nothing is inferred. The four that a conventional websocket client gets wrong:

    1. AUTHENTICATION IS PER-HANDSHAKE, NOT PER-MESSAGE. Three headers are sent
       with the HTTP upgrade request; there is no subscribe frame and no login
       message. The streams are named in the query string.

    2. THE STRING-TO-SIGN IS SPACE-JOINED, NOT NEWLINE-JOINED. The documentation
       states the elements are "joined with a single space character, not
       newlines". A newline-joined signature is a well-formed HMAC of the wrong
       string, so the handshake fails 401 and reads as a bad credential.

    3. A GET STILL CARRIES A BODY HASH. It is the SHA-256 of the EMPTY string,
       hex-encoded — not an empty field and not omitted. Omitting it produces the
       same indistinguishable 401.

    4. THE FRAME CARRIES NO PRICE. The payload is
       {"report": {"feedID": ..., "fullReport": ...}} and `fullReport` is an
       ABI-encoded blob. There is no decoded numeric field anywhere in the frame,
       so a client that looks for `price` finds nothing and sees a live connection
       delivering no data — which is indistinguishable from a quiet market.

DECIMALS ARE NOT INFERRED. The documentation says prices "use 8 or 18 decimals
depending on the stream", so the scale is configuration (ARC_CHAINLINK_DECIMALS)
and is fatal when unset. Defaulting to 18 for an 8-decimal stream reports BTC at
1e10 times its real price, and nothing downstream can catch it: the deviation
check compares each sample to the previous one, and every sample would be wrong
by the same factor.

VERIFICATION IS OFF-CHAIN HERE. The documentation notes that production use
should verify reports on-chain through the verifier proxy. ARC decodes the blob
locally and does not verify signatures. That is a real limitation and it is
stated on the dashboard rather than hidden: the report is trusted because the
TLS connection to the Data Streams endpoint is trusted, which is the same trust
ARC already places in the RTDS relay.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Final

import websockets
from eth_abi.abi import decode as abi_decode

from arc.clock import Clock
from arc.errors import ConfigInvariantError, FeedError
from arc.market.feed import BackoffPolicy, ReconnectingStream

__all__ = [
    "CHAINLINK_WS_PATH",
    "ChainlinkFeed",
    "auth_headers",
    "decode_full_report",
    "string_to_sign",
]

CHAINLINK_WS_PATH: Final[str] = "/api/v1/ws"

# The v3 "Crypto Advanced" report body, in declaration order. Types are from the
# official schema page and are NOT interchangeable: int192 is signed, and reading
# `price` as uint192 would turn a negative report into an astronomically large
# positive one instead of a rejection.
_V3_TYPES: Final[tuple[str, ...]] = (
    "bytes32",  # feedId
    "uint32",  # validFromTimestamp
    "uint32",  # observationsTimestamp
    "uint192",  # nativeFee
    "uint192",  # linkFee
    "uint32",  # expiresAt
    "int192",  # price
    "int192",  # bid
    "int192",  # ask
)

# The outer envelope: three context words then the report body. The body's first
# two bytes are the schema version.
_ENVELOPE_TYPES: Final[tuple[str, ...]] = ("bytes32[3]", "bytes")

_SCHEMA_V3: Final[int] = 3


def string_to_sign(
    method: str, full_path: str, body: bytes, api_key: str, timestamp_ms: int
) -> str:
    """The exact documented concatenation: METHOD PATH BODY_HASH KEY TIMESTAMP.

    Single spaces. The body hash is present even for GET, where it is the hash of
    the empty string — an omitted hash and a hash-of-empty are different strings
    and produce different signatures, both of which fail as an opaque 401.
    """
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()} {full_path} {body_hash} {api_key} {timestamp_ms}"


def auth_headers(
    api_key: str, api_secret: str, full_path: str, timestamp_ms: int, *, method: str = "GET"
) -> dict[str, str]:
    """The three required headers. Hex digest, not base64 (documented pitfall)."""
    message = string_to_sign(method, full_path, b"", api_key, timestamp_ms)
    signature = hmac.new(
        api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "Authorization": api_key,
        "X-Authorization-Timestamp": str(timestamp_ms),
        "X-Authorization-Signature-SHA256": signature,
    }


def decode_full_report(full_report: str | bytes) -> dict[str, Any]:
    """Decode one `fullReport` blob into its v3 fields. Raises FeedError otherwise.

    The price is returned as a raw INTEGER, unscaled. Scaling belongs to the
    caller because the scale is per-stream configuration, and a function that
    silently applied a default would be the exact 1e10 error this module refuses
    to make possible.
    """
    if isinstance(full_report, str):
        text = full_report[2:] if full_report.startswith("0x") else full_report
        try:
            raw = bytes.fromhex(text)
        except ValueError as exc:
            raise FeedError(f"fullReport is not hex: {exc}") from exc
    else:
        raw = full_report

    try:
        _context, blob = abi_decode(_ENVELOPE_TYPES, raw)
    except Exception as exc:  # eth_abi raises several unrelated types
        raise FeedError(f"fullReport envelope did not decode: {exc}") from exc

    if len(blob) < 2:
        raise FeedError("report blob is too short to carry a schema version")
    version = int.from_bytes(blob[:2], "big")
    if version != _SCHEMA_V3:
        # Refused, not best-effort decoded. A v4 body read with the v3 layout
        # yields plausible numbers in the wrong fields, and the first one is a
        # price.
        raise FeedError(
            f"report schema v{version} is not supported; ARC decodes v{_SCHEMA_V3} only"
        )

    try:
        fields = abi_decode(_V3_TYPES, blob)
    except Exception as exc:
        raise FeedError(f"v3 report body did not decode: {exc}") from exc

    return {
        "feedId": "0x" + fields[0].hex(),
        "validFromTimestamp": fields[1],
        "observationsTimestamp": fields[2],
        "expiresAt": fields[5],
        "price": fields[6],
        "bid": fields[7],
        "ask": fields[8],
    }


def _ws_target(base_url: str, feed_id: str) -> tuple[str, str]:
    """(url, full_path). The path INCLUDING the query is what gets signed."""
    full_path = f"{CHAINLINK_WS_PATH}?feedIDs={feed_id}"
    return base_url.rstrip("/") + full_path, full_path


class ChainlinkFeed:
    """One Data Streams connection. Yields frames shaped like RTDS frames.

    The translation to the RTDS envelope happens HERE, at the boundary, and not
    in validation.py. Everything downstream of a provider — PTB, windows,
    decision, risk, execution, dashboard — must remain unable to tell which
    provider is live (A21), so the alternative would be provider-shaped branches
    in the strategy path, which is exactly the mixed-provider operation the
    specification forbids.
    """

    __slots__ = (
        "_api_key",
        "_api_secret",
        "_clock",
        "_connect",
        "_decimals",
        "_feed_id",
        "_full_path",
        "_logger",
        "_stream",
        "_symbol",
        "_url",
        "connect_attempts",
        "connected",
        "messages_received",
    )

    def __init__(
        self,
        clock: Clock,
        *,
        api_key: str,
        api_secret: str,
        feed_id: str,
        decimals: int,
        symbol: str,
        base_url: str = "wss://ws.dataengine.chain.link",
        connect: Callable[[str, dict[str, str]], Awaitable[Any]] | None = None,
        backoff: BackoffPolicy | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._clock = clock
        self._api_key = api_key
        self._api_secret = api_secret
        self._feed_id = feed_id
        self._decimals = decimals
        self._symbol = symbol
        self._url, self._full_path = _ws_target(base_url, feed_id)
        self._connect = connect if connect is not None else _default_connect
        self._logger = logger
        self._stream = ReconnectingStream(
            self._open, name="Chainlink", url=self._url, backoff=backoff, logger=logger
        )
        self.connected = False
        self.connect_attempts = 0
        self.messages_received = 0

    @property
    def url(self) -> str:
        return self._url

    async def _open(self) -> Any:
        self.connect_attempts += 1
        # Signed at connect time, not at construction: the timestamp must be
        # within 5 seconds of server time, so a signature built once and reused
        # across a reconnect ladder is rejected the moment the first retry waits
        # longer than that.
        timestamp_ms = int(self._clock.now() * 1000)
        headers = auth_headers(self._api_key, self._api_secret, self._full_path, timestamp_ms)
        socket = await self._connect(self._url, headers)
        self.connected = True
        return socket

    def _translate(self, frame: str | bytes) -> str | None:
        """One Data Streams frame → one RTDS-shaped tick, or None to drop it."""
        try:
            payload = json.loads(frame)
        except ValueError:
            return None
        report = payload.get("report") if isinstance(payload, dict) else None
        if not isinstance(report, dict) or "fullReport" not in report:
            return None
        try:
            decoded = decode_full_report(report["fullReport"])
        except FeedError:
            # Counted as a received-but-unusable frame by the caller's own
            # rejection counter once it fails to parse. Dropped rather than
            # raised: one malformed report must not tear down a live connection.
            return None

        # full_accuracy_value is exact integer TEXT, which is what validation.py
        # requires and why no float is constructed anywhere on this path. The
        # scale is normalised to the 18 decimals that field is defined to carry.
        raw = int(decoded["price"])
        scaled = raw * 10 ** (18 - self._decimals) if self._decimals <= 18 else raw
        return json.dumps(
            {
                "symbol": self._symbol,
                "timestamp": int(decoded["observationsTimestamp"]),
                "full_accuracy_value": str(scaled),
                "feedId": decoded["feedId"],
            }
        )

    async def messages(self) -> AsyncIterator[str | bytes]:
        async for frame in self._stream.frames():
            self.messages_received += 1
            self.connected = self._stream.connected
            translated = self._translate(frame)
            if translated is not None:
                yield translated
        self.connected = False


async def _default_connect(url: str, headers: dict[str, str]) -> Any:
    return await websockets.connect(url, additional_headers=headers, open_timeout=15.0)


def build_chainlink_feed(
    clock: Clock,
    *,
    api_key: str,
    api_secret: str,
    feed_id: str,
    decimals: int,
    symbol: str,
    base_url: str,
    backoff: BackoffPolicy | None = None,
    logger: logging.Logger | None = None,
) -> ChainlinkFeed:
    """Construct the feed, or refuse with the missing item named.

    Each refusal names the one variable to set. "Chainlink is misconfigured" sends
    the operator to source; "ARC_CHAINLINK_DECIMALS is unset" does not.
    """
    if not api_key:
        raise ConfigInvariantError("TWAP_PROVIDER=CHAINLINK requires ARC_CHAINLINK_API_KEY")
    if not api_secret:
        raise ConfigInvariantError("TWAP_PROVIDER=CHAINLINK requires ARC_CHAINLINK_API_SECRET")
    if not feed_id:
        raise ConfigInvariantError(
            "TWAP_PROVIDER=CHAINLINK requires ARC_CHAINLINK_FEED_ID — the official "
            "stream ID for this market. ARC does not guess one: a wrong stream "
            "delivers real, correctly-signed prices for the wrong asset."
        )
    if decimals <= 0:
        raise ConfigInvariantError(
            "TWAP_PROVIDER=CHAINLINK requires ARC_CHAINLINK_DECIMALS. Data Streams "
            "prices use 8 or 18 decimals depending on the stream; the value is not "
            "carried in the report, and assuming the wrong one misreports the price "
            "by a factor of 10^10 with every sample wrong by the same factor, so "
            "nothing downstream can detect it."
        )
    return ChainlinkFeed(
        clock,
        api_key=api_key,
        api_secret=api_secret,
        feed_id=feed_id,
        decimals=decimals,
        symbol=symbol,
        base_url=base_url,
        backoff=backoff,
        logger=logger,
    )
