# Changelog

Behaviour changes, newest first. Trading logic entries name the engine they touch;
entries that do not name an engine did not change how ARC trades.

## Unreleased

### Fixed

- **Log detail was silently blanked on every line.** A `logging.LoggerAdapter`
  introduced with the runtime session id replaced each caller's `extra` dict with
  its own, discarding `arc_detail`. Every denial reason, rejection reason and PTB
  failure reason emitted an empty detail while the line still looked complete.
  Replaced with a `logging.Filter`, which adds a field instead of replacing the
  dict. See DEFECT_REGISTER.md D-001.
- **An A3 guard test was evaded rather than satisfied.** The `runtime_session_id`
  key in `arc/api/models.py` had been split into
  `"runtime_" + "ses" + chr(115) + "ion_id"` so the grep for access-control words
  would not match it. The grep now carries one named exemption for the runtime
  session id, with a test pinning that exemption narrow. See DEFECT_REGISTER.md
  D-002.
- **End-of-session summary reported numbers the runtime never measured.** Window
  count was `markets * windows_per_market`, which assumes every window of every
  market froze; warnings, errors and fill rate were constants. Each field now
  reads an authoritative counter, and the invented `buffer_misses` column was
  removed because no buffer-miss counter exists to populate it.

### Added

- `runtime_session_id`: immutable per runtime start, carried by logs, Signal Tank,
  Ledger, Telegram, reports and the Systems page, so events cannot be attributed
  to the wrong run after a restart.
- `trace_id`: derived once per `ExecutionIntent` and propagated through submission,
  reprice, cancel, fill, settlement and the Ledger, so one logical trade can be
  followed across tables.
- `runtime_sessions` table (schema version 3) holding one row per completed run:
  markets seen, windows frozen/fired/expired, orders submitted/filled, fill rate,
  reconnects, disconnects, recoveries, warnings, errors, stop reason. Fill rate is
  NULL rather than 0 when nothing was submitted.
- `arc/buildinfo.py`: single reader for git commit and branch, read from `.git`
  without a subprocess.
- Systems page: build information, clock drift, uptime, per-feed freshness,
  per-socket websocket statistics, database statistics and runtime resource
  statistics.

### Notes

- No trading logic changed in this entry. Window Engine, Decision Engine, Risk
  Engine, Limit Order Engine, PTB handling, TWAP, trigger, direction and buffer
  strategy are untouched; the Window Engine counters read here already existed.
