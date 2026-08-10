---
name: arc
description: Engineering rules, verification gates, and the phase workflow for the ARC Polymarket trading bot. Use for ANY work in this repository — reading, building, testing, reviewing, or deploying. Covers the A0–A21 constitution, the two-mode runtime, the frozen-trading-logic rule, the validation gate, and the exact commands that must pass before any change is called done.
---

# ARC

Deterministic trading bot for Polymarket 5-minute BTC Up/Down markets. One VPS,
one Python 3.12/3.13 process, one SQLite file. Long-term production system, not
a prototype.

Read this before writing code in this repo. Every rule below has cost real money
or real debugging time to establish.

## The two rules that override everything

**1. Ask before touching trading behaviour (A0).** Trading strategy, execution
logic, engine behaviour, order logic, risk logic, new trading features — stop and
ask. Do not infer permission from a prompt that merely touches the area.

**2. No placeholders (A1).** No TODOs, fake implementations, mock APIs, sample
logic, demo code, simplified versions, proof of concepts. Every function real.
A stub in a money path is worse than no code, because it looks finished.

## Official sources only

Only official Polymarket and official Chainlink documentation. **Never invent
feed IDs, endpoints, payload shapes, or behaviour.** Prefer official SDKs over
unofficial libraries (the official SDK is `polymarket-client`, imports as
`polymarket`). Verify breaking changes before modifying code against an SDK.

If an official API for a field does not exist, display the literal string
`UNAVAILABLE (Official API not available)`. Never fabricate, estimate, derive
heuristically, or interpolate a value the venue did not give you. This applies
to wallet balances, host facts, and everything in between.

## Architecture invariants

**Two runtime modes only.** V1 = paper, V2 = live. There is no third mode; the
word "observe" appears nowhere in the production runtime except where it refers
internally to receiving market data. V1 runs the COMPLETE pipeline — it is not a
lightweight simulator. The only component that differs is
`PaperExecutor` → `LiveExecutor` across `arc/execution/protocol.py`.
Status values: `STOPPED` `STARTING` `RUNNING (V1)` `RUNNING (V2)` `STOPPING`.
Nothing else.

**Three independent flags, never conflated:**
- `trading_enabled` — the SYSTEM gate. Controlled only by ARC, persisted. The
  operator can never override it.
- `execution_armed` — the OPERATOR gate. Start/Stop Trading only. Defaults FALSE
  after every startup.
- `_paused` — holds new submissions without disarming.

Risk requires `trading_enabled AND execution_armed` before an ExecutionIntent may
become an order. Runtime running ≠ trading running. Stopping trading stops NEW
submissions and nothing else — feeds, WebSocket, TWAP, PTB observation, recovery
and the ledger all keep running.

**Three quantities that must never be conflated:**
- `signal_twap` — ARC's own 300 s cumulative mean.
- `settlement_twap` — the venue's 30 s mean. Observational.
- `ptb` — the official frozen reference. **Never calculated, estimated,
  interpolated, inferred or refreshed** (A20). Fetched from official market
  metadata, frozen once per market; a second freeze raises.

**Strategy: exactly one** — `arc_twap_locked_buffer` (A17). Pinned, enabled,
not disableable, no selector, no dropdown, no ranking, no scoring, no ML, no
recommendation engine. A strategy is a **pure function**: no I/O, no clock, no
socket, no DB; it cannot place, cancel, reprice or size orders and cannot bypass
Risk. Every strategy parameter is configurable — never hardcode one. Additional
strategies are deferred, not cancelled; the gate is 100+ real markets of V1 data.

**Write-before-act (A4).** Every order row is persisted PENDING before the venue
call. **Level-triggered convergence (A12):** `advance()` / `pass_over()` are
idempotent, 200 ms tick. **MarketInstance (A11):** no `reset()`, at most two
live, a new market is a new object.

**No authentication anywhere (A3).** No auth, login, users, sessions, JWT, OAuth,
RBAC, RLS, secrets manager, audit_log. Loopback bind replaces authentication
(A4/A8): a non-loopback `API_BIND` is refused at startup. Remote access is
`ssh -L 8080:localhost:8080 user@vps`. There is no TESTNET.

## Error handling that matters (A14)

```
ConnectionLostError
  Operations in flight when the connection was lost have an INDETERMINATE
  outcome. Affected orders become INDETERMINATE and are resolved by
  RECONCILIATION. NEVER blind-retry — a blind retry can DOUBLE-FILL.

"Global Rate Limit Exceeded"
  This is a TRANSIENT LATENCY REJECT, not a rate limit.
  Retry WITHOUT backoff. Backoff here loses the window entirely.
```

`ConfigInvariantError` is **not** an `ArcError`. `ArcFatalError` →
`ConfigInvariantError`, `BindAddressError`, `SchemaMigrationError`.

## The API surface — exactly twelve routes (A15)

```
CONTROL     /start  /pause  /resume  /stop
STATE       /status  /settings  /history
LIVE        /ws
STRATEGY    /strategies  /strategies/{id}  /strategies/{id}/config
RESEARCH    /backtest  /orderbook
```

There is **no** `/health` (PM2 handles restarts). No hidden, debug, admin,
backup, export or diagnostics endpoints. New capability goes on an existing route
as a query parameter — `/history?q=&format=csv`, `/settings?action=backup`,
`/settings?snapshot=list`. The exactly-twelve criterion must keep passing.

**Decimal contract:** every Decimal crossing the API boundary is serialized as a
STRING, never a JSON number. Counts and timestamps stay numbers — `"12"` sorting
before `"9"` is the opposite bug.

## Dashboard rules

The dashboard MUST NEVER calculate Direction, PTB, Signal TWAP, Settlement TWAP,
Buffer, Trigger, Strategy, Execution or Risk. It visualizes backend state. **No
duplicated business logic inside the UI** — a number the frontend could compute
is a number that can disagree with the engine.

- **One status document**, assembled from one read of runtime state, pushed over
  `/ws`. Never poll continuously. Per-panel fetches show different instants and a
  market boundary between two fetches renders one market's PTB against another's
  TWAP.
- **Stale is never live.** On socket loss grey the whole document at once, plus a
  watchdog — a half-open socket never fires `onclose`, and the last frame would
  otherwise stay lit with LIVE showing.
- Submitted ≠ Filled. Separate boxes, separately coloured.
- `BUFFER_NOT_SATISFIED` is its own outcome — never a rejection, never a fill,
  never hidden.
- Rejection **reason** is a separate field from order **state**. Read the stable
  internal code; never depend on venue error text. The reason survives restart.
- `NO_DIRECTION` is displayed clearly and never replaced with an inferred one.
- The Unified Ledger is the only history. There is no Orders page and no Trade
  History page. Records update in place; nothing disappears.
- Countdown: real `MM:SS`, server-synced, **floored** (04:59 for the whole 299th
  second), **never negative**. Two timers, one `countdown()` call, one skew — two
  sources drift.
- No derived-math displays on live panels.

## Backtesting is a viewer, not a backtester (A18)

Build: cached historical candles · signal replay · trigger visualization.
Do **not** build win rate, average return, max drawdown, Sharpe, equity curve,
optimizer, parameter sweep, walk-forward, or **any** performance number.
Non-dismissible banner: *"Signal visualization only. Not performance. Polymarket
settles on a 30-second TWAP; spot candles cannot reproduce settlement
outcomes."* Real validation is Paper Mode.

## Engineering ladder

Stop at the first rung that holds:

1. Does this need to exist at all? Speculative → skip it, say so in one line.
2. Already in this codebase? Reuse it. Look before you write.
3. Stdlib does it? Use it.
4. Native platform feature? CSS over JS, `<input type="date">` over a picker lib,
   a DB constraint over app code.
5. Already-installed dependency? Use it. **Never add a dependency for what a few
   lines can do** — keep dependencies minimal.
6. One line?
7. Only then: the minimum code that works.

The ladder shortens the solution, never the reading. Trace the whole flow first —
every file the change touches — then climb. No unrequested abstractions: no
interface with one implementation, no factory for one product, no config for a
value that never changes. Deletion over addition. Boring over clever.

**Bug fix = root cause, not symptom.** Grep every caller of the function you are
about to touch. One guard in the shared function is a smaller diff than a guard
in every caller, and patching only the path the ticket names leaves every sibling
caller broken.

**Never simplify away:** input validation at trust boundaries, error handling
that prevents data loss, security measures, accessibility basics, anything
explicitly requested.

## Comments

Each non-obvious decision gets a comment saying **what failure it prevents** —
not what the code does. `# Counted on start rather than on clean exit: a process
killed by OOM never reaches an exit path, and those are the restarts worth
counting.` Mark a deliberate corner-cut with its ceiling and upgrade path.

## Tests

**Lazy code without its check is unfinished.** Non-trivial logic — a branch, a
loop, a parser, a money path, a security path — leaves one runnable check behind.
Real assertions; a test that cannot fail is not a test. Watch for these two
self-inflicted failures, both of which have happened here:

- An invented decorator or marker (`@pytest.mark.asyncio_off`) — the A1 no-fake-
  API failure wearing test clothes.
- An `or True` in an assertion.

Repo conventions:
- `pytest_asyncio` is **not** installed. Use `asyncio.run(...)` inside a sync test.
- `psutil` is **not** installed. Stdlib only for host facts.
- There is **no JS runtime**. Frontend invariants are asserted by reading
  `app.js` / `index.html` as text. This is enough for the patterns being banned.
- Encode a spec list as an explicit `dict`/`tuple` in the test, not as prose, so
  a panel deleted in a later refactor fails a test instead of being noticed by an
  operator months later.
- Assert payload↔markup mapping in **both** directions. A bound path the backend
  never ships renders an em dash forever; a shipped field nothing renders is a
  value the operator still SSHes for.
- `tests/conftest.py` provides `VALID_TRADING_VALUES`, `WINDOW_TS`, `CLOSE_TS`,
  `OFFSETS`, `clock`, `store`, `source_root`, and an autouse logger-restore.
- `tests/test_infrastructure.py` holds the repo-wide bans: network modules only
  where allowed, `sqlite3` only in `arc/storage/`, no placeholder markers, every
  module declares `__all__`, no SQL outside the storage package, no direct clock
  reads, no `float()` on a value.

## The gate

Nothing is done until all three pass:

```bash
python -m ruff check . && python -m mypy arc && python -m pytest -q -p no:randomly
```

ruff line-length 100, mypy strict on `arc/`. The full suite exceeds a 120 s tool
timeout — run it in the background and read the output file.

Also required by phase spec:

```bash
grep -ri "auth\|jwt\|login\|session\|rbac" arc/api/
grep -ri "rtds\|chainlink" arc/strategy/ arc/windows/ arc/decision/ arc/risk/ arc/execution/
grep -rn "strategy\|twap\|ptb\|buffer" arc/execution/
```

All three must return nothing (A21 and A17 boundaries).

## Output discipline (A19)

```
1  Read the phase prompt fully before writing any code.
2  If ANYTHING is unclear — even something small — STOP AND ASK. Do not assume.
   "I would rather answer ten questions now than rewrite code later."
3  Write complete, working code. Every function real. No placeholders.
4  Write the tests named in the phase's Tests Required section. Real assertions.
5  Each non-obvious decision gets a comment saying WHAT FAILURE IT PREVENTS.
6  Report: files created · decisions and why · what you could not verify · the
   exact command to validate.
7  DO NOT claim a phase is validated if you have not executed the tests. Say
   plainly that they are written but unrun.
8  Do not proceed to the next phase until this one is green.
```

**Item 7 is the one that gets broken under pressure.** Distinguish, every time,
between *asserted by a test that ran* and *believed to be true*. Live behaviour —
real market rotations, a real Telegram send, a real provider disconnect, browser
memory over 24×7, recovery after a real reboot — cannot be asserted from a dev
machine. Say so; do not let a green unit suite imply it.

## Config

Every setting is prefixed `ARC_` and lives in `arc/config.py`. **Stored settings
in `data/arc.db` win over `.env`** — a new key needs a database backfill, not just
an `.env` line. `.env.example` must list every configurable value; no hidden
configuration anywhere.

Provider selection is configuration only, never a runtime UI control. RTDS is the
default. `TWAP_PROVIDER=CHAINLINK` with valid credentials switches the entire
runtime to Chainlink until the operator changes it back. **No mixed-provider
operation** — one provider supplies all TWAP data or none of it.

## Configuration lock

While trading is active, Buffers, Enabled Windows, Submission Count, Position
Size and strategy configuration are locked. Workflow: Stop Trading → Edit → Save
→ Start Trading. Configuration cannot change during active execution.

## Transparency

Every action is visible on the OPS Deck, or streamed into Signal Tank, or
recorded permanently in the Unified Ledger. **No execution may happen silently.**
No silent warnings — every runtime warning is recorded, explained, reported and
fixed. The operator should never need SSH, PM2 logs, a SQLite browser, terminal
output or a Python console.

Priority order when they conflict: **1 Transparency · 2 Reliability ·
3 Simplicity · 4 Speed · 5 Determinism.**

## Performance targets

Lowest latency · fast startup · low memory · low CPU · stable WebSockets ·
automatic reconnection · fault tolerance · high uptime · clean logging · fast
order execution · minimal network overhead · efficient caching · predictable
execution timing.

Bounded everything: capped event deques, capped DOM lists with any dedup `Set`
pruned alongside, `replaceChildren` over append. A 24×7 process with an unbounded
list is a browser or a heap that grows all week. Reconnect on a loopback socket
uses a fixed short delay, not a growing backoff — a long backoff leaves the
operator watching a greyed panel minutes after the runtime came back.

## Preservation

This project has had significant engineering work. Understand the complete
architecture before changing anything. Preserve working functionality. Improve
instead of replacing. Refactor only for measurable benefit. Maintain backward
compatibility and preserve APIs, interfaces and configuration unless a change is
clearly justified — and say what the justification is.

## Corrections

Correct an earlier statement only when the error changes the user's code,
conclusions or decisions. State it plainly and continue. No apologies, no
preambles, no tallying past errors, no ruminating.
