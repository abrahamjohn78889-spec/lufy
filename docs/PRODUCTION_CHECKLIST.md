# Production checklist

Work top to bottom. Nothing here is optional, and nothing here is a substitute
for the operator watching the first live window.

## 1. Host

- [ ] Python 3.12 or 3.13.
- [ ] The clock is synchronised (`chrony` or `systemd-timesyncd`). ARC measures
      drift and blocks trading on a critical reading; a host with no NTP will
      block or, worse, freeze a window against a wrong second.
- [ ] The SQLite file lives on local disk, not a network mount.
- [ ] The process is under a supervisor (PM2, systemd) that restarts it.
      `--mode` decides which runtime a restart comes back with; it always comes
      back **disarmed**.

## 2. Configuration

- [ ] `.env` written from `.env.example`. Every uncommented key present.
- [ ] `ARC_API_BIND` is loopback. A non-loopback bind is refused at startup —
      the network boundary is the access control.
- [ ] Trading values are correct **in `data/arc.db`**, not only in `.env`.
      `.env` seeds SQLite once; SQLite wins afterwards. A new trading key needs
      a database backfill, not just a line in `.env`.
- [ ] `ARC_TWAP_PROVIDER` is `RTDS` (default) or `CHAINLINK`. If `CHAINLINK`,
      the API key, secret and feed id are set. Never mixed.
- [ ] `arc doctor` reports every credential as SET. It reports SET or UNSET and
      never the value.

## 3. Verification before the first live window

- [ ] `python -m ruff check .` — clean.
- [ ] `python -m mypy arc` — clean.
- [ ] `python -m pytest -q` — green. (Several minutes; not a fast check.)
- [ ] `arc doctor` — no FAIL.
- [ ] Start V1. Let it run **at least three full markets**. Confirm on the deck:
      the countdown matches Polymarket's own, PTB appears and does not move,
      Signal TWAP accumulates, windows open and freeze, and the Ledger records
      each window exactly once.
- [ ] Preflight shows Runtime Verification YES with no FAIL lines.
- [ ] Telegram delivers. Send one notification end to end before relying on it.
- [ ] Stop V1. Confirm status returns to STOPPED and no orphan tasks or sockets
      remain (the Provider and WebSocket rows go to Waiting).

## 4. The V1 validation run

V1 is the production validation environment: the same live feed, the same
official CLOB book, the same PTB and TWAPs as V2, with only the execution adapter
simulated. A long V1 run is therefore evidence about the production runtime.

- [ ] Run V1 continuously for **at least 100 consecutive markets** (~8.5 hours).
      Restarting mid-run is fine and is itself worth testing; a market the
      runtime never saw is not, and shows as a market gap.
- [ ] Read the report:

      curl -s 'localhost:8080/history?format=report&markets=200'

- [ ] **Recorder: complete, zero market gaps.** An incomplete market names the
      field it is missing. Nothing is reconstructed — a missing PTB is a fault to
      investigate, not a value to fill in.
- [ ] **Fill statistics** are present for every configured window, and the late
      windows are not silently empty.
- [ ] **No FAILED criteria.** A FAIL is a real defect found in the stored rows:
      duplicate intents, two live orders on one window, duplicate fill ids, an
      order with no intent, an unresolved order left behind.
- [ ] The UNVERIFIED list is the operator's work, not the suite's. Each entry
      prints a `how:` line saying what would demonstrate it. Work through them
      against the running deck before treating the run as validated.
- [ ] Host metrics are **not** in the report by design. Read them yourself on the
      VPS: `top`, `free -m`, `df -h`, `ss -s`.
- [ ] **Runtime metrics** (uptime, restarts, reconnects, dropped sockets,
      recoveries, latencies, recorder size, database growth, validation duration)
      are printed in their own block. Two latencies are measured from the order
      row's own timestamps — submission and fill; the four round trips ARC does
      not instrument (websocket, CLOB, RTDS, Chainlink) read
      `UNAVAILABLE (not instrumented)` rather than a plausible figure derived
      from something adjacent.
- [ ] **Reconnects, dropped sockets and recoveries are three different numbers.**
      A drop is a socket that was up and went down; the reconnect count is how
      many times the ladder tried. If they are equal, one attempt fixed each
      outage. If reconnects are far higher, read the Signal Tank.
- [ ] The verdict reads `READY FOR V2 LIVE TRADING` only when nothing is failed
      and nothing is unverified. Anything else is `NOT READY FOR V2 LIVE TRADING`,
      and that is the answer.

## 5. First live start

- [ ] Wallet funded, and the balance shown on the deck matches the wallet.
- [ ] Select V2. Press START V2 RUNTIME. Preflight must pass; a FAIL refuses the
      start and names the failing check.
- [ ] Leave trading **disarmed** for one full market and watch it. A start is not
      a trade.
- [ ] Press START TRADING only when the deck agrees with reality.
- [ ] Watch the first submitted order all the way to settlement in the Ledger.

## 6. Operating

- [ ] PAUSE, not STOP RUNTIME, to hold new orders. STOP RUNTIME tears down the
      feeds; PAUSE keeps managing resting orders.
- [ ] After any unclean exit, read the Recovery panel before arming. An
      INDETERMINATE order is resolved by reconciliation, never by resubmitting.
- [ ] Back up `data/arc.db` on a schedule. It is the only state that survives a
      restart, and it is the record of every order.

## 7. Known limits

- Chainlink Data Streams is configuration-complete but has not been validated
  against live Chainlink credentials. Treat the first Chainlink run as
  validation, not production.
- Wallet figures come from documented Polymarket SDK calls only. Where an
  official API does not exist, the deck reads `UNAVAILABLE (Official API not
  available)` rather than an estimate.
