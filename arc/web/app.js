/*
  ARC Operations Center — the browser half.

  ZERO business logic. Every value written to the DOM is read from the backend
  status document by the element's data-f path; nothing here compares a TWAP to a
  trigger, derives a direction, sizes a position or decides a stage. If a value is
  not in the document it is not on screen, which is deliberate: a number the
  frontend could compute is a number that can disagree with the engine.

  Decimals arrive as STRINGS and are assigned to textContent verbatim. There is no
  parseFloat on any price, quantity, buffer or trigger anywhere in this file.

  STALE IS NEVER LIVE. On socket loss the whole document greys out and the values
  stop being claimed as current. A dashboard that keeps rendering the last frame is
  the one failure mode an operator cannot detect by looking.
*/
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let state = null;      // the last status document
let events = [];       // Signal Tank buffer
let seen = new Set();  // event seq numbers already rendered
let lastFrame = 0;     // performance.now() of the last status frame

// The Signal Tank is capped for the same reason the backend's is: a 24x7 process
// with an unbounded DOM list is a browser that grows all week.
const MAX_ROWS = 1000;

// ── path read ────────────────────────────────────────────────────────────────

function at(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}

// ── formatters (presentation only — never arithmetic on a money value) ───────

const pad = (n) => String(n).padStart(2, '0');

function hhmmss(ts) {
  if (ts == null) return '—';
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function duration(seconds) {
  if (seconds == null) return '—';
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h ${m}m` : (h ? `${h}h ${m}m` : `${m}m ${s % 60}s`);
}

// FLOORED and NEVER NEGATIVE, matching the official timer: 04:59 for the whole
// 299th second. A ceiling would show 05:00 twice, and a negative countdown after
// close would read as a market that is somehow still open.
function countdown(closeTs, nowSeconds) {
  if (closeTs == null) return '--:--';
  const left = Math.max(0, Math.floor(closeTs - nowSeconds));
  return `${pad(Math.floor(left / 60))}:${pad(left % 60)}`;
}

function text(value) {
  if (value == null || value === '') return '—';
  if (value === true) return 'YES';
  if (value === false) return 'NO';
  return String(value);
}

// ── the binder ───────────────────────────────────────────────────────────────

function paint() {
  if (!state) return;
  for (const el of $$('[data-f]')) {
    const raw = at(state, el.dataset.f);
    if (el.hasAttribute('data-time')) el.textContent = hhmmss(raw);
    else if (el.hasAttribute('data-dur')) el.textContent = duration(raw);
    else if (el.hasAttribute('data-ms')) {
      el.textContent = raw == null ? '—' : `${Math.round(raw)} ms`;
    } else if (el.hasAttribute('data-list')) {
      el.textContent = Array.isArray(raw) && raw.length ? raw.join(', ') : 'none';
    } else el.textContent = text(raw);

    if (el.hasAttribute('data-flag')) {
      el.classList.toggle('yes', raw === true);
      el.classList.toggle('no', raw === false);
    }
  }
  paintBanner();
  paintEngines();
  paintPreflight();
  paintLoe();
  paintWindows();
  paintChips();
  paintRecoverySteps();
  paintModes();
  paintAnalytics();
  paintSettings();
  paintWarnings();
  paintErrorSummary();
  applyPresentation();
  tickTimers();
}

// The disable reason is shown in full, always. A truncated reason is a reason the
// operator has to go to the logs for, which is the trip this dashboard removes.
function paintBanner() {
  const b = $('#banner'), r = state.runtime;
  if (!r.trading_enabled) {
    b.className = 'banner red';
    b.textContent = `TRADING DISABLED BY SYSTEM — Reason: ${r.disable_reason || 'unspecified'}`;
  } else if (!r.execution_armed) {
    b.className = 'banner amber';
    b.textContent = 'TRADING NOT ARMED — the runtime is running; no orders will be submitted.';
  } else {
    b.className = 'banner green';
    b.textContent = `TRADING ARMED — ${state.execution.execution_label} · ${state.runtime.mode}`;
  }
}

function paintEngines() {
  const host = $('#engines');
  host.replaceChildren(...state.engines.map((e) => {
    const row = document.createElement('div');
    row.className = 'engine';
    row.innerHTML =
      `<span class="dot ${e.light.toLowerCase()}"></span>` +
      `<span class="en">${e.engine}</span><span class="es">${e.state}</span>` +
      `<span class="ed">${e.detail || ''}</span>`;
    return row;
  }));
}

function paintPreflight() {
  $('#preflight').replaceChildren(...state.preflight.checks.map((c) => {
    const row = document.createElement('div');
    row.className = `check ${c.result.toLowerCase()}`;
    row.innerHTML = `<b>${c.result}</b> <span>${c.check}</span> <i>${c.detail || ''}</i>`;
    return row;
  }));
  const pill = $('.pill[data-f="preflight.result"]');
  if (pill) pill.className = `pill ${state.preflight.result.toLowerCase()}`;
}

// The stage comes from the backend. This only highlights it.
function paintLoe() {
  const stage = state.derived.loe_stage;
  const order = state.derived.loe_stages;
  const reached = order.indexOf(stage);
  $$('#loe li').forEach((li) => {
    const i = order.indexOf(li.dataset.stage);
    li.classList.toggle('now', li.dataset.stage === stage);
    li.classList.toggle('done', i >= 0 && reached >= 0 && i < reached);
  });
}

function paintWindows() {
  const host = $('#windows');
  host.replaceChildren(...(state.market.windows || []).map((w) => {
    const div = document.createElement('div');
    div.className = `win ${w.state.toLowerCase()}`;
    div.innerHTML =
      `<div class="wl">${w.label}</div>` +
      `<div class="wst">${w.state}</div>` +
      `<div class="wd">${w.direction || '—'}</div>` +
      `<dl class="kv tight">` +
      `<dt>Frozen TWAP</dt><dd>${text(w.opening_twap)}</dd>` +
      `<dt>PTB</dt><dd>${text(w.ptb)}</dd>` +
      `<dt>Buffer</dt><dd>${text(w.buffer)}</dd>` +
      `<dt>Locked Trigger</dt><dd>${text(w.locked_trigger)}</dd>` +
      `<dt>Configured Buffer</dt><dd>${text(w.configured_buffer)} (~$${text(w.implied_btc_move)})</dd>` +
      `<dt>Opens</dt><dd>${hhmmss(w.opens_at)}</dd>` +
      `<dt>Fired</dt><dd>${hhmmss(w.fired_at)}</dd>` +
      `</dl>`;
    return div;
  }));
}

function paintChips() {
  const counts = state.execution.orders_by_state || {};
  $('#order-states').replaceChildren(...Object.entries(counts).map(([k, v]) => {
    const c = document.createElement('span');
    c.className = 'chip';
    c.textContent = `${k} ${v}`;
    return c;
  }));
}

function paintRecoverySteps() {
  $('#recovery-steps').replaceChildren(...(state.recovery.steps || []).map((s) => {
    const row = document.createElement('div');
    row.className = `check ${s.ok ? 'pass' : 'fail'}`;
    row.innerHTML = `<b>${s.ok ? 'OK' : 'FAIL'}</b> <span>${s.step}</span> <i>${s.detail || ''}</i>`;
    return row;
  }));
}

// The runtime the operator has SELECTED, which is not necessarily the one that is
// running: between choosing V2 and pressing START RUNTIME the process is still on
// V1. Cleared on the first frame so a reload shows what is actually running.
let selectedMode = null;

function paintModes() {
  const active = selectedMode || state.runtime.mode;
  $('#mode-v1').classList.toggle('on', active === 'V1');
  $('#mode-v2').classList.toggle('on', active === 'V2');
  // The buttons name the mode they will act on. "START RUNTIME" next to a
  // highlighted V2 is one glance away from being read as V1, and the two differ
  // by whether the orders are real.
  $('#runtime-start').textContent = `START ${active} RUNTIME`;
  $('#runtime-stop').textContent = `STOP ${active} RUNTIME`;
}

function paintAnalytics() {
  // Counters only. No win rate, no ROI, no Sharpe, no equity curve, no ranking.
  const rows = {
    'Markets Processed': state.stats.markets_processed,
    'Orders Submitted': state.stats.orders_submitted,
    'Orders Repriced': state.stats.orders_repriced,
    'Fills Recorded': state.stats.fills_recorded,
    'Observations Accepted': state.stats.observations_accepted,
    'Observations Rejected': state.stats.observations_rejected,
    'Settlement Samples': state.stats.settlement_samples,
    'PTB Frozen': state.stats.ptb_frozen,
    'PTB Unavailable': state.stats.ptb_unavailable,
    'Reconnects': state.stats.reconnects,
    'Runtime Uptime': duration(state.runtime.uptime_seconds),
  };
  const host = $('#analytics');
  host.replaceChildren();
  for (const [k, v] of Object.entries(rows)) {
    const dt = document.createElement('dt'); dt.textContent = k;
    const dd = document.createElement('dd'); dd.textContent = text(v);
    host.append(dt, dd);
  }
}

let settingsBuilt = false;

function paintSettings() {
  const s = state.settings;
  $('#lock-pill').textContent = s.locked ? 'LOCKED (trading armed)' : 'EDITABLE';
  $('#lock-pill').className = `pill ${s.locked ? 'fail' : 'pass'}`;

  if (!settingsBuilt) {
    const host = $('#editable');
    const fields = [
      ['buffers', 'Buffers (offset=value, comma separated)',
        Object.entries(s.buffers).map(([k, v]) => `${k}=${v}`).join(',')],
      ['execution_windows', 'Enabled Windows', s.execution_windows.join(',')],
      ['submission_count', 'Submission Count', s.submission_count],
      ['position_notional_usd', 'Position Size (USD)', s.position_notional_usd],
    ];
    host.replaceChildren(...fields.map(([key, label, value]) => {
      const wrap = document.createElement('label');
      wrap.className = 'field';
      wrap.innerHTML = `<span>${label}</span><input name="${key}" value="${value}">`;
      return wrap;
    }));

    const notify = $('#notify');
    notify.replaceChildren(...Object.keys(s.notifications).map((name) => {
      const l = document.createElement('label');
      l.className = 'toggle';
      l.innerHTML =
        `<input type="checkbox" data-cat="${name}"${s.notifications[name] ? ' checked' : ''}>` +
        `<span>${s.notification_labels[name]}</span>`;
      return l;
    }));
    settingsBuilt = true;
  }

  // The lock is enforced by the backend; disabling the inputs only tells the
  // operator why before they type, rather than after they press save.
  $$('#editable input').forEach((i) => { i.disabled = s.locked; });
  $('#save-settings').disabled = s.locked;
}

function paintWarnings() {
  const list = state.settings.warnings || [];
  $('#warnings').replaceChildren(...(list.length ? list : ['none']).map((w) => {
    const li = document.createElement('li');
    li.textContent = w;
    return li;
  }));
}

// ── Signal Tank ──────────────────────────────────────────────────────────────

function addEvent(ev) {
  // seq keyed: the socket replays the backlog on connect and the replay overlaps
  // the live stream, so without this the operator sees one fill listed twice.
  if (seen.has(ev.seq)) return;
  seen.add(ev.seq);
  events.push(ev);
  if (events.length > MAX_ROWS) {
    const dropped = events.splice(0, events.length - MAX_ROWS);
    dropped.forEach((d) => seen.delete(d.seq));
  }
}

function paintTank() {
  const q = $('#tank-q').value.trim().toLowerCase();
  const sev = $('#tank-sev').value;
  const rows = events.filter((e) =>
    (!sev || e.severity === sev) &&
    (!q || `${e.engine} ${e.event} ${e.detail}`.toLowerCase().includes(q)));

  $('#tank').replaceChildren(...rows.map((e) => {
    const tr = document.createElement('tr');
    tr.className = e.severity.toLowerCase();
    tr.id = `ev-${e.seq}`;
    for (const cell of [e.seq, e.ist || '—', e.et || '—', e.engine, e.severity,
                        e.detail ? `${e.event} — ${e.detail}` : e.event]) {
      const td = document.createElement('td');
      td.textContent = String(cell);
      tr.append(td);
    }
    return tr;
  }));

  if ($('#tank-follow').checked) {
    const last = $('#tank').lastElementChild;
    if (last) last.scrollIntoView({ block: 'nearest' });
  }
}

function paintErrorSummary() {
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const since = midnight.getTime() / 1000;
  const today = events.filter((e) => e.ts >= since);
  const of = (s) => today.filter((e) => e.severity === s);
  $('#err-warn').textContent = of('WARNING').length;
  $('#err-err').textContent = of('ERROR').length;
  $('#err-fatal').textContent = of('FATAL').length;

  const jump = (el, list) => {
    const e = list[list.length - 1];
    el.textContent = e ? `${e.event} ${e.detail || ''}`.trim() : '—';
    el.dataset.seq = e ? e.seq : '';
  };
  jump($('#err-last'), today.filter((e) => e.severity === 'ERROR' || e.severity === 'FATAL'));
  jump($('#warn-last'), of('WARNING'));
}

// ── stale handling ───────────────────────────────────────────────────────────

function setLive(live) {
  const el = $('#link');
  el.textContent = live ? 'LIVE' : 'DISCONNECTED — values are stale';
  el.className = `link ${live ? 'up' : 'down'}`;
  document.body.classList.toggle('stale', !live);
}

// ── timers ───────────────────────────────────────────────────────────────────

// Both timers read the same close_ts and the same interpolated clock, so they
// cannot drift from each other by construction — there is one source, not two.
let clockSkew = 0;   // serverNow - browserNow, measured on each status frame

function tickTimers() {
  if (!state) return;
  const now = Date.now() / 1000 + clockSkew;
  const s = countdown(state.market.close_ts, now);
  $('#timer1').textContent = s;
  $('#timer2').textContent = s;
}

// ── socket ───────────────────────────────────────────────────────────────────

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${proto}://${location.host}/ws`);

  socket.onopen = () => setLive(true);
  socket.onmessage = (msg) => {
    const frame = JSON.parse(msg.data);
    if (frame.type === 'status') {
      state = frame.data;
      clockSkew = state.ts - Date.now() / 1000;
      lastFrame = performance.now();
      paint();
    } else if (frame.type === 'signal') {
      addEvent(frame.data);
      paintTank();
      if (state) paintErrorSummary();
    }
  };
  const drop = () => {
    setLive(false);
    // Fixed short delay rather than a growing backoff: this is a loopback socket
    // on the same host, so a long backoff would leave the operator watching a
    // greyed-out panel for minutes after the runtime came back.
    setTimeout(connect, 1000);
  };
  socket.onclose = drop;
  socket.onerror = () => socket.close();
}

// A frame that never arrives is a dead socket the browser has not noticed. Without
// this the page would show the last frame indefinitely with LIVE still lit.
// The cadence is re-armed from the configured refresh rate on the first frame:
// a hardcoded interval would be configuration the operator cannot reach.
let timerHandle = setInterval(tick, 250);
let timerRate = 250;

function tick() {
  if (lastFrame && performance.now() - lastFrame > 5000) setLive(false);
  tickTimers();
}

// Theme and repaint cadence come from the backend, not from constants here. A
// value the markup hardcodes is a value .env cannot change, which is exactly the
// hidden configuration this dashboard is not allowed to have.
function applyPresentation() {
  if (!state || !state.settings) return;
  const theme = state.settings.theme;
  if (theme && document.documentElement.dataset.theme !== theme) {
    document.documentElement.dataset.theme = theme;
  }
  const rate = state.settings.refresh_rate_ms;
  if (rate && rate !== timerRate) {
    timerRate = rate;
    clearInterval(timerHandle);
    timerHandle = setInterval(tick, rate);
  }
}

// ── actions ──────────────────────────────────────────────────────────────────

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: body === undefined ? null : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, data };
}

function say(el, ok, message) {
  el.className = `msg ${ok ? 'ok' : 'bad'}`;
  el.textContent = message;
}

$$('[data-post]').forEach((btn) => {
  btn.addEventListener('click', async () => {
    const { ok, data } = await post(btn.dataset.post);
    // The refusal is shown verbatim. Paraphrasing a system refusal would put the
    // operator's belief about why trading is off out of step with the reason.
    const target = btn.closest('.panel').querySelector('.msg') || $('#control-msg');
    say(target, ok, ok ? JSON.stringify(data) : (data.detail || 'refused'));
  });
});

$$('[data-mode]').forEach((btn) => {
  btn.addEventListener('click', () => {
    // Selection only. Pressing V2 must not start V2 — the operator picks a runtime
    // and then decides to start it, because a mode button that booted a live venue
    // session on one click is a live session started by a misclick.
    selectedMode = btn.dataset.mode;
    paintModes();
    say($('#control-msg'), true, `${selectedMode} selected. Press START RUNTIME.`);
  });
});

$('#runtime-start').addEventListener('click', async () => {
  const mode = selectedMode || state.runtime.mode;
  const { ok, data } = await post(`/start?mode=${encodeURIComponent(mode)}`);
  say($('#control-msg'), ok, ok ? JSON.stringify(data) : (data.detail || 'refused'));
  if (ok) selectedMode = null;
});

$('#save-settings').addEventListener('click', async () => {
  const body = {};
  $$('#editable input').forEach((i) => { body[i.name] = i.value; });
  const { ok, data } = await post('/settings?action=save', body);
  say($('#settings-msg'), ok,
      ok ? 'Saved. Restart required for the engines to pick it up.'
         : (data.detail || 'rejected'));
});

$('#notify').addEventListener('change', async (e) => {
  const box = e.target.closest('[data-cat]');
  if (!box) return;
  const { ok, data } = await post('/settings?action=notifications',
                                  { [box.dataset.cat]: box.checked });
  say($('#notify-msg'), ok, ok ? 'Applied.' : (data.detail || 'rejected'));
});

$('#backup').addEventListener('click', async () => {
  const { ok, data } = await post('/settings?action=backup');
  say($('#backup-msg'), ok, ok ? `Backup written: ${data.backup}` : (data.detail || 'failed'));
  loadSnapshots();
});

async function loadSnapshots() {
  const res = await fetch('/settings?snapshot=list');
  const data = await res.json();
  $('#snapshots').replaceChildren(...(data.snapshots || []).map((s) => {
    const row = document.createElement('div');
    row.className = 'check pass';
    row.innerHTML = `<b>${s.bytes}</b> <span>${s.name}</span> <i>${hhmmss(s.modified)}</i>`;
    return row;
  }));
}

// ── ledger ───────────────────────────────────────────────────────────────────

const LEDGER_COLUMNS = [
  'market', 'window', 'ptb', 'signal_twap', 'settlement_twap', 'direction',
  'locked_trigger', 'buffer', 'intent_id', 'local_order_id', 'venue_order_id',
  'submission_time', 'fill_time', 'settlement_time', 'order_price', 'fill_price',
  'quantity', 'filled_quantity', 'remaining_quantity', 'state_display',
  'rejection_display', 'buffer_status', 'settlement_result', 'pnl', 'notes',
];
// Rendered as IST / ET from the backend's `<key>_display` block rather than
// reformatted here: one conversion utility server-side, no zone maths in the browser.
const TIME_COLUMNS = new Set(['submission_time', 'fill_time', 'settlement_time']);

function dualTime(row, key) {
  const d = row[`${key}_display`];
  if (!d || !d.utc) return '—';
  return `${d.ist} / ${d.et}`;
}

function ledgerQuery() {
  const p = new URLSearchParams();
  if ($('#led-q').value.trim()) p.set('q', $('#led-q').value.trim());
  if ($('#led-dir').value) p.set('direction', $('#led-dir').value);
  if ($('#led-state').value) p.set('state', $('#led-state').value);
  if ($('#led-result').value) p.set('result', $('#led-result').value);
  return p;
}

async function loadLedger() {
  const p = ledgerQuery();
  const res = await fetch(`/history?${p}`);
  const data = await res.json();
  const csv = new URLSearchParams(p); csv.set('format', 'csv');
  $('#led-csv').href = `/history?${csv}`;

  $('#ledger-table tbody').replaceChildren(...(data.records || []).map((r) => {
    const tr = document.createElement('tr');
    // Rejection reason is a SEPARATE column from state, deliberately: "Rejected"
    // without POST_ONLY_WOULD_CROSS beside it is a state with no explanation.
    tr.className = (r.state_display || '').toLowerCase().replace(/\s+/g, '-');
    for (const key of LEDGER_COLUMNS) {
      const td = document.createElement('td');
      td.textContent = TIME_COLUMNS.has(key) ? dualTime(r, key) : text(r[key]);
      tr.append(td);
    }
    return tr;
  }));
}

$('#led-go').addEventListener('click', loadLedger);
$('#led-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadLedger(); });

// ── backtest (A18: a viewer, never a backtester) ─────────────────────────────

const NS = 'http://www.w3.org/2000/svg';

function svgLine(x1, y1, x2, y2, cls) {
  const el = document.createElementNS(NS, 'line');
  el.setAttribute('x1', x1); el.setAttribute('y1', y1);
  el.setAttribute('x2', x2); el.setAttribute('y2', y2);
  el.setAttribute('class', cls);
  return el;
}

// One chart: candles, signal TWAP, PTB, locked trigger, fire points. No
// indicators, no overlays, no second chart, and no performance number anywhere —
// spot candles cannot reproduce a 30-second settlement TWAP.
function drawBacktest(payload) {
  const chart = $('#bt-chart');
  chart.replaceChildren();
  const candles = payload.candles || [];
  if (!candles.length) { say($('#bt-msg'), false, 'No cached candles in that range.'); return; }
  say($('#bt-msg'), true, `${candles.length} candles · ${payload.markets.length} markets`);

  // Chart geometry only. Number() here is on a pixel coordinate, never on a value
  // that is compared, stored, or acted on.
  const lows = candles.map((c) => Number(c.low)), highs = candles.map((c) => Number(c.high));
  const lo = Math.min(...lows), hi = Math.max(...highs), span = (hi - lo) || 1;
  const W = 1200, H = 420, pad = 10;
  const x = (i) => pad + (i * (W - 2 * pad)) / Math.max(candles.length - 1, 1);
  const y = (v) => H - pad - ((v - lo) / span) * (H - 2 * pad);

  candles.forEach((c, i) => {
    chart.append(svgLine(x(i), y(Number(c.low)), x(i), y(Number(c.high)), 'candle'));
    const up = Number(c.close) >= Number(c.open);
    chart.append(svgLine(x(i), y(Number(c.open)), x(i), y(Number(c.close)),
                         up ? 'body up' : 'body down'));
  });

  const index = new Map(candles.map((c, i) => [Number(c.open_ts), i]));
  for (const m of payload.markets) {
    const i = index.get(Number(m.window_ts));
    if (i === undefined) continue;
    const j = Math.min(i + 4, candles.length - 1);
    for (const [value, cls] of [[m.ptb, 'ptb'], [m.signal_twap, 'twap']]) {
      if (value == null) continue;
      chart.append(svgLine(x(i), y(Number(value)), x(j), y(Number(value)), cls));
    }
    for (const w of m.windows || []) {
      // Both triggers, because the replay does not know a direction: the frozen
      // TWAP that would have chosen one is not in a 5-minute candle.
      for (const t of [w.trigger_up, w.trigger_down]) {
        chart.append(svgLine(x(i), y(Number(t)), x(j), y(Number(t)), 'trigger'));
      }
      if (w.fired) {
        const dot = document.createElementNS(NS, 'circle');
        dot.setAttribute('cx', x(j));
        dot.setAttribute('cy', y(Number(m.signal_twap)));
        dot.setAttribute('r', 4);
        dot.setAttribute('class', 'fire');
        chart.append(dot);
      }
    }
  }

  const rows = payload.markets.flatMap((m) =>
    (m.windows || []).map((w) => [m.window_ts, m.ptb, m.signal_twap,
                                  `${w.offset_seconds}s`, w.buffer,
                                  `${w.trigger_up} / ${w.trigger_down}`,
                                  w.fired ? 'YES' : 'NO']));
  $('#bt-table tbody').replaceChildren(...rows.map((cells) => {
    const tr = document.createElement('tr');
    cells.forEach((v, i) => {
      const td = document.createElement('td');
      td.textContent = i === 0 ? hhmmss(v) : text(v);
      tr.append(td);
    });
    return tr;
  }));
}

$('#bt-go').addEventListener('click', async () => {
  const start = Date.parse($('#bt-start').value) / 1000;
  const end = Date.parse($('#bt-end').value) / 1000;
  if (!start || !end) { say($('#bt-msg'), false, 'Pick both ends of the range.'); return; }
  const res = await fetch(`/backtest?start=${Math.floor(start)}&end=${Math.floor(end)}`);
  const data = await res.json();
  if (!res.ok) { say($('#bt-msg'), false, data.detail || 'refused'); return; }
  // Rendered from the payload rather than hardcoded, so the chart cannot be drawn
  // without the warning the backend ships alongside it.
  $('#bt-warning').textContent = data.warning;
  drawBacktest(data);
});

// ── order book ───────────────────────────────────────────────────────────────

$('#book-go').addEventListener('click', async () => {
  const res = await fetch(`/orderbook?direction=${$('#book-dir').value}`);
  const data = await res.json();
  const host = $('#book');
  host.replaceChildren();
  const rows = res.ok
    ? { Market: data.market, Direction: data.direction, 'Best Bid': data.best_bid,
        'Current Passive Limit Price': data.passive_limit, 'Tick Size': data.tick_size }
    : { Error: data.detail };
  for (const [k, v] of Object.entries(rows)) {
    const dt = document.createElement('dt'); dt.textContent = k;
    const dd = document.createElement('dd'); dd.textContent = text(v);
    host.append(dt, dd);
  }
});

// ── workspaces ───────────────────────────────────────────────────────────────

function show(name) {
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.ws === name));
  $$('.ws').forEach((w) => w.classList.toggle('active', w.id === `ws-${name}`));
  if (name === 'ledger') loadLedger();
  if (name === 'system') loadSnapshots();
}

$$('.tab').forEach((t) => t.addEventListener('click', () => show(t.dataset.ws)));
$('#tank-q').addEventListener('input', paintTank);
$('#tank-sev').addEventListener('change', paintTank);

// Error Summary -> Signal Tank. The operator should never have to hunt for the
// event a counter refers to.
$$('.jump').forEach((el) => el.addEventListener('click', () => {
  const seq = el.dataset.seq;
  if (!seq) return;
  show('tank');
  $('#tank-q').value = '';
  $('#tank-sev').value = '';
  paintTank();
  const row = $(`#ev-${seq}`);
  if (row) { row.scrollIntoView({ block: 'center' }); row.classList.add('flash'); }
}));

// Populate the ledger state filter from the backend's own display list, so the
// options cannot drift from the states the ledger actually produces.
fetch('/status').then((r) => r.json()).then((doc) => {
  state = doc;
  paint();
  const sel = $('#led-state');
  Object.keys(doc.execution.orders_by_state || {}).forEach((s) => {
    const opt = document.createElement('option');
    opt.textContent = s;
    sel.append(opt);
  });
}).catch(() => setLive(false));

connect();
