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
  // §44: translate disable_reason to plain English in the Runtime panel.
  const drEl = document.querySelector('[data-f="runtime.disable_reason"]');
  if (drEl && state.runtime) {
    drEl.textContent = humanizeDisableReason(state.runtime.disable_reason || '');
  }
  paintBanner();
  paintEngines();
  paintPreflight();
  paintLoe();
  paintWindows();
  paintMajorityWindows();
  paintChips();
  paintRecoverySteps();
  paintHealth();
  paintModes();
  paintPaper();
  paintAnalytics();
  paintMajorityConfig();
  paintActiveOrders();
  paintWarnings();
  paintErrorSummary();
  applyPresentation();
  tickTimers();
}

// The disable reason is shown in full, always. A truncated reason is a reason the
// operator has to go to the logs for, which is the trip this dashboard removes.
// Raw enum names are mapped to human-readable sentences (§37).
function humanizeDisableReason(reason) {
  const map = {
    'TRADING_DISABLED_SPEC_UNVERIFIED':
      'Settlement spec not yet verified — trading will start once the market spec is confirmed',
    'TRADING_DISABLED': 'Trading disabled by system',
  };
  return map[reason] || reason;
}

function paintBanner() {
  const b = $('#banner'), r = state.runtime;
  if (r.paused) {
    b.className = 'banner amber';
    b.textContent = `TRADING PAUSED — ${state.execution.execution_label} · ${state.runtime.mode}`;
  } else if (!r.trading_enabled) {
    b.className = 'banner red';
    b.textContent = `TRADING DISABLED BY SYSTEM — ${humanizeDisableReason(r.disable_reason || 'unspecified')}`;
  } else if (!r.execution_armed) {
    b.className = 'banner amber';
    b.textContent = `TRADING STOPPED — runtime is running but no orders will be submitted.`;
  } else {
    b.className = 'banner green';
    b.textContent = `TRADING RUNNING — ${state.execution.execution_label} · ${state.runtime.mode}`;
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

// One row per configured MAJORITY window. Renders the configuration side by side
// with the live state for the current market. A market with no MAJORITY windows
// gets an empty body — the table headers are still there as a reminder that the
// engine exists and is currently configured to do nothing.
function paintMajorityWindows() {
  const tbody = $('#majority-windows tbody');
  if (!tbody) return;
  const cfg = state.majority?.config;
  const states = state.majority?.states_by_window ?? {};
  const windows = cfg?.windows ?? [];
  tbody.replaceChildren(...windows.map((w) => {
    const live = states[String(w.execution_window_seconds)] ?? {};
    const row = document.createElement('tr');
    row.innerHTML =
      `<td>${w.execution_window_seconds}s</td>` +
      `<td>${w.trigger_price}</td>` +
      `<td>${w.target_limit_price}</td>` +
      `<td>${w.shares}</td>` +
      `<td>${w.entry_price_min}–${w.entry_price_max}</td>` +
      `<td>${live.state ?? '—'}</td>` +
      `<td>${live.selected_side ?? '—'}</td>` +
      `<td>${live.verdict?.outcome ?? '—'}</td>` +
      `<td>${live.trigger_snapshot?.best_bid_up ?? '—'}</td>` +
      `<td>${live.trigger_snapshot?.best_bid_down ?? '—'}</td>` +
      `<td>${live.decision_snapshot?.best_bid_up ?? '—'}</td>` +
      `<td>${live.decision_snapshot?.best_bid_down ?? '—'}</td>`;
    return row;
  }));
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

// Gates and health transitions. Both are Systems-page tables that only change
// when the health snapshot does, so both are skipped while the revision holds —
// the timer path above still runs every frame, only this redraw is elided.
let paintedRevision = -1;

function paintHealth() {
  const revision = state.runtime.health_revision;
  if (revision === paintedRevision) return;
  paintedRevision = revision;

  $('#gates').replaceChildren(...(state.gates || []).map((g) => {
    const row = document.createElement('div');
    row.className = `check ${g.state === 'PASS' ? 'pass' : g.state === 'FAIL' ? 'fail' : ''}`;
    row.innerHTML =
      `<b>${g.state}</b> <span>${g.id} ${g.gate}</span> <i>${g.detail || ''}</i>`;
    return row;
  }));

  $('#health-history').replaceChildren(...(state.health_history || []).slice().reverse()
    .map((h) => {
      const row = document.createElement('div');
      row.className = 'check';
      row.innerHTML =
        `<b>#${h.revision}</b> <span>${h.utc_display}</span> <i>${h.detail}</i>`;
      return row;
    }));
}


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
  // The MAJORITY panel names the mode the runtime is actually running, not the
  // operator's pending selection: V1 is paper, V2 is live money.
  const shown = $('#majority-mode');
  if (shown) shown.textContent = state.runtime.mode === 'V2' ? 'V2 LIVE' : 'V1 PAPER';
}

// §33: V1 paper bankroll panel. Hidden entirely in V2 where real funds answer
// through the Wallet block; a visible-but-empty panel would imply the operator
// had paper funds when they do not. Controls are disabled unless paper.editable,
// which is true only when the runtime is STOPPED — same guard the route enforces.
function paintPaper() {
  const panel = $('#paper-panel');
  if (!panel) return;
  const p = state.paper;
  // V2: status_payload returns paper=null. Hide the whole section.
  if (p == null) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  const editable = !!p.editable;
  const input = $('#paper-start-input');
  const setBtn = $('#paper-set-start');
  const resetBtn = $('#paper-reset');
  if (input) input.disabled = !editable;
  if (setBtn) setBtn.disabled = !editable;
  if (resetBtn) resetBtn.disabled = !editable;
}

$('#paper-set-start')?.addEventListener('click', async () => {
  const input = $('#paper-start-input');
  const val = input?.value?.trim();
  if (!val) { say($('#paper-msg'), false, 'enter a starting balance'); return; }
  const { ok, data } = await post('/settings?action=paper', { start_balance: val });
  say($('#paper-msg'), ok,
      ok ? `Starting balance set to ${data.paper?.start_balance ?? val}.`
         : (data.detail || 'rejected'));
  if (ok && input) input.value = '';
});

$('#paper-reset')?.addEventListener('click', async () => {
  // The operator must confirm: a reset zeroes realised counters while leaving
  // history intact. Accidental clicks on a button this consequential are exactly
  // what the spec's "with confirmation" clause prevents.
  if (!confirm('Reset paper account? Realised P&L counters will restart from zero. Settlement history is preserved.')) return;
  const { ok, data } = await post('/settings?action=paper', { reset: true });
  say($('#paper-msg'), ok,
      ok ? 'Paper account reset. Starting balance unchanged.'
         : (data.detail || 'rejected'));
});

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
    'Dropped Sockets': state.stats.disconnects,
    'Recoveries': state.stats.recoveries,
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
let majorityWindowsBuilt = false;

const MAJORITY_PRESETS = [3, 5, 7, 10, 15, 25, 30, 45, 60, 90, 120];
const MAJORITY_WINDOW_KEYS = ['buffer', 'trigger_price', 'target_limit_price', 'shares', 'entry_price_min', 'entry_price_max'];
const MAJORITY_WINDOW_LABELS = {
  buffer: 'Buffer Amount',
  trigger_price: 'Trigger',
  target_limit_price: 'Target Limit',
  shares: 'Shares',
  entry_price_min: 'Entry Min',
  entry_price_max: 'Entry Max',
};

function _majorityWindowRow(win, defaults) {
  const wrap = document.createElement('div');
  wrap.className = 'majority-window-row';
  wrap.innerHTML = `<span class="mwin-label">${win}s</span>`;
  MAJORITY_WINDOW_KEYS.forEach((key) => {
    const val = defaults[key] ?? '';
    const label = document.createElement('label');
    label.className = 'field';
    label.innerHTML =
      `<span>${MAJORITY_WINDOW_LABELS[key] || key}</span>` +
      `<input name="majority_w_${win}_${key}" value="${val}">`;
    wrap.appendChild(label);
  });
  return wrap;
}

function paintMajorityConfig() {
  const s = state.settings;
  const lockPill = $('#lock-pill');
  if (lockPill) {
    lockPill.textContent = s.locked ? 'LOCKED (trading armed)' : 'EDITABLE';
    lockPill.className = `pill ${s.locked ? 'fail' : 'pass'}`;
  }

  if (!settingsBuilt) {
    const host = $('#majority-editable');
    if (!host) return;

    // ── MAJORITY ───────────────────────────────────────────────────────────
    const mcfg = s.majority || {};
    const mwinKeys = MAJORITY_WINDOW_KEYS;

    const majorityScalarFields = [
      ['majority_buffer',       'Buffer Amount (default)', ''],
      ['majority_trigger_price','Trigger Price',       ''],
      ['majority_target_limit_price', 'Target Limit Price', ''],
      ['majority_shares',       'Shares',               ''],
      ['majority_entry_price_min', 'Entry Price Min',  ''],
      ['majority_entry_price_max', 'Entry Price Max',  ''],
      ['majority_price_retry_attempts', 'Retry Attempts', ''],
    ];

    const windows = mcfg.windows || [];
    const wVals = {};
    for (const w of windows) { wVals[w.execution_window_seconds] = w; }
    const enabledWins = windows.map((w) => w.execution_window_seconds);

    // preset bar
    const presetsDiv = document.createElement('div');
    presetsDiv.id = 'majority-presets';
    presetsDiv.innerHTML = '<span class="preset-label">Windows</span>';
    MAJORITY_PRESETS.forEach((w) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `preset-btn${enabledWins.includes(w) ? ' active' : ''}`;
      btn.textContent = w;
      btn.dataset.window = w;
      btn.addEventListener('click', () => {
        const input = $('#majority_windows_input');
        const cur = input.value
          ? input.value.split(',').map(Number).filter(Boolean)
          : [...enabledWins];
        if (cur.includes(w)) {
          const next = cur.filter((x) => x !== w);
          input.value = next.join(',');
          btn.classList.remove('active');
        } else {
          cur.push(w); cur.sort((a, b) => a - b);
          input.value = cur.join(',');
          btn.classList.add('active');
        }
        _rebuildMajorityWindowRows();
      });
      presetsDiv.appendChild(btn);
    });
    // custom input
    const customInput = document.createElement('input');
    customInput.id = 'majority_windows_input';
    customInput.name = 'majority_execution_windows';
    customInput.placeholder = '3,15,60…';
    customInput.value = enabledWins.join(',') || '';
    customInput.addEventListener('input', () => {
      MAJORITY_PRESETS.forEach((w) => {
        const btn = presetsDiv.querySelector(`[data-window="${w}"]`);
        if (btn) btn.classList.toggle('active', customInput.value.split(',').map(Number).includes(w));
      });
      _rebuildMajorityWindowRows();
    });
    presetsDiv.appendChild(customInput);

    // majority_enabled checkbox
    const enabledHidden = document.createElement('input');
    enabledHidden.type = 'hidden';
    enabledHidden.name = 'majority_enabled';
    enabledHidden.value = mcfg.enabled ? 'true' : 'false';
    const enabledCb = document.createElement('input');
    enabledCb.type = 'checkbox';
    enabledCb.name = 'majority_enabled';
    enabledCb.value = 'true';
    if (mcfg.enabled) enabledCb.checked = true;
    enabledCb.addEventListener('change', () => {
      enabledHidden.value = enabledCb.checked ? 'true' : 'false';
    });
    const enabledWrap = document.createElement('label');
    enabledWrap.className = 'field';
    enabledWrap.appendChild(enabledHidden);
    enabledWrap.appendChild(enabledCb);
    const enabledSpan = document.createElement('span');
    enabledSpan.textContent = 'Enabled';
    enabledWrap.appendChild(enabledSpan);

    // Three ON/OFF switches. The hidden input carries the true/false the backend
    // parses; the checkbox and the ON/OFF text are the operator's surface — no
    // boolean value is ever shown as a word.
    const switchDefs = [
      ['majority_trigger_limit_enabled', 'Trigger + Target Price', mcfg.trigger_limit_enabled],
      ['majority_buffer_enabled', 'Buffer', mcfg.buffer_enabled],
      ['majority_price_retry_enabled', 'Price Retry', mcfg.price_retry_enabled],
    ];
    const switchHost = document.createElement('div');
    switchHost.id = 'majority-switches';
    switchHost.replaceChildren(
      enabledWrap,
      ...switchDefs.map(([key, label, on]) => {
        const wrap = document.createElement('label');
        wrap.className = 'field onoff';
        wrap.innerHTML =
          `<input type="hidden" name="${key}" value="${on ? 'true' : 'false'}">` +
          `<span>${label}</span>` +
          `<input type="checkbox" data-switch="${key}"${on ? ' checked' : ''}>` +
          `<span class="onoff-state">${on ? 'ON' : 'OFF'}</span>`;
        wrap.querySelector('[data-switch]').addEventListener('change', (e) => {
          wrap.querySelector('input[type="hidden"]').value = e.target.checked ? 'true' : 'false';
          wrap.querySelector('.onoff-state').textContent = e.target.checked ? 'ON' : 'OFF';
          paintMajorityConfig();
        });
        return wrap;
      }),
    );
    const retryHint = document.createElement('p');
    retryHint.className = 'cfg-hint';
    retryHint.textContent =
      'Price Retry re-prices by +1/-1 tick while Trigger + Target is OFF.';

    const scalarHost = document.createElement('div');
    scalarHost.id = 'majority-scalars';
    scalarHost.replaceChildren(
      ...majorityScalarFields.map(([key, label, value]) => {
        const wrap = document.createElement('label');
        wrap.className = 'field';
        wrap.innerHTML = `<span>${label}</span><input name="${key}" value="${value}">`;
        return wrap;
      }),
    );

    const windowRowsHost = document.createElement('div');
    windowRowsHost.id = 'majority-window-rows';

    function _rebuildMajorityWindowRows() {
      const wins = (customInput.value || '').split(',').map(Number).filter(Boolean);
      const wVals2 = {};
      for (const w of wins) { wVals2[w] = wVals[w] || {}; }
      windowRowsHost.replaceChildren(
        ...wins.map((w) => _majorityWindowRow(w, wVals2[w] || {})),
      );
    }
    _rebuildMajorityWindowRows();

    // Organize into sections. Each card groups related fields so the operator can
    // scan at a glance. No TWAP configuration — TWAP is data support only.
    const timeCard = document.createElement('div');
    timeCard.className = 'cfg-card';
    timeCard.innerHTML = '<h4>Time Window</h4>';
    timeCard.appendChild(presetsDiv);

    const switchCard = document.createElement('div');
    switchCard.className = 'cfg-card';
    switchCard.innerHTML = '<h4>Operator Switches</h4>';
    switchCard.append(switchHost, retryHint);

    const entryCard = document.createElement('div');
    entryCard.className = 'cfg-card';
    entryCard.innerHTML = '<h4>Entry</h4>';
    entryCard.appendChild(scalarHost);

    const winCard = document.createElement('div');
    winCard.className = 'cfg-card';
    winCard.innerHTML = '<h4>Per-Window Overrides</h4>';
    winCard.appendChild(windowRowsHost);

    host.replaceChildren(timeCard, switchCard, entryCard, winCard);

    const notify = $('#notify');
    if (notify) {
      notify.replaceChildren(...Object.keys(s.notifications).map((name) => {
        const l = document.createElement('label');
        l.className = 'toggle';
        l.innerHTML =
          `<input type="checkbox" data-cat="${name}"${s.notifications[name] ? ' checked' : ''}>` +
          `<span>${s.notification_labels[name]}</span>`;
        return l;
      }));
    }
    settingsBuilt = true;
  }

  // The lock is enforced by the backend; disabling the inputs only tells the
  // operator why before they type, rather than after they press save. Switch
  // dependencies are applied here too — fields gated by a switch are disabled
  // when the switch is OFF, regardless of the lock.
  const trigBox = $('#majority-editable input[data-switch="majority_trigger_limit_enabled"]');
  const bufBox = $('#majority-editable input[data-switch="majority_buffer_enabled"]');
  const retryBox = $('#majority-editable input[data-switch="majority_price_retry_enabled"]');
  $$('#majority-editable input').forEach((i) => {
    if (i.dataset.switch) { i.disabled = s.locked; return; }
    const n = i.name || '';
    let depOff = false;
    if (n === 'majority_price_retry_attempts') depOff = !(retryBox && retryBox.checked);
    else if (n.endsWith('trigger_price') || n.endsWith('target_limit_price')) {
      depOff = !(trigBox && trigBox.checked);
    } else if (n.endsWith('_buffer')) {
      depOff = !(bufBox && bufBox.checked);
    }
    i.disabled = s.locked || depOff;
  });
  const saveBtn = $('#save-majority-config');
  if (saveBtn) saveBtn.disabled = s.locked;
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
    for (const cell of [e.seq, e.utc_display || '—', e.ist || '—', e.et || '—',
                        e.engine, e.severity,
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

// All countdowns read the same close_ts and the same interpolated clock, so they
// cannot drift from each other by construction — there is one source, not two.
let clockSkew = 0;   // serverNow - browserNow, measured on each status frame

function tickTimers() {
  if (!state) return;
  const now = Date.now() / 1000 + clockSkew;
  const s = countdown(state.market.close_ts, now);
  $('#timer1').textContent = s;
  $('#timer2').textContent = s;
  $('#timer3').textContent = s;
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
    if (!ok) { say(target, false, data.detail || 'refused'); return; }
    // Human-readable confirmations — never dump raw JSON at the operator.
    const path = btn.dataset.post.split('?')[0];
    let msg;
    if (path === '/stop')        msg = `${data.mode} runtime stopped.`;
    else if (path === '/pause')  msg = `Trading paused. Execution ${data.execution_armed ? 'ARMED' : 'NOT ARMED'}.`;
    else if (path === '/resume') msg = `Trading resumed. Execution ${data.execution_armed ? 'ARMED' : 'NOT ARMED'}.`;
    else                         msg = 'Done.';
    say(target, true, msg);
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
  const msg = ok
    ? `${data.mode} runtime started. Execution ${data.execution_armed ? 'ARMED' : 'NOT ARMED'} — trading does not begin until the Limit Order Engine is armed.`
    : (data.detail || 'refused');
  say($('#control-msg'), ok, msg);
  if (ok) selectedMode = null;
});

// ── MAJORITY START TRADING ────────────────────────────────────────────────────
// Gathers current config from the form and POSTs to action=start, which applies
// the config, ensures the runtime is running (restarts if config changed), and
// arms trading. This is the PRIMARY operator action.
$('#majority-start').addEventListener('click', async () => {
  const body = {};
  $$('#majority-editable input').forEach((i) => {
    // Skip data-switch checkboxes: they are visual toggles only. Their paired
    // hidden input carries the true/false the backend parses. Including them
    // here would add either an empty-string key (no name attr) or a duplicate
    // that overwrites the hidden input's value.
    if (!i.name || i.dataset.switch) return;
    body[i.name] = i.value;
  });
  const { ok, data } = await post(
    '/strategies/MAJORITY/config?action=start', body,
  );
  // §16: human-readable message. Mode label from runtime state (V1/V2), not raw enum.
  const modeLabel = state.runtime.mode === 'V2' ? 'V2 LIVE' : 'V1 PAPER';
  say($('#majority-msg'), ok,
      ok ? `TRADING RUNNING — ${modeLabel} • MAJORITY ENGINE`
         : (data.detail || 'refused'));
});

// ── SAVE MAJORITY CONFIGURATION ──────────────────────────────────────────────
// Saves to disk without starting/restarting. The operator edits, saves, then
// presses START TRADING when ready. Config changes require a restart to take
// effect — that's the honest architectural constraint.
$('#save-majority-config').addEventListener('click', async () => {
  const body = {};
  $$('#majority-editable input').forEach((i) => {
    if (!i.name || i.dataset.switch) return;
    body[i.name] = i.value;
  });
  const { ok, data } = await post('/settings?action=save', body);
  say($('#majority-config-msg'), ok,
      ok ? 'Saved. Press START TRADING to apply.'
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

// ── Active Limit Orders ───────────────────────────────────────────────────────
// §23: one card per active window (non-terminal state). Settled windows are
// removed from this view — they live in the Ledger. Fields come from the
// MAJORITY states_by_window payload; order-level detail (price, fill, shares)
// is not yet exposed per-window by the API and will be added in a follow-up.

function paintActiveOrders() {
  const host = $('#active-orders');
  if (!host) return;
  const states = state.majority?.states_by_window ?? {};
  // Only non-terminal states represent active windows with live orders or
  // pending decisions. Terminal states (FILLED, SETTLED, REJECTED, etc.) belong
  // in the Ledger.
  const entries = Object.entries(states).filter(([_, s]) => !s.terminal);
  if (!entries.length) {
    host.replaceChildren();
    const div = document.createElement('div');
    div.className = 'check';
    div.innerHTML = '<span>No active limit orders.</span>';
    host.appendChild(div);
    return;
  }
  host.replaceChildren(...entries.map(([windowSec, s]) => {
    const div = document.createElement('div');
    div.className = 'check loe-card';
    const remaining = s.close_ts != null
      ? Math.max(0, Math.round(s.close_ts - state.ts)) + 's'
      : '—';
    const row = (label, val) => `<span class="loe-row"><i>${label}</i><b>${val ?? '—'}</b></span>`;
    div.innerHTML =
      `<b>${windowSec}s window</b>` +
      `<span class="dir">${s.selected_side || '—'}</span>` +
      `<i>${s.state}</i>` +
      `<span>${remaining} left</span>` +
      row('Trigger', s.locked_trigger) +
      row('Buffer', s.buffer) +
      row('Limit Price', s.limit_price) +
      row('Fill Price', s.fill_price) +
      row('Shares', s.shares) +
      row('Filled', s.filled_shares) +
      row('Status', s.order_state) +
      row('Retries', s.retry_count) +
      row('Live P&L', s.live_pnl);
    return div;
  }));
}

// ── EMERGENCY CLOSE ALL ───────────────────────────────────────────────────────
// Operator-only. Requires confirmation. Never auto-invoked by the bot.
$('#emergency-close-all').addEventListener('click', async () => {
  const confirmed = window.confirm(
    'EMERGENCY CLOSE ALL TRADES\n\n' +
    'This will stop all trading and disarm the engine. ' +
    'Open positions will settle naturally.\n\n' +
    'Are you sure you want to proceed?',
  );
  if (!confirmed) return;
  // Disarm trading to stop new order submissions
  const { ok, data } = await post('/strategies/MAJORITY/config?action=disarm');
  say($('#close-msg'), ok,
      ok ? 'Trading stopped. Open positions will settle naturally.'
         : (data.detail || 'failed'));
});

// ── Telegram Test Button (§34) ────────────────────────────────────────────────
// Real send attempt, honest outcome. The message text comes from the server so the
// operator sees exactly what the backend determined — not a paraphrase that might
// soften a failure or overstate a success.
$('#telegram-test').addEventListener('click', async () => {
  const { data } = await post('/settings?action=notifications', { test: true });
  say($('#notify-msg'), !!data.ok, data.message || data.detail || 'failed');
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

// ── Saved Configurations ─────────────────────────────────────────────────────
// Named config profiles. Save stores the current settings as a JSON file; Load
// writes that profile into the settings store and reloads the page so the form
// reflects the new values. Loading does NOT start trading.

let savedConfigs = [];

async function loadSavedConfigs() {
  const res = await fetch('/settings?configs=list');
  const data = await res.json();
  savedConfigs = data.configs || [];
  const host = $('#saved-configs-list');
  if (!host) return;
  if (!savedConfigs.length) {
    host.replaceChildren();
    const div = document.createElement('div');
    div.className = 'check';
    div.innerHTML = '<span>No saved configurations yet.</span>';
    host.appendChild(div);
    return;
  }
  host.replaceChildren(...savedConfigs.map((c) => {
    const row = document.createElement('div');
    row.className = 'check pass';
    row.style.cursor = 'pointer';
    row.innerHTML =
      `<b>${c.name}</b> <span>${c.windows || '—'}</span> <i>${hhmmss(c.modified)}</i>`;
    row.addEventListener('click', () => {
      $('#saved-config-name').value = c.name;
      say($('#saved-configs-msg'), true, `Selected: ${c.name}. Press Load to apply.`);
    });
    return row;
  }));
}

$('#save-named-config').addEventListener('click', async () => {
  const name = $('#saved-config-name').value.trim();
  if (!name) {
    say($('#saved-configs-msg'), false, 'Enter a name for the configuration.');
    return;
  }
  const { ok, data } = await post('/settings?action=save_config', { name });
  say($('#saved-configs-msg'), ok,
      ok ? `Saved configuration "${data.name}".` : (data.detail || 'failed'));
  if (ok) loadSavedConfigs();
});

$('#load-named-config').addEventListener('click', async () => {
  const name = $('#saved-config-name').value.trim();
  if (!name) {
    say($('#saved-configs-msg'), false, 'Enter or select a configuration to load.');
    return;
  }
  const { ok, data } = await post('/settings?action=load_config', { name });
  if (ok) {
    say($('#saved-configs-msg'), true,
        `Loaded "${name}". Press START TRADING to apply.`);
    // Reload so the form rebuilds with the newly loaded values.
    setTimeout(() => window.location.reload(), 600);
  } else {
    say($('#saved-configs-msg'), false, data.detail || 'failed');
  }
});

// ── ledger ───────────────────────────────────────────────────────────────────

function dualTime(row, key) {
  const d = row[`${key}_display`];
  if (!d || !d.utc) return '—';
  return `${d.utc_display} / ${d.ist} / ${d.et}`;
}

// §35 columns — slim, human-readable. Full detail lives in the expandable row.
const LEDGER_COLUMNS = [
  'intent_id', 'submission_time', 'market', 'window', 'offset_seconds',
  'direction', 'order_price', 'locked_trigger', 'quantity', 'local_order_id',
  'fill_price', 'settlement_result', 'pnl', 'state_display',
];

function ledgerRangeEpochs(preset) {
  const now = Math.floor(Date.now() / 1000);
  if (preset === 'today') {
    const d = new Date(); d.setHours(0, 0, 0, 0);
    return { since: Math.floor(d.getTime() / 1000), until: null };
  }
  if (preset === 'yesterday') {
    const d = new Date(); d.setDate(d.getDate() - 1); d.setHours(0, 0, 0, 0);
    const start = Math.floor(d.getTime() / 1000);
    return { since: start, until: start + 86400 };
  }
  if (preset === '7d') return { since: now - 7 * 86400, until: null };
  if (preset === '30d') return { since: now - 30 * 86400, until: null };
  return { since: null, until: null };
}

function ledgerQuery() {
  const p = new URLSearchParams();
  const q = $('#led-q').value.trim();
  if (q) p.set('q', q);
  const side = $('#led-side').value;
  if (side) p.set('direction', side);
  const status = $('#led-status').value;
  if (status) p.set('status', status);
  const range = ledgerRangeEpochs($('#led-range').value);
  if (range.since != null) p.set('since', String(range.since));
  if (range.until != null) p.set('until', String(range.until));
  return p;
}

// Plain-English explanations from existing record fields. No backend change needed.
function whyExplanations(r) {
  const lines = [];
  // WHY IT OPENED — use the backend-provided buffer status display value directly.
  if (r.buffer_status === 'SATISFIED') {
    lines.push(`Opened because buffer was satisfied (${r.window}).`);
  } else if (r.rejection_display && r.state_display !== 'Submitted') {
    lines.push(`Did not open: ${r.rejection_display}.`);
  } else if (r.buffer_status === 'WAITING') {
    lines.push('Window is still waiting for conditions to evaluate.');
  }
  // WHY THIS SIDE — direction is a backend value; we only display it, never assign.
  // Only meaningful when an order was actually placed (fill exists).
  if (r.direction && r.fill_price) {
    lines.push(`Direction ${r.direction} chosen based on majority signal at freeze.`);
  }
  // WHY IT FILLED
  if (r.fill_price && r.filled_quantity) {
    lines.push(`Filled ${r.filled_quantity} shares at ${r.fill_price} via limit order.`);
  } else if (r.state_display === 'Submitted') {
    lines.push('Order submitted but not yet filled.');
  }
  // WHY IT SETTLED THIS WAY
  if (r.settlement_result && r.settlement_result.indexOf('UNRESOLVED') === -1) {
    const pnlText = r.pnl ? ` P&L: ${r.pnl}.` : '';
    lines.push(`Settled as ${r.settlement_result}.${pnlText}`);
  }
  // Rejection explanation (for actual order rejections, distinct from buffer/direction skips above)
  if (r.rejection_display && r.state_display === 'Rejected') {
    lines.push(`Rejection: ${r.rejection_display}`);
  }
  return lines.length ? lines.join('\n') : 'No additional context.';
}

async function loadLedger() {
  const p = ledgerQuery();
  const res = await fetch(`/history?${p}`);
  const data = await res.json();
  const csv = new URLSearchParams(p); csv.set('format', 'csv');
  $('#led-csv').href = `/history?${csv}`;

  // Summary row (§35)
  const t = data.totals || {};
  const paper = state?.paper;
  const summaryEl = $('#ledger-summary');
  if (summaryEl) {
    summaryEl.innerHTML = `
      <div><span class="lbl">Total Trades</span><b>${text(t.filled_orders)}</b></div>
      <div><span class="lbl">Wins</span><b class="yes">${text(t.win_count)}</b></div>
      <div><span class="lbl">Losses</span><b class="no">${text(t.loss_count)}</b></div>
      <div><span class="lbl">Skipped</span><b>${text(t.buffer_not_satisfied)}</b></div>
      <div><span class="lbl">Rejected</span><b>${text(t.rejected_orders)}</b></div>
      <div><span class="lbl">Markets</span><b>${text(t.markets_processed)}</b></div>
      ${paper ? `<div><span class="lbl">Paper P&amp;L</span><b>${text(paper.realized_pnl)}</b></div>` : ''}
    `;
  }

  $('#ledger-table tbody').replaceChildren(...(data.records || []).map((r) => {
    const tr = document.createElement('tr');
    tr.className = (r.state_display || '').toLowerCase().replace(/\s+/g, '-');
    tr.style.cursor = 'pointer';
    for (const key of LEDGER_COLUMNS) {
      const td = document.createElement('td');
      if (key === 'submission_time') {
        td.textContent = dualTime(r, 'submission_time');
      } else if (key === 'offset_seconds') {
        td.textContent = `${r.offset_seconds}s`;
      } else {
        td.textContent = text(r[key]);
      }
      tr.append(td);
    }
    // Expandable detail row (§35)
    tr.addEventListener('click', () => {
      const next = tr.nextElementSibling;
      if (next && next.classList.contains('ledger-detail')) {
        next.remove();
        return;
      }
      const detail = document.createElement('tr');
      detail.className = 'ledger-detail';
      const td = document.createElement('td');
      td.colSpan = LEDGER_COLUMNS.length;
      const ids = [
        r.intent_id && `Intent: ${r.intent_id}`,
        r.trace_id && `Trace: ${r.trace_id}`,
        r.local_order_id && `Local Order: ${r.local_order_id}`,
        r.venue_order_id && `Venue Order: ${r.venue_order_id}`,
      ].filter(Boolean).join(' · ');
      const times = [
        r.submission_time_display?.utc_display && `Submitted UTC: ${r.submission_time_display.utc_display}`,
        r.fill_time_display?.utc_display && `Filled UTC: ${r.fill_time_display.utc_display}`,
        r.settlement_time_display?.utc_display && `Settled UTC: ${r.settlement_time_display.utc_display}`,
        r.ptb && `PTB: ${r.ptb}`,
        r.signal_twap && `Signal TWAP: ${r.signal_twap}`,
        r.settlement_twap && `Settlement TWAP: ${r.settlement_twap}`,
        r.buffer && `Buffer: ${r.buffer}`,
      ].filter(Boolean).join(' · ');
      td.innerHTML = `<div class="ledger-detail-inner">
        <div class="ids">${ids}</div>
        <div class="times">${times}</div>
        <pre class="why">${whyExplanations(r)}</pre>
      </div>`;
      detail.append(td);
      tr.after(detail);
    });
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

// ── workspaces ───────────────────────────────────────────────────────────────

function show(name) {
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.ws === name));
  $$('.ws').forEach((w) => w.classList.toggle('active', w.id === `ws-${name}`));
  if (name === 'ledger') loadLedger();
  if (name === 'analytics') { loadSnapshots(); loadPerformance(); }
  if (name === 'system') { loadSnapshots(); }
  if (name === 'tank') loadBook();
}

// §49 Order Book panel. Fetched on tab open so the operator sees what the engine
// would join at this instant, not a stale value from a prior frame. Two calls
// because UP and DOWN are separate books on Polymarket.
async function loadBook() {
  const fill = (id, val) => { const el = $(id); if (el) el.textContent = text(val); };
  try {
    const [up, down] = await Promise.all([
      fetch('/orderbook?direction=UP').then(r => r.ok ? r.json() : {}),
      fetch('/orderbook?direction=DOWN').then(r => r.ok ? r.json() : {}),
    ]);
    fill('#book-up-bid', up.best_bid);
    fill('#book-up-limit', up.passive_limit);
    fill('#book-down-bid', down.best_bid);
    fill('#book-down-limit', down.passive_limit);
    fill('#book-tick', up.tick_size || down.tick_size);
  } catch (_) { /* silent: book is informational, not operational */ }
}

// ── performance panel ────────────────────────────────────────────────────────
// Reads /history for totals that the wallet payload doesn't expose (skips,
// fills, rejected, average fill latency). Win rate is computed here from
// server-provided integers only — no float arithmetic on business values.
async function loadPerformance() {
  try {
    const res = await fetch('/history');
    const data = await res.json();
    const t = data.totals || {};
    $('#an-skips').textContent = text(t.buffer_not_satisfied);
    $('#an-filled').textContent = text(t.filled_orders);
    $('#an-rejected').textContent = text(t.rejected_orders);
    $('#an-fill-latency').textContent = t.average_fill_seconds != null
      ? `${t.average_fill_seconds}s` : '—';
    // §47 extended analytics. All values pre-computed on the backend; no
    // arithmetic here because the UI blindness contract forbids it.
    $('#an-total-trades').textContent = text(t.total_trades);
    $('#an-realized-pnl').textContent = text(t.realized_pnl);
    $('#an-avg-trade').textContent = text(t.average_trade_pnl);
    $('#an-up-side').textContent = t.up_wins != null && t.up_losses != null
      ? `${text(t.up_wins)} / ${text(t.up_losses)}` : '—';
    $('#an-down-side').textContent = t.down_wins != null && t.down_losses != null
      ? `${text(t.down_wins)} / ${text(t.down_losses)}` : '—';
  } catch (e) { /* silent: wallet bindings still render */ }
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
  // Load saved configs on initial page load
  loadSavedConfigs();
}).catch(() => setLive(false));

connect();
