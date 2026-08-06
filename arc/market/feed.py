"""RTDS websocket: the Chainlink price stream.

Four properties of this relay that a conventional websocket client gets wrong (A5):

    1. NO subscribe filters. The topic is subscribed whole. Sending a filter list
       is not merely ignored — it produces a subscription that delivers nothing, and
       the connection stays open, so the failure looks like a quiet market.
    2. The keepalive is the LITERAL STRING "PING", not a JSON frame and not a
       websocket ping opcode. A JSON {"type":"ping"} is discarded and the relay
       closes the connection on its own idle timer.
    3. NO snapshot on connect. The first message arrives with the next tick. Code
       that waits for an initial state before declaring the feed up waits forever.
    4. The stale threshold is 600,000 ms — ten minutes. That is the relay's own
       notion of a dead subscription, not ARC's staleness policy, which is far
       tighter and lives in watchdog.py.

TRAP 1: the interval between messages is NEVER used to infer the TWAP window
length, and never used as a health check for it. This module measures gaps only to
decide whether data is arriving. `windowSeconds` is a field in the payload and is
read as a field; see settlement_feed.py.

This module holds no notion of a market boundary. It once tracked whether the
connection spanned each 300s boundary, to gate a PTB source that read the price
observed at that instant. That source is gone: live measurement showed the observed
boundary price differs from the venue's published close price, so it was an estimate,
and A1 forbids estimating the PTB. The official value now comes from the venue's own
published `finalPrice` (see ptb.py), which no property of this connection can affect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import websockets

from arc.clock import Clock
from arc.errors import ConnectionLostError, FeedError
from arc.logging_setup import log_event

__all__ = [
    "CHAINLINK_TOPIC",
    "CHAINLINK_TYPE",
    "KEEPALIVE_FRAME",
    "RTDS_URL",
    "SDK_STALE_THRESHOLD_MS",
    "BackoffPolicy",
    "ReconnectingStream",
    "RtdsFeed",
    "subscribe_frame",
]

RTDS_URL: Final[str] = "wss://ws-live-data.polymarket.com"
CHAINLINK_TOPIC: Final[str] = "crypto_prices_chainlink"

# Required alongside the topic. Established against the live relay on 2026-08-05:
# a subscription carrying only `topic` is answered `{"message": "Invalid request
# body"}`, and probing made the relay leak its own lookup —
#
#     leger GetTopics error: rpc error: code = NotFound desc =
#     topic: crypto_prices_chainlink and type: crypto_prices_chainlink not found
#
# — which shows the relay keys subscriptions on the (topic, type) PAIR and, absent a
# type, substitutes the topic for it and finds nothing. The failure this prevents is
# the worst shape available: the connection opens, the subscribe is rejected, and no
# price ever arrives, which is indistinguishable from a quiet market.
CHAINLINK_TYPE: Final[str] = "update"

# Literal text, not JSON. See property 2 in the module docstring.
KEEPALIVE_FRAME: Final[str] = "PING"

# The relay's own threshold for calling a subscription dead. Recorded here because
# it explains the keepalive cadence below; ARC's trading staleness policy is
# unrelated and much tighter (watchdog.py).
SDK_STALE_THRESHOLD_MS: Final[int] = 600_000

# Comfortably inside any plausible relay idle timeout while adding negligible
# traffic. A keepalive tuned close to the timeout turns one dropped frame into a
# disconnect.
_KEEPALIVE_INTERVAL_SECONDS: Final[float] = 20.0

_CONNECT_TIMEOUT_SECONDS: Final[float] = 15.0


def subscribe_frame() -> dict[str, Any]:
    """The subscribe message. Carries the topic and its type, and NOTHING else.

    No `filters`, no `symbols`, no `assets` key. Adding one produces a subscription
    that delivers no messages on an otherwise healthy connection (property 1).

    `type` is not a filter — it is half of the relay's subscription key, and the
    subscribe is rejected outright without it (see CHAINLINK_TYPE).
    """
    return {
        "action": "subscribe",
        "subscriptions": [{"topic": CHAINLINK_TOPIC, "type": CHAINLINK_TYPE}],
    }


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Bounded exponential backoff for reconnection.

    Bounded on purpose. Unbounded backoff eventually reaches delays measured in
    minutes, and a bot that takes four minutes to notice the feed came back has
    missed most of a session while reporting that it is reconnecting.
    """

    initial_seconds: float = 0.5
    max_seconds: float = 30.0
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.initial_seconds <= 0:
            raise ValueError(f"initial_seconds must be positive, got {self.initial_seconds}")
        if self.max_seconds < self.initial_seconds:
            raise ValueError(
                f"max_seconds ({self.max_seconds}) must not be below "
                f"initial_seconds ({self.initial_seconds})"
            )
        if self.multiplier <= 1.0:
            raise ValueError(f"multiplier must exceed 1, got {self.multiplier}")

    def delay_for(self, attempt: int) -> float:
        """Delay before attempt N (1-based). Capped at max_seconds."""
        if attempt < 1:
            raise ValueError(f"attempt is 1-based, got {attempt}")
        delay = self.initial_seconds * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_seconds)


class ReconnectingStream:
    """A frame iterator that reconnects forever with bounded backoff.

    Extracted from RtdsFeed so the Chainlink provider reuses the identical ladder
    rather than growing a second one. Two implementations of "reconnect" would
    eventually disagree about when to give up, and the one that gave up would do
    so on the provider nobody was watching.

    The opener is a callable returning a connected socket, so what differs between
    providers — a subscribe frame here, three signed headers there — stays in the
    provider and never in the retry logic.
    """

    __slots__ = ("_backoff", "_logger", "_name", "_open", "_url", "connected")

    def __init__(
        self,
        opener: Callable[[], Awaitable[Any]],
        *,
        name: str,
        url: str,
        backoff: BackoffPolicy | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._open = opener
        self._name = name
        self._url = url
        self._backoff = backoff if backoff is not None else BackoffPolicy()
        self._logger = logger
        self.connected = False

    async def frames(self) -> AsyncIterator[str | bytes]:
        attempt = 0
        while True:
            socket: Any = None
            try:
                socket = await self._open()
                self.connected = True
                attempt = 0
                log_event(logging.INFO, f"{self._name} Connected", self._url, logger=self._logger)
                async for frame in socket:
                    yield frame
                raise ConnectionLostError(f"{self._url} closed the stream")
            except asyncio.CancelledError:
                raise
            except (ConnectionLostError, FeedError, OSError, websockets.WebSocketException) as exc:
                self.connected = False
                attempt += 1
                delay = self._backoff.delay_for(attempt)
                log_event(
                    logging.WARNING,
                    f"{self._name} Disconnected",
                    f"{exc}  reconnecting in {delay:.1f}s (attempt {attempt})",
                    logger=self._logger,
                )
                await asyncio.sleep(delay)
            finally:
                if socket is not None:
                    with contextlib.suppress(Exception):
                        await socket.close()


class RtdsFeed:
    """One RTDS connection. Yields raw payloads; validates nothing.

    Parsing and validation live in validation.py so that a payload the relay
    changes the shape of is rejected in one place with a reason, rather than raising
    an attribute error somewhere inside the read loop.

    The connector is injected so the reconnect ladder and the keepalive can both be
    driven from a test without a socket.
    """

    __slots__ = (
        "_backoff",
        "_clock",
        "_connect",
        "_keepalive_seconds",
        "_logger",
        "_url",
        "connect_attempts",
        "connected",
        "keepalives_sent",
        "messages_received",
    )

    def __init__(
        self,
        clock: Clock,
        *,
        url: str = RTDS_URL,
        connect: Callable[[str], Awaitable[Any]] | None = None,
        backoff: BackoffPolicy | None = None,
        keepalive_seconds: float = _KEEPALIVE_INTERVAL_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._clock = clock
        self._url = url
        self._connect = connect if connect is not None else _default_connect
        self._backoff = backoff if backoff is not None else BackoffPolicy()
        self._keepalive_seconds = keepalive_seconds
        self._logger = logger
        self.connected = False
        self.connect_attempts = 0
        self.messages_received = 0
        self.keepalives_sent = 0

    @property
    def url(self) -> str:
        return self._url

    @property
    def backoff(self) -> BackoffPolicy:
        return self._backoff

    async def _open(self) -> Any:
        self.connect_attempts += 1
        try:
            socket = await asyncio.wait_for(
                self._connect(self._url), timeout=_CONNECT_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise FeedError(f"connecting to {self._url} timed out") from exc
        except OSError as exc:
            raise FeedError(f"connecting to {self._url} failed: {exc}") from exc
        except websockets.WebSocketException as exc:
            raise FeedError(f"connecting to {self._url} failed: {exc}") from exc

        # Subscribe immediately. There is no snapshot to wait for (property 3), so a
        # connection that has subscribed is a connection that is up.
        await socket.send(json.dumps(subscribe_frame()))
        self.connected = True
        log_event(logging.INFO, "Feed Connected", self._url, logger=self._logger)
        return socket

    async def _keepalive_loop(self, socket: Any) -> None:
        """Send the literal "PING" on a timer until cancelled (property 2)."""
        while True:
            await asyncio.sleep(self._keepalive_seconds)
            await socket.send(KEEPALIVE_FRAME)
            self.keepalives_sent += 1

    async def messages(self) -> AsyncIterator[str | bytes]:
        """Yield raw frames forever, reconnecting with bounded backoff.

        A dropped connection is logged and retried rather than raised: the caller
        wants the stream to continue, and a reconnect is a normal event on a 24/7 run.
        Only the caller's own cancellation ends this loop.
        """
        attempt = 0
        while True:
            socket: Any = None
            keepalive: asyncio.Task[None] | None = None
            try:
                socket = await self._open()
                attempt = 0
                keepalive = asyncio.create_task(self._keepalive_loop(socket))
                async for frame in socket:
                    self.messages_received += 1
                    # The relay echoes the keepalive. Swallowed here so a caller
                    # never has to know the keepalive exists to parse the stream.
                    if isinstance(frame, str) and frame.strip().upper() in ("PING", "PONG"):
                        continue
                    yield frame
                raise ConnectionLostError(f"{self._url} closed the stream")
            except asyncio.CancelledError:
                raise
            except (ConnectionLostError, FeedError, OSError, websockets.WebSocketException) as exc:
                self.connected = False
                attempt += 1
                delay = self._backoff.delay_for(attempt)
                log_event(
                    logging.WARNING,
                    "Feed Disconnected",
                    f"{exc}  reconnecting in {delay:.1f}s (attempt {attempt})",
                    logger=self._logger,
                )
                await asyncio.sleep(delay)
            finally:
                if keepalive is not None:
                    keepalive.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await keepalive
                if socket is not None:
                    with contextlib.suppress(Exception):
                        await socket.close()


async def _default_connect(url: str) -> Any:
    """Open a real websocket. Replaced by an injected connector in tests."""
    return await websockets.connect(url, open_timeout=_CONNECT_TIMEOUT_SECONDS)
