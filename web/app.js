'use strict';
// Receiver screen: status, what arrived from the iPhone, the guided Health Auto Export setup, Rotate token, Disconnect.
// Reads only this app's documented relative API. Health data itself is shown by the Health app.
const NAME = document.title;
const APP = document.documentElement.dataset.app;
const el = id => document.getElementById(id);
let S = null, SETUP = null, pollTimer = null, showSetup = false, stepOpenedAt = 0;

function say(text, kind = '') {
  const s = el('status');
  s.classList.toggle('wz-sr', !kind);
  s.classList.toggle('note', kind === 'note');
  s.textContent = text;
}
async function api(path, data) {
  const init = data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
  const r = await fetch(path, init);
  let body = {};
  try { body = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(body.error || `The ${NAME} app could not do that. Try again.`);
  return body;
}
function h(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
const dayLabel = (iso, opts = { day: 'numeric', month: 'short', year: 'numeric' }) => iso ? new Date(iso + 'T12:00:00').toLocaleDateString(undefined, opts) : '';
function ago(iso) {
  if (!iso) return 'never';
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return 'just now'; if (m < 60) return m + ' min ago';
  const hrs = Math.round(m / 60); return hrs < 36 ? hrs + ' h ago' : Math.round(hrs / 24) + ' days ago';
}
const n = (v, one, many) => `${v || 0} ${(v || 0) === 1 ? one : many}`;
const setHref = (id, v) => { const e = el(id); if (e && v) e.href = v; };

// ---------- opening the Health app ----------
// Inside Wizard both shells are same-origin and answer the documented wizard:open event (native bridge v0);
// anywhere else the plain link opens Health in this window.
for (const id of ['open-health', 'open-health-2']) {
  el(id).addEventListener('click', e => {
    try {
      const top = window.top;
      if (top !== window && top.wizardUX && top.location.origin === location.origin) {
        top.dispatchEvent(new CustomEvent('wizard:open', { detail: { app: 'wizard-health', path: '/wizard-health/' } }));
        e.preventDefault();
      }
    } catch (_) { /* not same-origin: follow the link */ }
  });
}

// ---------- copy buttons ----------
async function copyFrom(input, btn) {
  const text = input.value;
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch (e) {
    input.focus(); input.select();
    try { ok = document.execCommand('copy'); } catch (_) {}
  }
  const was = btn.textContent;
  btn.textContent = ok ? 'Copied' : 'Selected';
  setTimeout(() => { btn.textContent = was; }, 1500);
  if (!ok) say('Selected. Use your device\u2019s copy.', 'note');
}
document.addEventListener('click', e => {
  const b = e.target.closest('[data-copy]');
  if (b) copyFrom(el(b.dataset.copy), b);
  const nx = e.target.closest('[data-next]');
  if (nx) showStep(+nx.dataset.next);
});

// ---------- guided setup ----------
// The iPhone posts to this app through the box's private tailnet address. Opened through Wizard over https the
// screen already sits on that address; otherwise the server suggests the box's ts.net name.
function boxUrl(path) {
  if (location.protocol === 'https:') return location.origin + path;
  if (SETUP && SETUP.push_url_hint) return SETUP.push_url_hint.replace(/\/[^/]+\/api\/push$/, '') + path;
  return location.origin + path;
}
function settingsList(dl, rows) {
  dl.replaceChildren();
  for (const [k, v] of rows) dl.append(h('dt', '', k), h('dd', '', v));
}
function showStep(k) {
  for (const li of document.querySelectorAll('.hl-step')) {
    const on = +li.dataset.step === k;
    li.classList.toggle('open', on);
    li.querySelector('.hl-step-h').setAttribute('aria-expanded', String(on));
  }
  if (k === 4) { stepOpenedAt = Date.now(); poll(3000); }
  const li = document.querySelector(`.hl-step[data-step="${k}"]`);
  if (li) requestAnimationFrame(() => li.scrollIntoView({ block: 'nearest', behavior: 'smooth' }));
}
for (const li of document.querySelectorAll('.hl-step')) li.querySelector('.hl-step-h').onclick = () => showStep(+li.dataset.step);

function renderSetup() {
  setHref('hae-link', SETUP.hae_url);
  setHref('ts-link', SETUP.tailscale_url);
  el('v-prices').textContent = SETUP.prices.replace(/^Premium is /, '');
  el('v-ping').value = boxUrl(SETUP.ping_path);
  el('v-name').value = SETUP.automation_name;
  el('v-url').value = boxUrl(SETUP.push_path);
  el('v-hkey').value = SETUP.header_name;
  el('v-hval').value = SETUP.header_value;
  el('v-metrics').replaceChildren(...SETUP.metrics.map(m => h('li', 'wz-chip' + (m.needed ? '' : ' hl-opt'), m.label)));
  el('v-metrics-all').value = SETUP.metrics_text;
  settingsList(el('v-settings'), SETUP.settings);
  settingsList(el('v-wsettings'), SETUP.workout_settings);
}

function renderLive() {
  const su = (S && S.setup) || {};
  const lp = su.last_push;
  const got = !!lp;
  const live = el('live');
  live.dataset.state = got ? 'ok' : 'waiting';
  el('live-title').textContent = got ? 'Received data from your iPhone' : 'Waiting for your iPhone\u2026';
  el('live-sub').textContent = got
    ? `${ago(lp.at)} \u00b7 ${n(lp.days, 'day', 'days')}${lp.first_day && lp.last_day && lp.first_day !== lp.last_day ? ` (${dayLabel(lp.first_day, { day: 'numeric', month: 'short' })} to ${dayLabel(lp.last_day, { day: 'numeric', month: 'short' })})` : ''}, ${n(lp.records, 'record', 'records')}`
    : 'This updates by itself.';
  el('live-actions').hidden = !got;
  const rej = su.last_rejected;
  const fresh = rej && (!lp || new Date(rej.at) > new Date(lp.at));
  el('live-problem').hidden = !fresh;
  el('live-problem').textContent = fresh ? `Your iPhone tried ${ago(rej.at)}, but it was refused: ${rej.reason}` : '';
  const done = { 1: got, 2: got, 3: got, 4: got };
  for (const li of document.querySelectorAll('.hl-step')) li.classList.toggle('done', !!done[+li.dataset.step]);
}

// ---------- sheets ----------
el('open-rotate').onclick = () => WzSheet.open(el('sheet-rotate'));
el('sr-go').onclick = async () => {
  const b = el('sr-go'); b.disabled = true;
  try {
    SETUP = await api('api/token/rotate', {});
    WzSheet.close(el('sheet-rotate'));
    say('New token made. Paste the new header value into Health Auto Export.', 'note');
    showSetup = true;
    await refresh();
    showStep(3);
  } catch (err) { say(err.message, 'error'); }
  finally { b.disabled = false; }
};
el('open-disconnect').onclick = () => WzSheet.open(el('sheet-disconnect'));
el('sd-go').onclick = async () => {
  const b = el('sd-go'); b.disabled = true;
  try {
    await api('api/disconnect', {});
    WzSheet.close(el('sheet-disconnect'));
    say(`${NAME} disconnected.`, 'note');
    SETUP = null; showSetup = false;
    el('setup').dataset.opened = '';
    await refresh();
  } catch (err) { say(err.message, 'error'); }
  finally { b.disabled = false; }
};
el('show-setup').onclick = () => {
  showSetup = !showSetup;
  render();
  if (showSetup) showStep(3);
};

// ---------- status ----------
const received = () => S && S.status === 'connected';
function connLine() {
  if (!received()) return (S.setup && S.setup.last_rejected) ? 'Your iPhone was refused. See step 4.' : 'Set up below so Health can show your Apple Health data.';
  return 'Last data ' + ago(S.last_sync);
}
function render() {
  const pill = el('conn-pill');
  pill.textContent = received() ? 'Receiving' : 'Not set up';
  pill.className = 'wz-pill ' + (received() ? (S.error ? 'warn' : 'ok') : 'off');
  el('conn-line').textContent = connLine();
  const c = S.counts || {}, su = S.setup || {};
  el('account').hidden = !received();
  if (received()) {
    const lp = su.last_push || {};
    el('acct-name').textContent = 'Your iPhone';
    el('acct-sub').textContent = S.history_from ? `History from ${dayLabel(S.history_from)} \u00b7 ${n(c.days, 'day', 'days')}, ${n(c.sleeps, 'sleep', 'sleeps')}, ${n(c.workouts, 'workout', 'workouts')}` : 'No days yet';
    el('sync-title').textContent = 'Last data ' + ago(S.last_sync);
    el('sync-sub').textContent = `${n(lp.records, 'record', 'records')} over ${n(lp.days, 'day', 'days')}` + (lp.automation ? ` from \u201c${lp.automation}\u201d` : '');
    el('show-setup').textContent = showSetup ? 'Hide setup steps' : 'Setup steps';
  }
  el('sync-error').hidden = !(received() && S.error);
  el('sync-error').textContent = S.error || '';
  el('more').hidden = !received() && !su.last_rejected;
  const dl = el('details'); dl.replaceChildren();
  const rows = [['Status', received() ? 'Receiving from your iPhone' : 'Waiting for the first data'],
    ['Last data', S.last_sync ? new Date(S.last_sync).toLocaleString() : 'never'],
    ['History from', S.history_from ? dayLabel(S.history_from) : '\u2013'], ['Latest day', S.latest_date ? dayLabel(S.latest_date) : '\u2013'],
    ['Saved here', `${n(c.days, 'day', 'days')}, ${n(c.sleeps, 'sleep', 'sleeps')}, ${n(c.workouts, 'workout', 'workouts')}`],
    ['Deliveries', String(su.pushes || 0)], ['URL', SETUP ? boxUrl(SETUP.push_path) : '\u2013'],
    ['Header', su.header_name || '\u2013'], ['Token made', su.token_created_at ? new Date(su.token_created_at).toLocaleString() : '\u2013'],
    ['Shared as', 'health data for Health (read-only, box-only)'], ['App version', S.version]];
  if (su.last_rejected) rows.push(['Last refused', `${new Date(su.last_rejected.at).toLocaleString()}: ${su.last_rejected.reason}`]);
  for (const [k, v] of rows) dl.append(h('dt', '', k), h('dd', '', v));
  const setup = !received() || showSetup;
  el('setup').hidden = !setup;
  if (setup && SETUP) {
    renderSetup();
    renderLive();
    if (!el('setup').dataset.opened) { el('setup').dataset.opened = '1'; showStep(received() ? 3 : (su.last_rejected ? 4 : 1)); }
  }
}

async function refresh() {
  try {
    S = await api('api/status');
    if (!SETUP) SETUP = await api('api/setup');
    render();
  } catch (err) { say(err.message, 'error'); }
}
function poll(ms) {
  clearTimeout(pollTimer);
  const tick = async () => {
    await refresh();
    const waiting = !el('setup').hidden && document.querySelector('.hl-step.open[data-step="4"]') && Date.now() - stepOpenedAt < 30 * 60000;
    pollTimer = setTimeout(tick, waiting ? 3000 : 60000);
  };
  pollTimer = setTimeout(tick, ms);
}

// Domain-only Back: sheets push their own entries through wizard-sheet.js; the screen itself is the root.
if (!window.WizardApp || WizardApp.version !== 1) {
  say(`Wizard system functions are unavailable. Open ${NAME} through Wizard.`, 'error');
} else {
  WizardApp.ready({ name: APP, version: document.documentElement.dataset.version || '' });
  // Setup steps opened over the receiving view close first; otherwise the screen is the root.
  WizardApp.back.register({
    canGoBack: () => !!(showSetup && received()),
    back: () => { if (!(showSetup && received())) return false; showSetup = false; render(); return true; },
  });
}
refresh().then(() => poll(60000));
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
