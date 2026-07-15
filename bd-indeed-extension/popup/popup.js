// Popup — a thin view over background state. All orchestration lives in the
// service worker; the popup just sends commands and renders `run` from storage.

import { extractJobs } from '../lib/csv.js';

const $ = (id) => document.getElementById(id);
const send = (msg) => new Promise((resolve) => chrome.runtime.sendMessage(msg, (r) => {
  if (chrome.runtime.lastError) resolve({ error: chrome.runtime.lastError.message });
  else resolve(r || {});
}));

// Resume mode toggle: 'tailored' (pre-tailor the whole queue) or 'base' (skip
// tailoring, upload the base resume for every job — faster, no LLM cost).
let _resumeMode = 'tailored';
function setResumeMode(m) {
  _resumeMode = (m === 'base') ? 'base' : 'tailored';
  const t = $('resumeTailored'); const b = $('resumeBase');
  if (t) t.classList.toggle('active', _resumeMode === 'tailored');
  if (b) b.classList.toggle('active', _resumeMode === 'base');
}
$('resumeTailored')?.addEventListener('click', () => setResumeMode('tailored'));
$('resumeBase')?.addEventListener('click', () => setResumeMode('base'));

const state = {
  candidates: [],
  dbJobs: [],
  rescueJobs: [],
  csvJobs: [],
  pastedJobs: [],
  csvName: '',
  view: null,
};

// The popup is torn down every time it loses focus — persist the setup
// selections so the BD agent doesn't re-fetch jobs / re-upload the CSV.
async function persistDraft() {
  await chrome.storage.local.set({
    setupDraft: {
      candidateId: $('candidateSelect').value || null,
      dbJobs: state.dbJobs,
      rescueJobs: state.rescueJobs,
      csvJobs: state.csvJobs,
      pastedJobs: state.pastedJobs,
      csvName: state.csvName,
    },
  });
}

async function restoreDraft() {
  const { setupDraft: d } = await chrome.storage.local.get('setupDraft');
  if (!d) return;
  if (d.candidateId && state.candidates.some(c => c.id === d.candidateId)) {
    $('candidateSelect').value = d.candidateId;
  }
  state.dbJobs = d.dbJobs || [];
  state.rescueJobs = d.rescueJobs || [];
  state.csvJobs = d.csvJobs || [];
  state.pastedJobs = d.pastedJobs || [];
  state.csvName = d.csvName || '';
  if (state.dbJobs.length) $('dbJobsInfo').textContent = `${state.dbJobs.length} Indeed job(s) loaded (restored).`;
  if (state.rescueJobs.length) $('rescueInfo').textContent = `${state.rescueJobs.length} blocked/failed job(s) to rescue (restored).`;
  if (state.csvJobs.length) $('csvInfo').textContent = `${state.csvJobs.length} URL(s) from ${state.csvName || 'CSV'} (restored).`;
  if (state.pastedJobs.length) $('pasteInfo').textContent = `${state.pastedJobs.length} pasted URL(s) (restored).`;
  updateStartEnabled();
}

// ── view switching ─────────────────────────────────────────────────────────

function show(view) {
  state.view = view;
  for (const v of ['login', 'setup', 'run', 'settings']) {
    $(`view-${v}`).hidden = v !== view;
  }
}

async function refresh() {
  const st = await send({ type: 'GET_STATE' });
  if (st.error) {
    show('login');
    showError('loginError', st.error);
    return;
  }
  if (state.view === 'settings') return; // don't yank the user out of settings

  if (!st.authed) {
    show('login');
    return;
  }
  if (st.run && !['idle'].includes(st.run.status)) {
    renderRun(st.run);
    show('run');
    return;
  }
  $('whoami').textContent = st.email || '';
  fillSettingsForm(st.settings);
  $('maxJobs').value = st.settings.maxJobs;
  $('autoSubmit').checked = !!st.settings.autoSubmit;
  setResumeMode(st.settings.resumeMode === 'base' ? 'base' : 'tailored');
  show('setup');
  if (!state.candidates.length) loadCandidates();
}

function showError(id, msg) {
  const el = $(id);
  el.textContent = msg;
  el.hidden = !msg;
}

// ── login ──────────────────────────────────────────────────────────────────

$('loginBtn').addEventListener('click', async () => {
  showError('loginError', '');
  $('loginBtn').disabled = true;
  $('loginBtn').textContent = 'Signing in…';
  const res = await send({ type: 'LOGIN', email: $('loginEmail').value.trim(), password: $('loginPassword').value });
  $('loginBtn').disabled = false;
  $('loginBtn').textContent = 'Sign in';
  if (res.error) { showError('loginError', res.error); return; }
  refresh();
});

$('logoutBtn').addEventListener('click', async () => {
  await send({ type: 'LOGOUT' });
  refresh();
});

// ── setup ──────────────────────────────────────────────────────────────────

async function loadCandidates() {
  const sel = $('candidateSelect');
  const res = await send({ type: 'FETCH_CANDIDATES' });
  if (res.error) {
    sel.innerHTML = '<option value="">Failed to load candidates</option>';
    showError('setupError', res.error);
    if (res.authError) show('login');
    return;
  }
  state.candidates = res.candidates || [];
  sel.innerHTML = '<option value="">— select candidate —</option>' +
    state.candidates.map(c => `<option value="${c.id}">${escapeHtml(c.name)} (${escapeHtml(c.email || '')})</option>`).join('');
  await restoreDraft();
  updateStartEnabled();
}

function selectedCandidate() {
  return state.candidates.find(c => c.id === $('candidateSelect').value) || null;
}

$('candidateSelect').addEventListener('change', () => {
  state.dbJobs = []; // DB/rescue jobs are candidate-specific; CSV list is kept
  state.rescueJobs = [];
  $('dbJobsInfo').textContent = 'No jobs loaded yet.';
  $('rescueInfo').textContent = '';
  updateStartEnabled();
  persistDraft();
});

$('fetchRescueBtn').addEventListener('click', async () => {
  const cand = selectedCandidate();
  showError('setupError', '');
  if (!cand) { showError('setupError', 'Select a candidate first.'); return; }
  $('fetchRescueBtn').disabled = true;
  $('fetchRescueBtn').textContent = 'Fetching…';
  const res = await send({ type: 'FETCH_RESCUE_JOBS', candidateId: cand.id, limit: 50 });
  $('fetchRescueBtn').disabled = false;
  $('fetchRescueBtn').textContent = 'Rescue blocked';
  if (res.error) { showError('setupError', res.error); return; }
  state.rescueJobs = res.jobs || [];
  $('rescueInfo').textContent = state.rescueJobs.length
    ? `${state.rescueJobs.length} blocked/failed job(s) the agent couldn't finish — will retry here.`
    : 'No blocked/failed applications for this candidate. 🎉';
  updateStartEnabled();
  persistDraft();
});

$('fetchJobsBtn').addEventListener('click', async () => {
  const cand = selectedCandidate();
  showError('setupError', '');
  if (!cand) { showError('setupError', 'Select a candidate first.'); return; }
  $('fetchJobsBtn').disabled = true;
  $('fetchJobsBtn').textContent = 'Fetching…';
  const res = await send({ type: 'FETCH_DB_JOBS', candidateId: cand.id, limit: 100 });
  $('fetchJobsBtn').disabled = false;
  $('fetchJobsBtn').textContent = 'Fetch from database';
  if (res.error) { showError('setupError', res.error); return; }
  state.dbJobs = res.jobs || [];
  $('dbJobsInfo').textContent = state.dbJobs.length
    ? `${state.dbJobs.length} Indeed job(s) found in database (best match first).`
    : 'No Indeed jobs in the database — use the CSV fallback below.';
  updateStartEnabled();
  persistDraft();
});

$('csvInput').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) { state.csvJobs = []; state.csvName = ''; $('csvInfo').textContent = ''; updateStartEnabled(); persistDraft(); return; }
  const text = await file.text();
  state.csvJobs = extractJobs(text);
  state.csvName = file.name;
  $('csvInfo').textContent = state.csvJobs.length
    ? `${state.csvJobs.length} Indeed URL(s) parsed from ${file.name}.`
    : `No Indeed URLs found in ${file.name}.`;
  updateStartEnabled();
  persistDraft();
});

function combinedJobs() {
  // Rescue jobs first (highest value — otherwise lost), then DB jobs
  // (relevance-ordered by the backend), then pasted URLs, then CSV rows.
  const out = [];
  const seen = new Set();
  for (const j of [...state.rescueJobs, ...state.dbJobs, ...state.pastedJobs, ...state.csvJobs]) {
    const key = canonical(j.url);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(j);
  }
  return out;
}

const canonical = (u) => {
  try {
    const url = new URL(u);
    const jk = url.searchParams.get('jk');
    return jk ? `jk:${jk}` : (url.origin + url.pathname);
  } catch { return u; }
};

function refreshSourceLines() {
  $('clearDbBtn').hidden = !state.dbJobs.length;
  $('clearRescueBtn').hidden = !state.rescueJobs.length;
  $('clearCsvBtn').hidden = !state.csvJobs.length;
  $('clearPastedBtn').hidden = !state.pastedJobs.length;
}

function updateStartEnabled() {
  refreshSourceLines();
  $('startBtn').disabled = !(selectedCandidate() && combinedJobs().length);
  const n = combinedJobs().length;
  $('startBtn').textContent = n ? `Start applying (${Math.min(n, Number($('maxJobs').value) || 10)} of ${n} jobs)` : 'Start applying';
}

$('clearDbBtn').addEventListener('click', () => {
  state.dbJobs = []; $('dbJobsInfo').textContent = 'No jobs loaded yet.';
  updateStartEnabled(); persistDraft();
});
$('clearRescueBtn').addEventListener('click', () => {
  state.rescueJobs = []; $('rescueInfo').textContent = '';
  updateStartEnabled(); persistDraft();
});
$('clearCsvBtn').addEventListener('click', () => {
  state.csvJobs = []; state.csvName = ''; $('csvInfo').textContent = ''; $('csvInput').value = '';
  updateStartEnabled(); persistDraft();
});

function isHttpUrl(v) {
  try { const u = new URL(v.trim()); return u.protocol === 'http:' || u.protocol === 'https:'; }
  catch { return false; }
}

$('addPastedBtn').addEventListener('click', () => {
  const text = $('pasteUrls').value.trim();
  if (!text) { $('pasteInfo').textContent = 'Paste one or more URLs first.'; return; }
  // Split strictly by line (URLs can contain commas in query params).
  const lines = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
  const seen = new Set(state.pastedJobs.map(j => canonical(j.url)));
  let added = 0, rejected = 0;
  for (const line of lines) {
    if (!isHttpUrl(line)) { rejected++; continue; }
    const k = canonical(line);
    if (seen.has(k)) continue;
    seen.add(k); state.pastedJobs.push({ url: line, title: null, company: null }); added++;
  }
  $('pasteInfo').textContent = `${state.pastedJobs.length} pasted URL(s) queued`
    + (added ? ` (+${added} new)` : '')
    + (rejected > 0 ? ` — ${rejected} line(s) ignored (not valid URLs)` : '');
  $('pasteUrls').value = '';
  updateStartEnabled(); persistDraft();
});

$('clearPastedBtn').addEventListener('click', () => {
  state.pastedJobs = []; $('pasteInfo').textContent = ''; $('pasteUrls').value = '';
  updateStartEnabled(); persistDraft();
});
$('maxJobs').addEventListener('input', updateStartEnabled);

$('startBtn').addEventListener('click', async () => {
  const cand = selectedCandidate();
  const jobs = combinedJobs();
  if (!cand || !jobs.length) return;
  showError('setupError', '');
  $('startBtn').disabled = true;
  const res = await send({
    type: 'START_RUN',
    candidate: cand,
    jobs,
    // Portal password now comes from the backend candidate record (fetchPortalCredentials)
    // — no manual field in the popup.
    sitePassword: null,
    settingsPatch: {
      maxJobs: Math.max(1, Number($('maxJobs').value) || 10),
      autoSubmit: $('autoSubmit').checked,
      resumeMode: _resumeMode,
    },
  });
  if (res.error) {
    showError('setupError', res.error);
    $('startBtn').disabled = false;
    return;
  }
  refresh();
});

// ── run view ───────────────────────────────────────────────────────────────

const ICONS = {
  pending: '·', active: '▶', applied: '✓', failed: '✗', skipped: '⤼', needs_human: '⚠',
};

function renderRun(run) {
  $('runCandidate').textContent = run.candidate?.name || '';
  const pill = $('runStatus');
  pill.textContent = run.status.replace('_', ' ');
  pill.className = `pill ${run.status}`;

  const counts = run.jobs.reduce((a, j) => { a[j.status] = (a[j.status] || 0) + 1; return a; }, {});
  $('runCounts').textContent =
    `${counts.applied || 0} applied · ${counts.failed || 0} failed · ${counts.skipped || 0} skipped · ${counts.pending || 0} left`;

  const att = $('attention');
  if (run.status === 'awaiting_human' && run.attention) {
    att.hidden = false;
    att.textContent = `⚠ ${run.attention.reason}`;
  } else att.hidden = true;

  const list = $('jobList');
  list.innerHTML = run.jobs.map((j, i) => `
    <li class="${j.status === 'active' ? 'active' : ''}">
      <span class="st">${ICONS[j.status] || '·'}</span>
      <span class="t">
        <span class="title">${escapeHtml(j.title || j.url)}</span>
        ${j.company ? `<span class="note">${escapeHtml(j.company)}</span>` : ''}
        ${j.note ? `<span class="note">${escapeHtml(j.note)}</span>` : ''}
      </span>
      ${j.status === 'applied'
        ? '<span class="note done-tag">✓ applied</span>'
        : `<button class="link-btn mark-one" data-idx="${i}" title="Record this job as SUBMITTED in the backend">Mark applied</button>`}
    </li>`).join('');

  const running = ['running', 'awaiting_human'].includes(run.status);
  $('pauseBtn').hidden = !running;
  $('resumeBtn').hidden = run.status !== 'paused';
  $('markAppliedBtn').hidden = !running || run.currentIndex < 0;
  $('skipBtn').hidden = !running || run.currentIndex < 0;
  $('stopBtn').hidden = ['done', 'stopped'].includes(run.status);
  $('newRunBtn').hidden = !['done', 'stopped'].includes(run.status);

  const logEl = $('logView');
  logEl.innerHTML = (run.log || []).slice(-80).map(l =>
    `<div class="${l.level}">${new Date(l.ts).toLocaleTimeString()} ${escapeHtml(l.msg)}</div>`).join('');
  logEl.scrollTop = logEl.scrollHeight;
}

$('pauseBtn').addEventListener('click', async () => { await send({ type: 'PAUSE_RUN' }); refresh(); });
$('resumeBtn').addEventListener('click', async () => { await send({ type: 'RESUME_RUN' }); refresh(); });
$('skipBtn').addEventListener('click', async () => { await send({ type: 'SKIP_CURRENT_JOB' }); refresh(); });
$('markAppliedBtn').addEventListener('click', async () => { await send({ type: 'MARK_APPLIED' }); refresh(); });
// Per-row "Mark applied" (delegated — the list re-renders). Records that job as
// SUBMITTED in the backend even if it previously failed in the browser.
$('jobList').addEventListener('click', async (e) => {
  const btn = e.target.closest('.mark-one');
  if (!btn) return;
  const idx = Number(btn.dataset.idx);
  btn.disabled = true; btn.textContent = 'Marking…';
  const res = await send({ type: 'MARK_APPLIED', jobIndex: idx });
  if (res && res.ok === false && res.detail) { btn.textContent = 'Mark applied'; btn.disabled = false; }
  refresh();
});
$('stopBtn').addEventListener('click', async () => { await send({ type: 'STOP_RUN' }); refresh(); });
$('newRunBtn').addEventListener('click', async () => { await send({ type: 'CLEAR_RUN' }); state.dbJobs = []; state.rescueJobs = []; state.csvJobs = []; state.pastedJobs = []; refresh(); });

// ── settings ───────────────────────────────────────────────────────────────

function fillSettingsForm(s) {
  $('setSalary').value = s.desiredSalary || '';
  $('setHours').value = s.desiredHours || '';
  $('setDefaultLocation').value = s.defaultLocation || '';
  $('setDelay').value = s.jobDelaySec ?? 8;
  $('setStall').value = s.stallMinutes ?? 5;
}

$('settingsBtn').addEventListener('click', async () => {
  const st = await send({ type: 'GET_STATE' });
  if (st.settings) fillSettingsForm(st.settings);
  show('settings');
});

$('backBtn').addEventListener('click', () => { state.view = null; refresh(); });

$('saveSettingsBtn').addEventListener('click', async () => {
  const res = await send({
    type: 'SAVE_SETTINGS',
    patch: {
      desiredSalary: $('setSalary').value.trim() || '85000',
      desiredHours: $('setHours').value.trim() || '40',
      defaultLocation: $('setDefaultLocation').value.trim() || 'United States',
      jobDelaySec: Number($('setDelay').value) || 8,
      stallMinutes: Number($('setStall').value) || 5,
    },
  });
  const msg = $('settingsMsg');
  msg.hidden = false;
  msg.textContent = res.error ? `Save failed: ${res.error}` : 'Saved.';
  setTimeout(() => { msg.hidden = true; }, 2000);
});

// ── util ───────────────────────────────────────────────────────────────────

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// Live updates while the popup is open.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.run && state.view !== 'settings' && state.view !== 'setup') {
    const run = changes.run.newValue;
    if (run) { renderRun(run); if (state.view !== 'run') show('run'); }
  }
});

refresh();
