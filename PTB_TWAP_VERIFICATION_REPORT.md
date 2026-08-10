# PTB / TWAP Verification Report

Scope: every official endpoint, websocket, document, field and assumption the bot uses
to obtain the **Price To Beat** and the **TWAP quantities**, and where each one lands in
the code.

Evidence dates are the live-observation sessions run against the production venue.
Nothing in this report is inferred from documentation alone; where a fact was
established by measurement, the measurement is stated.

---

## 1. The three quantities (A6)

They are never conflated and never named a bare `twap`. Declared in
[models.py:9](arc/domain/models.py:9).

| Quantity | Meaning | Owner | Read by strategy? |
|---|---|---|---|
| `signal_twap` | ARC's own cumulative mean over the market's 300 s life | `TwapAccumulator` — [models.py:94](arc/domain/models.py:94) | **YES — the only strategy input** |
| `settlement_twap` | The venue's Chainlink 30 s mean over the settlement window | `SettlementTwapCollector` — [settlement_feed.py:106](arc/market/settlement_feed.py:106) | **NO — observational only** |
| `ptb` | The official immutable opening reference | `resolve_ptb` — [ptb.py:208](arc/market/ptb.py:208) | Yes, as a frozen window value |

`settlement_twap` being read by nothing is asserted, not merely intended:
[test_strategy_protocol.py:49](tests/test_strategy_protocol.py:49) —
`test_the_context_does_not_carry_the_settlement_twap`.

---

## 2. Official endpoints

Exactly two remote endpoints exist in `arc/`. A grep for `https://` / `wss://` over the
package returns these and nothing else.

### 2.1 Gamma metadata (HTTPS, read-only, unauthenticated)

```
https://gamma-api.polymarket.com/markets?slug=<slug>
```

* Constant: `GAMMA_MARKETS_URL` — [discovery.py:54](arc/market/discovery.py:54)
* Client: `MarketDiscovery.fetch_metadata` — [discovery.py:372](arc/market/discovery.py:372)
* Timeout: 10 s (`_REQUEST_TIMEOUT_SECONDS`). Every transport failure becomes
  `FeedError`, which is operational, not fatal (A8: the process always starts).
* Unauthenticated on purpose: discovery must work in V1 paper mode, where no
  credentials exist at all.

**Decoding is not the stdlib default.** The body is parsed with
`json.loads(..., parse_float=Decimal)` — `decode_json` at
[discovery.py:250](arc/market/discovery.py:250), and the same parser is passed through
httpx at [discovery.py:387](arc/market/discovery.py:387). Gamma sends `priceToBeat` as a
**bare JSON number**; the stdlib default would bind the official PTB to a C double
before any ARC code could see it, losing digits irrecoverably *before* A1's "never
estimate the PTB" has anything left to protect.

### 2.2 RTDS price relay (WebSocket)

```
wss://ws-live-data.polymarket.com
```

* Constant: `RTDS_URL` — [feed.py:57](arc/market/feed.py:57)
* Client: `RtdsFeed` — [feed.py:136](arc/market/feed.py:136)
* Connect timeout 15 s; bounded exponential backoff 0.5 s → 30 s (`BackoffPolicy`,
  [feed.py:104](arc/market/feed.py:104)). Bounded deliberately: unbounded backoff reaches
  multi-minute delays, and a bot that takes four minutes to notice the feed returned has
  missed the session while reporting "reconnecting".

---

## 3. WebSocket protocol facts (each one verified live)

Subscribe frame — `subscribe_frame()`, [feed.py:89](arc/market/feed.py:89):

```json
{"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "update"}]}
```

| # | Fact | Constant | Failure it prevents |
|---|---|---|---|
| 1 | **No** `filters` / `symbols` / `assets` key. The topic is subscribed whole. | `subscribe_frame()` | A filter list is not ignored — it yields a subscription that delivers nothing while the socket stays open, which reads as a quiet market. |
| 2 | `type` is **required** and is half the subscription key, not a filter. | `CHAINLINK_TYPE = "update"` — [feed.py:71](arc/market/feed.py:71) | Established live **2026-08-05**: a subscription carrying only `topic` is answered `{"message": "Invalid request body"}`, and probing made the relay leak `leger GetTopics error: rpc error: code = NotFound desc = topic: crypto_prices_chainlink and type: crypto_prices_chainlink not found` — i.e. absent a `type` it substitutes the topic and finds nothing. Silent dead subscription. |
| 3 | Keepalive is the **literal text** `PING`, not a JSON frame and not a websocket ping opcode. | `KEEPALIVE_FRAME = "PING"` — [feed.py:74](arc/market/feed.py:74); sent every 20 s by `_keepalive_loop` | A `{"type":"ping"}` frame is discarded and the relay closes on its own idle timer. |
| 4 | **No snapshot on connect.** The first message arrives with the next tick. | `_open()` treats "subscribed" as "up" — [feed.py:202](arc/market/feed.py:202) | Code that waits for an initial state before declaring the feed up waits forever. |
| 5 | Relay's own dead-subscription threshold is 600 000 ms. | `SDK_STALE_THRESHOLD_MS` — [feed.py:79](arc/market/feed.py:79) | Recorded because it explains the 20 s keepalive cadence. It is **not** ARC's staleness policy, which is far tighter and lives in [watchdog.py](arc/market/watchdog.py). |
| 6 | The relay **echoes** the keepalive. | `messages()` swallows `PING`/`PONG` — [feed.py:235](arc/market/feed.py:235) | A caller would otherwise have to know the keepalive exists in order to parse the stream. |

Topic: `CHAINLINK_TOPIC = "crypto_prices_chainlink"` — [feed.py:58](arc/market/feed.py:58).
Expected symbol: `EXPECTED_SYMBOL = "BTC/USD"` — [observe.py:59](arc/runtime/observe.py:59).

Envelope shapes handled: a bare tick, a list of ticks, or `{"payload"|"data": …}`
containing either — `_messages_in` at [observe.py:442](arc/runtime/observe.py:442).

---

## 4. Payload fields

Every field is looked up under the spellings the relay is known to use. This is tolerant
**lookup** only: a payload carrying none of them is **rejected**, never defaulted
(`parse_payload`, [validation.py:209](arc/market/validation.py:209)).

| Field | Accepted keys | Code | Notes |
|---|---|---|---|
| Exact price | `full_accuracy_value`, `fullAccuracyValue` | `_FULL_ACCURACY_KEYS` [validation.py:74](arc/market/validation.py:74); `_as_full_accuracy_price` [validation.py:154](arc/market/validation.py:154) | **Preferred source.** Scaled by `scaleb(-18)` — an exact Decimal exponent shift, not a division: no quotient, no rounding context. |
| Lossy price | `value`, `price` | `_PRICE_KEYS` [validation.py:56](arc/market/validation.py:56); `_as_price` [validation.py:125](arc/market/validation.py:125) | Fallback only. `_as_price` **refuses floats by design**. |
| Timestamp | `timestamp`, `ts`, `time` | `_TS_KEYS`; `_as_seconds` [validation.py:101](arc/market/validation.py:101) | ms stamps (≥ 1e11) are read as ms; that is a unit reading, not a repair. Outside 2020-01-01 … 2100-01-01 → rejected, never shifted. |
| Symbol | `symbol`, `pair`, `asset` | `_SYMBOL_KEYS` | Must equal `BTC/USD` case-insensitively, else `WRONG_SYMBOL`. |
| Window length | `windowSeconds`, `window_seconds` | `_WINDOW_KEYS`; `_as_window_seconds` [validation.py:192](arc/market/validation.py:192) | Absent → stored as `None`, **never defaulted to 30**. Absence is the reference-stream signature (TRAP 2). |
| Feed ID | `feedId`, `feed_id` | `_FEED_KEYS` | Recorded verbatim; this is how U2 gets pinned down. |

### 4.1 Why `full_accuracy_value` is mandatory, not an optimisation

Confirmed against the live relay **2026-08-05**. Every payload carries both:

```
"value":               64195.85640491587           <- a bare JSON number
"full_accuracy_value": "64195856404915870000000"   <- exact integer TEXT
```

and `full_accuracy_value / 10**18` reproduces `value` exactly.

`value` is a JSON number, so it is already bound to a C double before any parser sees
it, and `_as_price` refuses floats. **A build reading only `value` would reject every
live observation and accumulate no signal TWAP at all.** Reading the integer text keeps
the entire pipeline exact and never touches a float.

Fixed-point padding is stripped after scaling (`normalize()`, re-quantized to exponent
zero when integral) so a price does not render 18 digits wide and compare unequal to the
same value written plainly — [validation.py:179](arc/market/validation.py:179).

### 4.2 Gamma metadata fields

| Field | Accepted keys | Code |
|---|---|---|
| Close time | `closeTime`, `close_ts`, `endDateTs`, `gameStartTime` | `_CLOSE_TS_KEYS` [discovery.py:61](arc/market/discovery.py:61) |
| Condition ID | `conditionId`, `condition_id` | `_CONDITION_KEYS`; **absence is a hard `FeedError`** — a market with no condition id cannot be traded or settled against |
| CLOB tokens | `clobTokenIds`, `clob_token_ids`, `tokens` | `_TOKEN_KEYS`; accepts a list or a JSON-encoded string |
| **Price To Beat** | `priceToBeat`, `price_to_beat`, `strikePrice`, `openingPrice` | `_PTB_KEYS`; `_official_ptb` [discovery.py:281](arc/market/discovery.py:281) |
| **Final price** | `finalPrice`, `final_price` | `_FINAL_PRICE_KEYS`; `_official_final_price` [discovery.py:293](arc/market/discovery.py:293) |
| Active / closed | `active` / `closed` | `_ACTIVE_KEYS` / `_CLOSED_KEYS` |

**Where the PTB actually lives.** Established against the live endpoint **2026-08-05**:
it is *not* a top-level market field. A flat scan of all 70+ top-level keys finds
nothing, and a regex over the key names for beat/strike/reference/opening matches
nothing either. It is nested one level down, on the market's event:

```
markets[0].events[0].eventMetadata = {"finalPrice": …, "priceToBeat": …}
```

Verified self-consistent on a settled market: `finalPrice` 64260.55 < `priceToBeat`
64276.70 with `outcomePrices ["0","1"]` — i.e. Down. **A lookup that searched only the
top level would find no PTB on any market ever and send every single one down the
fail-closed DEAD path, which reads identically to the venue being down.** Nested lookup:
`_event_metadata` at [discovery.py:265](arc/market/discovery.py:265).

Both PTB and finalPrice go through `_as_ptb_text` [discovery.py:220](arc/market/discovery.py:220),
which **refuses a float rather than stringifying it**: `str()` of an already-rounded
double preserves the rounding while looking exact. A float reaching that function means
some caller bypassed `decode_json`, and refusing is the correct answer to that.

---

## 5. Price To Beat: sources and evidence

A1 Rule 1 in full force — *fetch the official PTB from official metadata; never
calculate it, never estimate it*. There is **no arithmetic in `ptb.py` that produces a
price**: no mean, no midpoint, no interpolation, no last-spot substitution, no
carry-forward. The only operations applied are `to_decimal` on the venue's exact text
and a positivity check.

### L1 — `OFFICIAL_METADATA`
`SOURCE_METADATA` [ptb.py:72](arc/market/ptb.py:72). The market metadata's own
`priceToBeat`. Used verbatim.

### L2 — `OFFICIAL_PREVIOUS_CLOSE`
`SOURCE_PREVIOUS_CLOSE` [ptb.py:73](arc/market/ptb.py:73). The venue's **published**
`finalPrice` of market M−1.

Live measurement **2026-08-05**, six consecutive settled markets, **zero mismatches**:

```
priceToBeat(M) == finalPrice(M-1)      exactly
```

Markets are contiguous (A5), so M−1's close instant *is* M's `window_ts`, and the venue
publishes its own number for that instant. This is a **lookup of an official venue
value**, not a calculation.

Timing: the venue writes `eventMetadata` (both fields, together, at settlement) roughly
**25 s after a market closes** — i.e. ~25 s into the next market's life, **260 s before**
that market's earliest execution window at close−15 s. M's official opening reference is
therefore readable long before M needs it. Retry cadence for an unresolved PTB is 5 s
([observe.py:66](arc/runtime/observe.py:66) comment block), so publication is found
within one interval.

Cache: `PreviousClosePtbCache` [ptb.py:113](arc/market/ptb.py:113). Only the latest entry
is kept (an older entry can never be read again and would grow without bound on a 24/7
run). An out-of-order fetch **never overwrites a newer entry** — metadata responses are
not ordered, and letting a slow M−2 response win would hand the next market the wrong
window's reference. `usable_for()` is an **exact** `opens_window_ts == window_ts` match
with no tolerance: the value was published for a specific market, not observed near a
moment in time.

### What was REMOVED, and why

An earlier build read the price observed on the feed at the 300 s boundary. Measured
against the venue's published number it differed by **6E-12** — genuinely, not as a
decoding artifact. Close is not official, and substituting it is exactly the estimation
A1 forbids. **The boundary path is gone from `ptb.py` entirely**, and with it the
connection-spanning bookkeeping that used to gate it — see the note at
[feed.py:22](arc/market/feed.py:22).

### Fail-closed

Both sources unavailable → `PtbResolution(value=None)` → `freeze_ptb_for` sets
`MarketPhase.DEAD` with `dead_reason = DEAD_REASON_PTB_UNAVAILABLE` ("PTB_UNAVAILABLE")
and logs `PTB Unavailable` — [ptb.py:256](arc/market/ptb.py:256). The process keeps
running and keeps collecting observations for that slug for the record; it never trades
it.

### Freeze-once

`MarketInstance.freeze_ptb` [models.py:510](arc/domain/models.py:510) **raises on a
second call even with an identical value** (A11/A12). `freeze_ptb_for` therefore cannot
be used to refresh a PTB — a code path that believes it may re-fetch is caught by an
exception, not by a comment. Persisted through
`Store.save_ptb` [store.py:177](arc/storage/store.py:177), whose `WHERE ptb IS NULL`
clause is the actual guarantee rather than the Python check preceding it.
`restore_ptb` [models.py:529](arc/domain/models.py:529) is the separate, idempotent
restart path so recovery never needs to defeat the one-way freeze.

---

## 6. The signal TWAP (strategy input)

* `TwapAccumulator` [models.py:94](arc/domain/models.py:94) stores **`running_sum` +
  `observation_count`** and divides **on read** (`mean`, [models.py:119](arc/domain/models.py:119)).
  This is hazard **H1**: the incremental-mean form rounds at every step, and over 300 s
  of ticks the drift is large enough to move a trigger.
* Restored across restarts by `TwapAccumulator.restore(running_sum, observation_count)`
  [models.py:132](arc/domain/models.py:132) — the two exact components, never the mean.
* Ingestion: `MarketInstance.add_observation` [models.py:561](arc/domain/models.py:561),
  accepted only in `_OBSERVING_PHASES` = DISCOVERED · ACTIVE · CANCELLING · SETTLING
  [models.py:62](arc/domain/models.py:62). SETTLING is included deliberately — those are
  the observations inside the settlement averaging window. DEAD and SETTLED refuse: a
  settled market's signal TWAP is a closed historical record.
* Validation before ingestion: `ObservationValidator`
  [validation.py:278](arc/market/validation.py:278), limits
  `max_age_ms=30_000`, `max_future_ms=2_000`, `max_deviation_percent=5`. The last
  accepted price is **not** updated by a rejected sample — otherwise one bad print widens
  the band around itself and the next bad print is admitted.

### Where the signal TWAP is consumed

Frozen once per window at the opening instant, then never re-read:
`ExecutionWindow.freeze` [models.py:167](arc/domain/models.py:167) locks all five values
atomically — `opening_twap`, `ptb`, `buffer`, `direction`, `locked_trigger`. Everything
is validated and derived into locals **before any field is assigned**, so a failure
leaves the window fully PENDING rather than half-frozen with a real TWAP beside a
defaulted buffer.

Direction contract (strict operators only, `>=`/`<=` forbidden here):

```
opening_twap >  ptb  ->  UP
opening_twap <  ptb  ->  DOWN
opening_twap == ptb  ->  NoDirectionError  ->  window state NO_DIRECTION, terminal
```

[models.py:214](arc/domain/models.py:214). Equality is not a tie to be broken — it is the
absence of a direction. The rejection is raised *before* the assignment block and is
**never retried**, because a retry would freeze against a later TWAP.

Trigger firing keeps the inclusive operators — a separate rule, deliberately:
`ExecutionWindow.is_triggered` [models.py:275](arc/domain/models.py:275),
`UP: signal_twap >= locked_trigger`, `DOWN: signal_twap <= locked_trigger`. An exact
touch is a successful trigger.

Triggers are re-evaluated the instant an observation lands
([observe.py:322](arc/runtime/observe.py:322)), not on the 200 ms rotation tick — the
signal TWAP only moves when an observation arrives, so this is the only instant at which
a trigger can newly become satisfied; deferring it would make the check sampled rather
than continuous.

---

## 7. The settlement TWAP (observational)

* Collector: `SettlementTwapCollector` [settlement_feed.py:106](arc/market/settlement_feed.py:106).
  Same exact-sum / divide-on-read shape as the signal accumulator (H1). Per-market
  instance, created fresh, dropped at close, **no reset path** (A11).
* Window: `window_start = close_ts - 30` — [settlement_feed.py:124](arc/market/settlement_feed.py:124),
  and the same reading in `settlement_window_start` [timing.py:140](arc/domain/timing.py:140).
  **This placement is UNVERIFIED (U1) and is flagged as such in both places.**
* Out-of-window observations are **dropped, not clamped** — a clamped sample would shift
  the recorded mean and make the U1 comparison answer the wrong question
  ([settlement_feed.py:144](arc/market/settlement_feed.py:144)).
* Wiring: created on every market open and fed on every accepted observation —
  [observe.py:336](arc/runtime/observe.py:336) and [observe.py:314](arc/runtime/observe.py:314).

---

## 8. TRAP 1 and TRAP 2

### TRAP 1 — 30 s and 60 s are LOOKBACK LENGTHS, not publication rates

Stated at `SETTLEMENT_WINDOW_SECONDS` [timing.py:52](arc/domain/timing.py:52), restated in
the `feed.py` docstring, and restated a third time in
[watchdog.py:13](arc/market/watchdog.py:13) — the module most likely to violate it.

The watchdog measures inter-message gaps **only** to answer "is data arriving", never to
infer or health-check a window length. Nothing in `watchdog.py` reads or asserts
`window_seconds`.

### TRAP 2 — assert `windowSeconds == 30` from the payload's own field

`assert_settlement_window` [settlement_feed.py:69](arc/market/settlement_feed.py:69)
raises `SettlementWindowAssertionError` when the field **disagrees** *and* when it is
**absent**. Absence is the reference-stream signature; treating it as "unknown, probably
fine" is exactly how reference prices get recorded as settlement means.

The TWAP stream is **not** `0x0003…75b8` / `BTC/USD-RefPrice-DS-Premium-Global-003` — the
feed IDs changed at mainnet launch.

The assertion runs on the **first** payload per collector and then not again
([settlement_feed.py:171](arc/market/settlement_feed.py:171)): the window is a property of
the stream, not of a message, and re-asserting per message would turn one malformed frame
into a stream-level failure.

The refusal message embeds `TWAP_SETTLEMENT_EFFECTIVE_TS` rendered as text
([settlement_feed.py:55](arc/market/settlement_feed.py:55)) so an operator can tell a
correct pre-switchover refusal from a genuine wrong-feed fault — without it the two read
identically in the log.

**`TWAP_SETTLEMENT_EFFECTIVE_TS = 1_786_060_800` (2026-08-07 00:00:00 UTC)** —
[timing.py:70](arc/domain/timing.py:70). Confirmed live 2026-08-05: every 5-minute market
carries `cryptoMarketConfig.twapEnabled = false` (btc-5m and eth-5m alike), and the market
description states the pre-TWAP rule — resolution against *"the price at the beginning of
that range"*, not an average of it.

**This constant is DIAGNOSTIC ONLY. Nothing branches on it.** In particular it never
relaxes the TRAP 2 assertion: a date is not evidence about which stream is connected, and
letting a clock grant permission that only a payload can grant is how a reference stream
gets recorded as a settlement mean.

---

## 9. Assumptions — the four unverified items (A8)

None can be answered from documentation, so `SpecChecker`
[spec_check.py:91](arc/market/spec_check.py:91) answers what it can from the live stream
and records the rest as `UNRESOLVED`.

| ID | Question | Status in this build | Code |
|---|---|---|---|
| **U1** | Does the settlement window sit `[close_ts-30, close_ts]` or straddle close? | Length confirmed from the payload; **placement UNRESOLVED**. This build uses `close_ts - 30` and **nothing decides on it**. | `U1_WINDOW_PLACEMENT` [spec_check.py:53](arc/market/spec_check.py:53); `window_start` [settlement_feed.py:124](arc/market/settlement_feed.py:124) |
| **U2** | The exact feed ID of the 30 s BTC/USD TWAP stream | Read from the payload's `feedId` and recorded verbatim; never assumed | `U2_FEED_ID` [spec_check.py:150](arc/market/spec_check.py:150) |
| **U3** | Is PTB still a snapshot at `window_ts`, or is it itself a 30 s TWAP? | UNRESOLVED. Recorded observationally per settled market | `record_settled_market` [spec_check.py:171](arc/market/spec_check.py:171) |
| **U4** | Does the settled comparison use `>=` or `>`? | UNRESOLVED. Needs a market that settled with the two values **exactly equal**, which is rare | `U4_COMPARISON` [spec_check.py:188](arc/market/spec_check.py:188) |

U3 and U4 are left as *observation*, not inference: a guess drawn from a market whose
values were not exactly equal would look like evidence and would be wrong.

### The fail-closed gate

`SpecCheckResult.status` reaches `VERIFIED` only when the stream identity is confirmed
over `_SAMPLE_TARGET = 3` payloads. Anything short of that:

* `SpecChecker.apply` [spec_check.py:206](arc/market/spec_check.py:206) records the status
  and logs `Spec Unverified`. **It does not raise** — the caller is startup step 5, and a
  raise there would kill a process that is required to keep serving its dashboard and
  keep accumulating its TWAP.
* Enforcement is at the **order-submission boundary inside the Risk Engine**, gate 1:
  `_gate_trading_enabled` [engine.py:181](arc/risk/engine.py:181) denies with
  `DenialReason.TRADING_DISABLED_SPEC_UNVERIFIED`.

**THE PROCESS ALWAYS STARTS.** Feeds live, TWAP accumulating, windows opening, decisions
evaluated and **recorded** but never submitted. That shape is the point: a process that
refused to boot on an unverified spec would also refuse to collect the very data that
resolves the spec, so the unknown could never be closed. A process that booted and traded
anyway would be trading a settlement model nobody has checked.

Feed staleness can also disable trading (`FEED_STALE`,
[observe.py:349](arc/runtime/observe.py:349)) but **can never re-enable it** — re-enabling
requires VERIFIED status from the spec check. A watchdog that could enable trading would
be a second, weaker authority over the same flag, and the weaker one would win whenever
data happened to be flowing.

---

## 10. Provider abstraction

`arc/market/providers.py`. One interface, chosen by configuration alone. The PTB Engine,
Window Engine, Decision Engine, Risk Engine, Limit Order Engine and dashboard all receive
observations carrying **no trace of their origin**.

| Provider | Status | Code |
|---|---|---|
| `RTDS` | The only implementation. Default (`twap_provider: str = "RTDS"`, [config.py:147](arc/config.py:147)) | `RtdsFeed` |
| `CHAINLINK` | **Configuration-ready and unimplemented.** Selecting it raises `ConfigInvariantError` | `build_provider` [providers.py:60](arc/market/providers.py:60) |

`CHAINLINK` is a real enum member so the operator gets a clear refusal rather than
"unknown provider CHAINLINK", which reads as a typo instead of "implemented once the
official details exist".

There is deliberately **no Chainlink module, no placeholder feed ID and no speculative
endpoint**: a stub that connected to a guessed identifier would produce prices that look
exactly like real ones. Credential slots exist and default empty —
`chainlink_api_key`, `chainlink_api_secret`, `chainlink_feed_id`
([config.py:150](arc/config.py:150)); the two keys are `SecretStr`, so the value never
appears in a repr, an f-string, a pydantic validation error or a traceback.

`build_provider` **refuses rather than falling back to RTDS**: an operator who configured
Chainlink and silently got RTDS would be trading a different price source than the one
they believe is live, with nothing on the dashboard saying so.

`TWAP_PROVIDER` is bootstrap-only and deliberately **not** a trading value — the source of
the data is an infrastructure choice, and putting it on the Settings page would let it be
swapped mid-market.

---

## 11. Slug and timing arithmetic (A5)

```
window_ts = floor(now / 300) * 300
slug      = f"btc-updown-5m-{window_ts}"
close_ts  = window_ts + 300
```

`slug_math` [discovery.py:95](arc/market/discovery.py:95); constants
`MARKET_DURATION_SECONDS = 300`, `SLUG_PREFIX = "btc-updown-5m-"`
[timing.py:43](arc/domain/timing.py:43).

The arithmetic is local because the slug must be known before any request can be made.
**But the venue's `close_ts` WINS.** On disagreement the divergence is logged as
`SLUG_MATH_DIVERGENCE` and the venue's value is used
([discovery.py:418](arc/market/discovery.py:418)) — the venue settles the market, so the
venue's clock decides when it closes; a bot trusting its own arithmetic would cancel and
settle at the wrong instant and the error would look like latency. The computed value is
retained alongside as `computed_close_ts` purely so the divergence stays inspectable.

Markets are **contiguous**: the next `window_ts` equals this market's `close_ts`
(`next_slug_math` [discovery.py:105](arc/market/discovery.py:105)). Prefetching N+1's
metadata before the boundary is what lets N+1 freeze its PTB the instant N closes, rather
than after a round trip that would lose the first seconds of its signal TWAP.

`settlement_determined_fraction` [timing.py:154](arc/domain/timing.py:154) implements A7 —
`(w−t)/w`, so at t=15 half the outcome is already arithmetically fixed and at t=3, ninety
percent. **Display only; no trading decision reads it.**

---

## 12. Documents and evidence relied upon

| Source | What it established | Status |
|---|---|---|
| Live Gamma endpoint, **2026-08-05** | PTB lives at `events[0].eventMetadata.priceToBeat`, not top-level; `finalPrice` sits beside it; both written ~25 s after close | Measured |
| Live Gamma endpoint, **2026-08-05**, six consecutive settled markets | `priceToBeat(M) == finalPrice(M−1)` exactly, zero mismatches | Measured |
| Live RTDS relay, **2026-08-05** | `(topic, type)` subscription pair required; relay error text leaked; literal `PING` keepalive; no connect snapshot | Measured |
| Live RTDS payloads, **2026-08-05** | Both `value` (JSON number) and `full_accuracy_value` (18-dp integer text) present; the latter reproduces the former exactly | Measured |
| Live market config, **2026-08-05** | `cryptoMarketConfig.twapEnabled = false` on btc-5m and eth-5m; description states resolution against the price at the beginning of the range | Measured |
| ARC boundary observation vs venue `finalPrice` | Differed by 6E-12 → the boundary path is an estimate → **removed** | Measured, then deleted from the codebase |
| Venue documentation on settlement window placement (U1), exact TWAP feed ID (U2), PTB form (U3), settled comparison operator (U4) | **Does not exist / not obtainable** | Unresolved; fail-closed via the Risk Engine |

No official Chainlink feed ID, credential set or documentation for the TWAP stream has
been verified, which is precisely why `CHAINLINK` raises instead of connecting.

---

## 13. Summary of assumptions still open

1. **U1** — settlement window placement. Build uses `close_ts − 30`; flagged in two
   places; nothing decides on it.
2. **U2** — exact 30 s TWAP feed ID. Read from the payload, never assumed.
3. **U3** — whether PTB is a snapshot or itself a 30 s TWAP.
4. **U4** — `>=` vs `>` in the venue's settled comparison.
5. **Pre-switchover state** — before 2026-08-07 no stream carries `windowSeconds`, so
   TRAP 2 fails and trading stays disabled. Correct fail-closed behaviour; the refusal
   message says so explicitly.
6. **Key-spelling tolerance** — payload fields are looked up under several spellings
   because no document pins them. Tolerant lookup only; a payload matching none is
   rejected, never defaulted.

All six are enforced the same way: `trading_enabled = False` with reason
`TRADING_DISABLED_SPEC_UNVERIFIED`, denied at the single order-submission boundary in the
Risk Engine, with everything else in the process still running and still recording.

---

## 14. Video-review evidence (observational, CLOSED)

Two screen recordings of the live Polymarket UI were reviewed frame by frame against a
single BTC Up/Down 5-minute market (`btc-updown-5m-1786220100`, the 4:15–4:20 PM ET
window on 2026-08-08). This section records what the **video** established. It is
**evidence only**: nothing here changed, or is permitted to change, any trading, TWAP,
settlement, PTB, direction, trigger, buffer, Window Engine, Decision Engine, Risk Engine
or Limit Order Engine logic. No code was modified on the basis of this recording.

### The distinction this section must not blur

**`Current Price` ≠ `Settlement 30 s TWAP` ≠ `Final Price`.** These are three different
quantities and the video only ever shows the first. The Polymarket UI's live "Current
Price" ticker is a spot readout; the venue settles on a 30-second mean over the settlement
window (`settlement_twap`, §7); the recorded outcome is a separate `finalPrice` field
written to Gamma metadata ~25 s after close (§5, §12). A spot price sitting below PTB is
consistent with a DOWN settlement but is **not** the settlement value and is **not** the
final result.

### Second review — final 30-second settlement window (directly observed)

| Observation | Value | Basis |
|---|---|---|
| Settlement window (last 30 s before close) | Directly observed, frame by frame | Video |
| Price To Beat (PTB) | `$65,032.01`, frozen for the entire window | Read from UI |
| Current Price across the window | ≈ `$65,024.87`–`$65,024.92` (~`$7.1` below PTB) | Read from UI |
| Visible CLOB pricing | Down favoured at ≈ `99¢`; Up collapsed to `1–2¢` | Read from UI |
| Direction the video supports | **DOWN** (strongly) | Inferred from spot + book |

**Marked `UNVERIFIED FROM VIDEO`:**

- **The exact Chainlink 30-second settlement TWAP value.** The UI never renders it; the
  `~$7.1`-below-PTB figure is the *spot* Current Price, not the settlement mean. The TWAP
  value cannot be read off this recording.
- **The explicit final settlement banner / resolved result.** The recording ends at the
  close tick without displaying a settled outcome. A DOWN resolution is *inferred* from the
  flat spot line and the persistent ~`$7` gap; it was **not** directly observed as a
  settlement banner. Authoritative confirmation would be the Gamma `finalPrice` for this
  market or the venue's own resolution.

### Status

**CLOSED / COMPLETE.** This review is no longer pending production verification. The only
items from this particular recording that remain unverified are the two named above — the
exact settlement TWAP value and the explicit final settlement result — and both are
unverifiable *from video by construction*, not open work items against the code.
