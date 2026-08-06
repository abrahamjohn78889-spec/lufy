# ARC — Architecture

One process, one SQLite file, one VPS. No queue, no broker, no second service, no
container orchestration. Everything below runs inside a single Python process and
the operator reaches it through one loopback HTTP port.

```
                    ┌──────────────────────────────────────────┐
  browser ── ws ───▶│  FastAPI app  (loopback only)            │
   (SSH tunnel)     │    twelve routes + /ws                   │
                    │    ┌──────────────────────────────────┐  │
                    │    │  RuntimeSupervisor               │  │
                    │    │    holds ONE ArcRuntime          │  │
                    │    │    START / STOP  →  rebuild it   │  │
                    │    └───────────────┬──────────────────┘  │
                    └────────────────────┼─────────────────────┘
                                         ▼
       ┌───────────────────────── ArcRuntime ────────────────────────────┐
       │ Market Discovery → Rotator → Window Engine → Decision Engine    │
       │        │              │            │              │             │
       │   PTB (frozen)   Signal TWAP  Settlement TWAP   Risk Engine     │
       │                       ▲                             │           │
       │                  Provider feed                      ▼           │
       │              (RTDS or Chainlink, never both)  Limit Order Engine│
       │                                                     │           │
       │  Recovery · Recorder · Statistics · Watchdog         ▼          │
       │                                            V1 Paper / V2 Live   │
       └──────────────────┬──────────────────────────────┬───────────────┘
                          ▼                              ▼
                    EventHub (Signal Tank)          SQLite (arc.db)
                          │                              ▲
             ┌────────────┼────────────┐                 │
             ▼            ▼            ▼            write-before-act
       dashboard      Telegram      log file
```

## The layers

| Package | Owns |
|---|---|
| `arc/config.py` | two-layer settings: `.env` for infrastructure, SQLite for trading |
| `arc/clock.py` | the only source of time; drift measurement |
| `arc/domain/` | enums and value objects. No I/O, no behaviour that reads the world |
| `arc/market/` | discovery, the provider feed, PTB, rotation, the watchdog |
| `arc/windows/` | window lifecycle: open → freeze → fire → expire |
| `arc/decision/` | the TWAP-vs-PTB comparison. Pure, given a window |
| `arc/risk/` | the gates. Nothing is submitted that this did not pass |
| `arc/execution/` | the Limit Order Engine, fills, reconciliation, wallet |
| `arc/runtime/` | the loop, the supervisor, RuntimeState, the EventHub, the ledger |
| `arc/storage/` | the store. Every schema change is a numbered migration |
| `arc/api/` | twelve routes, the payload builders, the WebSocket |
| `arc/notify/` | Telegram. Outbound only |
| `arc/web/` | the dashboard: three static files, no build step |

Dependencies point one way: `domain` ← `market`/`windows`/`decision`/`risk`/
`execution` ← `runtime` ← `api`. Nothing in the strategy path knows which
provider is feeding it — enforced as a test, not a convention.

## The two lifecycles

They are independent, and conflating them is the failure mode the deck is
designed against.

**Runtime lifecycle** — START RUNTIME brings up provider, feed, discovery, PTB,
TWAP accumulators, CLOB, websockets, recovery, recorder, statistics, Signal
Tank, Telegram, ledger and every engine. STOP RUNTIME takes all of it down.
Selecting V1 or V2 starts nothing; it only selects.

**Trading lifecycle** — START TRADING arms the Limit Order Engine. PAUSE holds
new intents while resting orders continue to be managed. RESUME allows new
intents again and never restarts the runtime. STOP TRADING disarms; orders
already at the venue continue through reconciliation, fill and settlement.

Stopping the runtime always disarms trading first, as its own step, before
anything is cancelled.

## Rebuilt, never reused

`RuntimeSupervisor.start()` constructs a new `ArcRuntime`, feed, executor, venue
client and HTTP client every time. A stop destroys them. That is the whole of the
V1/V2 isolation guarantee: the two modes cannot share execution state, order ids,
sockets or wallet sessions, because after a stop none of those objects exist.
Only persistent SQLite state survives a switch.

Between runs a deliberately inert runtime exists so every dashboard panel has
something to read. It opens no sockets and holds no venue session.

## One event stream

`log_event` is the only publisher. A `SignalTankHandler` on the `arc` logger
turns each call into an OPS Deck line, a Signal Tank event, a Telegram message
and a log-file record together. There is no second publish path, because two
taps eventually disagree about what happened.

Telegram subscribes to that stream rather than being called from the emitting
sites, so a log line added later still reaches the operator. Twenty-six
independently toggleable categories map from the event label, with a fallback
on severity for anything unmapped — a new event surfaces as a Warning or a Fatal
Error rather than vanishing because nobody extended a table. Outbound only: no
polling, no webhook, no command handling, because a chat message that could move
money would make the Telegram account a second set of trading credentials.

## One clock, three renderings

UTC is the canonical timestamp and the only one stored. `arc/timefmt.py` derives
IST (`Asia/Kolkata`) and ET (`America/New_York`) from it at render time, by named
zone rather than fixed offset, so DST cannot desynchronise them and a replay
shows the wall clock the live run showed. All three appear together on the OPS
Deck, Limit Order Engine, Ledger, Trade History, Runtime Events, Signal Tank,
Telegram and the Production Validation Report; UTC is kept in the display because
it is the value the database is keyed on.

No derived value re-enters the trading path. A test fails if `decision`, `risk`,
`windows`, `strategy`, `execution` or `domain` so much as imports the formatter.

## What is measured, and what is not

Latency is reported only where a timestamp pair actually exists: submission
(`order.created_at` → `order.updated_at`) and fill (`order.created_at` → the
first fill row). Websocket frames, CLOB calls and provider responses are not
individually timestamped, so those figures print
`UNAVAILABLE (not instrumented)`. Host CPU, memory, disk and network are not
sampled at all. A number invented beside measured ones is read as measured.

Reconnects, dropped sockets and recoveries are counted separately: a drop is a
socket that was up and went down, a reconnect is one attempt by the ladder, and
one outage can produce many attempts.

## Three quantities that are never conflated

- `signal_twap` — 300 s cumulative mean from the provider. Drives decisions.
- `settlement_twap` — the venue's own 30 s value. Observational only.
- `ptb` — the official Price To Beat from Polymarket metadata. Frozen once per
  market, never calculated, estimated, interpolated or refreshed.

## Three gates

| Gate | Owner | Persisted |
|---|---|---|
| `trading_enabled` | the system (risk, spec check, recovery) | yes |
| `execution_armed` | the operator | never — a restart comes back disarmed |
| `_paused` | the operator | no |

## Access control is the network boundary

The API binds loopback and refuses to start on any other interface. There is no
login, no session, no token, no role. Remote access is an SSH tunnel:

```bash
ssh -L 8080:localhost:8080 user@vps
```

## Durability

Every order row is written PENDING before the venue call. A connection lost
mid-submit leaves the order INDETERMINATE, and reconciliation — never a retry —
decides what happened. A blind retry can double-fill.
