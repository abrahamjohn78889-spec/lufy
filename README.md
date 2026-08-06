# ARC

A deterministic trading bot for Polymarket's 5-minute BTC Up/Down markets. One
VPS, one Python process, one SQLite file, one strategy, no cluster.

## What it does

Every five minutes Polymarket opens a BTC Up/Down market with an official Price
To Beat. ARC accumulates a signal TWAP from a price feed, and at configured
seconds-before-close windows it freezes the PTB and the TWAP, determines a
direction, and submits a passive maker limit order if the frozen values clear the
configured buffer. It does not chase, it does not market-order, and it never
computes a PTB — the official value is fetched or the market is skipped.

## Requirements

- Python 3.12 or 3.13
- SQLite (stdlib)
- A Polymarket account with API credentials, for V2 only

```bash
pip install -e .
cp .env.example .env
```

## Configuration

Two layers, on purpose.

**Infrastructure** — bind address, port, database path, log directory, provider
URLs, credentials — is read from `.env` on every startup. Change the file,
restart the process.

**Trading** — windows, buffers, position size, limits, submission count — is read
from `.env` **only on the first run**, which seeds SQLite. After that SQLite is
the source of truth and is edited from the dashboard's Settings page. A stale
`.env` cannot silently revert a buffer the operator changed in the UI.

Precedence: **CLI > SQLite > .env > built-in defaults.** There are no built-in
defaults for trading values; a substituted buffer is indistinguishable from a
configured one.

Every setting is documented in [.env.example](.env.example). A test fails the
build if a field exists in code but not in that file — configuration reachable
only by reading source is configuration the operator will never find.

### Provider

`ARC_TWAP_PROVIDER=RTDS` (default) or `CHAINLINK`. One provider supplies all TWAP
data or none of it; there is no fallback, because trading against a different
price source than the dashboard names is worse than not trading.

Chainlink Data Streams is implemented against the official documented wire format
and is **configuration-complete but not yet validated against live Chainlink
credentials.** Supplying valid credentials requires no code change.

## Running

```bash
arc doctor
```

Validates configuration, storage, credentials (reported as SET or UNSET, never
printed) and clock, and prints a full report. Run it before anything else.

```bash
arc run --mode=v1
```

Starts the process with the V1 paper runtime up, and serves the dashboard on
`http://127.0.0.1:8080`.

The bind is loopback and a non-loopback `ARC_API_BIND` is refused at startup.
There is no login, no session, no token: the network boundary *is* the access
control. Reach it remotely over SSH:

```bash
ssh -L 8080:localhost:8080 user@vps
```

## The two lifecycles

These are separate, and confusing them is the failure the OPS Deck is laid out to
prevent.

### Runtime lifecycle

```
Select V1 or V2  →  START RUNTIME  →  Runtime READY  →  STOP RUNTIME
```

Selecting a mode starts nothing. **START RUNTIME** brings up the entire selected
system: provider, RTDS or Chainlink, market discovery, PTB discovery, signal
TWAP, settlement TWAP observation, CLOB, the official websockets, decision, risk
and limit order engines, recovery, recorder, statistics, Signal Tank, ledger,
Telegram and health monitoring. The **only** difference between V1 and V2 is the
execution adapter.

**STOP RUNTIME** disarms trading first, then shuts everything down. No websocket,
feed, worker, polling task, execution task, recorder or runtime service survives
it. The process returns to idle with the dashboard still serving — the dashboard
outlives every runtime, or you could never see that a stop succeeded.

V1 and V2 never run at once, and nothing is reused across a stop except the
SQLite file. Switching modes performs a full teardown and rebuilds the object
graph, so two runtimes cannot share execution state, orders, sockets, providers
or adapters.

V2 additionally requires preflight to pass and refuses to start naming the
failing checks.

### Trading lifecycle

```
Configure the Limit Order Engine  →  START TRADING  →  PAUSE / RESUME  →  STOP TRADING
```

**A running runtime is not a trading runtime.** `execution_armed` is FALSE after
every start and is never persisted, so a restart comes back disarmed — a gate
that survived a crash would re-arm a system nobody was watching.

- **START TRADING** arms the Limit Order Engine.
- **PAUSE TRADING** stops new ExecutionIntents. Resting orders continue to be
  managed to a terminal state; nothing is cancelled because you paused.
- **RESUME TRADING** continues, with no runtime restart.
- **STOP TRADING** disarms. Existing orders continue through reconciliation,
  fills, settlement and cleanup.

Trading buttons never touch the runtime. Runtime buttons never start trading.

## Three gates

| Gate | Owner | Persisted |
|---|---|---|
| `trading_enabled` | the system | yes |
| `execution_armed` | the operator | no, never |
| `paused` | the operator | no |

They are displayed independently. A single combined light would hide
system-disabled-while-armed, which is a real state meaning "the operator wants to
trade and the system is refusing."

## Three quantities that are never conflated

- **signal TWAP** — the 300 s cumulative mean ARC accumulates. Decides direction.
- **settlement TWAP** — the venue's official 30 s mean. Observational only.
- **PTB** — the official Price To Beat, fetched from market metadata, frozen once
  per market. Never calculated, estimated, interpolated or refreshed.

## API

Exactly twelve routes plus one WebSocket. No hidden, admin, diagnostic, export or
backup endpoints.

```
POST /start                              POST /stop
POST /pause                              POST /resume
GET  /status                             GET  /settings    POST /settings
GET  /history                            GET  /strategies
GET  /strategies/{id}                    GET|POST /strategies/{id}/config
GET  /backtest                           GET  /orderbook
WS   /ws
```

`/start` and `/stop` are the runtime lifecycle. Trading is armed through
`/strategies/{id}/config?action=arm|disarm`. Strategy *parameters* are not
writable there: one strategy is pinned, and buffers and windows are edited on the
Settings page.

## Transparency

Nothing happens silently. Every significant runtime event appears in the OPS
Deck, the Signal Tank, the Ledger, Telegram and the logs.

## Development

```bash
python -m ruff check . && python -m mypy arc && python -m pytest -q -p no:randomly
```

All three must pass. Work commits directly to `main`; there are no feature,
development or long-lived branches, and the repository stays production-ready.
