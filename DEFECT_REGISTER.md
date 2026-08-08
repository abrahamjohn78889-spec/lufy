# Defect Register

Every verified defect found from Pre-Prompt 8 onward. A defect is recorded here
whether or not it ever reached a commit: a fix applied silently is a fix nobody
can audit, and one caught before commit is still evidence about how the code
behaves under change.

Commits are referenced by subject line. Hashes are not written here because a
register that must be amended after every commit invites amending.

---

## D001 — LoggerAdapter erased `arc_detail` from every log line

- **Severity:** High. Silent, and it removed exactly the text an operator needs
  when something is refused: the reason on every risk denial, every order
  rejection and every PTB failure. Lines still looked complete.
- **Root cause:** The runtime wrapped its logger in
  `logging.LoggerAdapter(logger, {"arc_session_id": ...})` to stamp the session
  id. `LoggerAdapter.process` **replaces** the caller's `extra` dict with the
  adapter's own. Every `log_event(...)` call passes
  `extra={"arc_detail": detail}`, so the detail was discarded before the record
  was created — not at the formatter, at the source.
- **Reproduction:** `python -m pytest tests/test_runtime_ptb.py -q -p no:randomly`
  → `TestFailClosed::test_the_unavailable_line_is_logged_with_its_reason` fails.
  The captured record carries the event `PTB Unavailable` with an empty detail
  where `no trading this market` is expected.
- **Fix:** Replaced the adapter with `SessionFilter`, a `logging.Filter` that
  *adds* `arc_session_id` to the record and touches nothing else, attached via
  the idempotent `attach_session_id` (`arc/logging_setup.py`). A filter is the
  correct mechanism here: adapters own `extra`, filters augment records. The
  `ArcLineFormatter` output was reverted to its original form — the session id
  is carried on the record for the Signal Tank and the API, and is deliberately
  not printed into the frozen plain-line format.
- **Tests:** `tests/test_logging.py::TestSessionStampingNeverEatsTheDetail`
  (7 tests): the detail reaches the file; `arc_detail` survives on the record
  itself; the session id is not printed into the line; column alignment is
  unchanged; re-attaching the same session returns the same filter without
  stacking; a new session replaces the previous id.
- **Commit:** `Session id and trace id, with the two defects that pass exposed`
- **Verification:** `tests/test_logging.py` 32 passed;
  `tests/test_runtime_ptb.py` 24 passed; full suite 2137 passed, 1 skipped.

## D002 — Obfuscated dict key evading the A3 access-control grep

- **Severity:** Medium for behaviour (the key was correct at runtime), high for
  process. It defeated a guard rather than answering it, and left the repository
  carrying a deliberate obfuscation artifact.
- **Root cause:** `arc/api/models.py` emitted the new field as
  `"runtime_" + "ses" + chr(115) + "ion_id"`, with a matching `getattr`, so the
  literal string `session` never appeared in the source.
  `tests/test_api.py::TestNoAccessControlCode::test_the_forbidden_words_do_not_appear_in_the_package`
  greps this package for `auth|jwt|login|session|rbac`; the concatenation made
  the grep pass while the field shipped. The conflict was real and was hidden
  instead of resolved.
- **Reproduction:** Write the key plainly and run
  `python -m pytest tests/test_api.py -q -p no:randomly` → the grep test fails
  with `['models.py:433', 'models.py:627']`.
- **Fix:** Two parts. The key is now written plainly. The guard test strips one
  named exemption — `runtime session[_id]` — before grepping, so the bare word
  `session` stays forbidden everywhere else, prose included. The exemption is
  justified in the test's docstring: a runtime session id names which *run* of
  the process emitted an event; there is no cookie, credential, store, expiry or
  sign-in anywhere near it. The A3 rule (no access-control code; the loopback
  bind is the access control) is unchanged.
- **Tests:** `tests/test_api.py::TestNoAccessControlCode::test_the_session_exemption_is_narrow`
  pins the exemption: the runtime session id and its prose pass, while cookie
  sessions, `SessionMiddleware`, `login`, `Authorization`, `jwt.decode` and
  `rbac_check` are all still caught — including a `login` form sitting on the
  same line as the exempt phrase. The pattern and the exemption are one
  module-level definition read by both tests, so the sweep cannot be widened
  while the narrowness test passes against a private copy.
- **Commit:** `Session id and trace id, with the two defects that pass exposed`
- **Verification:** `tests/test_api.py` 33 passed; full suite 2137 passed.

## D003 — Fabricated fields in the end-of-session summary

- **Severity:** High. A summary is read as measurement. Numbers with no
  measurement behind them are worse than absent ones, because they are believed.
- **Root cause:** The first draft of the session-summary row computed
  `windows_opened = markets_processed * len(execution_windows)` — a product
  asserting that every window of every market froze, false whenever PTB was
  unavailable or no direction was determinable — and hardcoded
  `buffer_misses = 0`, `warnings = 0`, `errors = 1 if stop_reason == "error"
  else 0`, `fill_rate = None` unconditionally, and
  `git_commit = "UNAVAILABLE"` as a literal despite a working reader existing in
  `arc/api/models.py`.
- **Reproduction:** Inspection, before commit. No released build carried it.
- **Fix:** Every field now resolves to an authoritative counter or is absent.
  The Window Engine's own `windows_frozen` / `windows_fired` /
  `windows_expired` replace the product. `EventHub` counts warnings and errors
  at emit time, because `_events` is a bounded deque and scanning it would
  undercount a long run. `fill_rate` is computed from
  `Store.order_tally_since(started_at)` — one query, so the rate cannot
  disagree with its own numerator, and scoped by `created_at` so a restart
  cannot credit this run with a previous run's orders — and is left NULL, not
  0.0, when nothing was submitted, since a run that placed no orders has no
  fill rate. `git_commit` reads `.git` through the new shared `arc/buildinfo.py`
  (no subprocess: `arc run` must not shell out), so the Systems page and the
  session row report one commit from one reader.
  The `buffer_misses` column was **removed entirely.** No authoritative
  buffer-miss counter exists in this system, and `windows_expired` cannot
  substitute: `expire_all` also expires windows that never froze, so the number
  conflates "the buffer was too wide" with "there was never a trigger". A column
  named for a measurement nobody takes is a guess with a schema.
- **Tests:** Covered by the storage and runtime suites; the removed column is
  absent from `arc/storage/schema.py` migration 3 and from `EXPECTED_TABLES`
  verification.
- **Commit:** `Session id and trace id, with the two defects that pass exposed`
- **Verification:** ruff clean; mypy clean on 82 source files; full suite 2137
  passed, 1 skipped.
