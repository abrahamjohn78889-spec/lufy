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

## 4. First live start

- [ ] Wallet funded, and the balance shown on the deck matches the wallet.
- [ ] Select V2. Press START V2 RUNTIME. Preflight must pass; a FAIL refuses the
      start and names the failing check.
- [ ] Leave trading **disarmed** for one full market and watch it. A start is not
      a trade.
- [ ] Press START TRADING only when the deck agrees with reality.
- [ ] Watch the first submitted order all the way to settlement in the Ledger.

## 5. Operating

- [ ] PAUSE, not STOP RUNTIME, to hold new orders. STOP RUNTIME tears down the
      feeds; PAUSE keeps managing resting orders.
- [ ] After any unclean exit, read the Recovery panel before arming. An
      INDETERMINATE order is resolved by reconciliation, never by resubmitting.
- [ ] Back up `data/arc.db` on a schedule. It is the only state that survives a
      restart, and it is the record of every order.

## 6. Known limits

- Chainlink Data Streams is configuration-complete but has not been validated
  against live Chainlink credentials. Treat the first Chainlink run as
  validation, not production.
- Wallet figures come from documented Polymarket SDK calls only. Where an
  official API does not exist, the deck reads `UNAVAILABLE (Official API not
  available)` rather than an estimate.
