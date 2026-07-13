// BD Indeed Auto-Apply — background orchestrator (MV3 service worker).
//
// Owns the run state machine: job queue → tab driving → answer pipeline →
// tailored-resume packages (backend prepare-package) → human-attention alerts.
// The content script (content/indeed.js) is a dumb executor: it classifies the
// page, scrapes fields, asks this worker for answers/resume bytes, fills, and
// reports results. All state lives in chrome.storage.local so the popup can
// render it and the worker can die/wake freely.

import * as api from './lib/api.js';
import { answerFields, rememberAnswers } from './lib/answers.js';
import { aiDecideAction } from './lib/ai.js';

const hostFromUrl = (u) => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return '*'; } };

// Signature for the loop detector: same host+path + same set of field labels =
// same step. Query/hash stripped (Taleo/Workday put changing session tokens
// there). Labels normalised + sorted so field order doesn't matter.
function pageStepSig(url, fields) {
  let base = '*';
  try { const u = new URL(url); base = u.hostname.replace(/^www\./, '') + u.pathname; } catch {}
  const labels = (fields || []).map(f => String(f.label || f.type || '').toLowerCase().replace(/\s+/g, ' ').trim())
    .filter(Boolean).sort().join('|');
  return `${base}::${labels}`;
}

const LOG_CAP = 300;

// In-memory caches (rebuilt on worker wake; heavy blobs also mirrored to
// storage.session so a mid-run worker restart doesn't refetch PDFs).
const pkgCache = new Map(); // jobIdx -> {status, filename, b64, error}

// ── state helpers ──────────────────────────────────────────────────────────

async function getRun() {
  const { run } = await chrome.storage.local.get('run');
  return run || null;
}

async function saveRun(run) {
  await chrome.storage.local.set({ run });
}

async function log(run, msg, level = 'info') {
  run.log = run.log || [];
  run.log.push({ ts: Date.now(), msg, level });
  if (run.log.length > LOG_CAP) run.log = run.log.slice(-LOG_CAP);
  console.log(`[bd-indeed] ${msg}`);
}

function notify(title, message) {
  try {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icons/icon128.png'),
      title,
      message: String(message).slice(0, 250),
      priority: 2,
    });
  } catch (e) { console.warn('notify failed', e); }
}

async function updateBadge(run) {
  try {
    if (!run || ['idle', 'done', 'stopped'].includes(run.status)) {
      await chrome.action.setBadgeText({ text: run?.status === 'done' ? 'DONE' : '' });
      if (run?.status === 'done') await chrome.action.setBadgeBackgroundColor({ color: '#16a34a' });
      return;
    }
    if (run.status === 'awaiting_human') {
      await chrome.action.setBadgeText({ text: '!' });
      await chrome.action.setBadgeBackgroundColor({ color: '#dc2626' });
      return;
    }
    if (run.status === 'paused') {
      await chrome.action.setBadgeText({ text: '||' });
      await chrome.action.setBadgeBackgroundColor({ color: '#f59e0b' });
      return;
    }
    const remaining = run.jobs.filter(j => j.status === 'pending' || j.status === 'active').length;
    await chrome.action.setBadgeText({ text: String(remaining) });
    await chrome.action.setBadgeBackgroundColor({ color: '#2563eb' });
  } catch { /* badge is cosmetic */ }
}

function candidateProfile(run) {
  const c = run.candidate || {};
  return {
    id: c.id, name: c.name, email: c.email, phone: c.phone,
    location: c.location, work_auth: c.work_auth, years_exp: c.years_exp,
    tech_stack: c.tech_stack, linkedin_url: c.linkedin_url,
  };
}

function markProgress(run) {
  run.lastProgressAt = Date.now();
  // Human unblocked us (page moved / new answers requested) — resume.
  if (run.status === 'awaiting_human') {
    run.status = 'running';
    run.attention = null;
  }
}

// ── run lifecycle ──────────────────────────────────────────────────────────

async function startRun({ candidate, jobs, settingsPatch, sitePassword, manualFill }) {
  if (settingsPatch) await api.saveSettings(settingsPatch);
  const settings = await api.getSettings();

  const run = {
    id: `run_${Date.now()}`,
    status: 'running',
    candidate,
    // Fill-only mode (web-app "Apply manually" handoff): the engine fills the
    // form but never submits — the user reviews and clicks submit themselves.
    // Per-run, so it can't pollute the persisted autoSubmit setting.
    manualFill: !!manualFill,
    // The candidate's job-portal login. Fetched from the backend (Supabase
    // candidates table) below; the popup field, if filled, overrides it. Lets
    // the engine pass ATS login/signup walls.
    sitePassword: sitePassword || null,
    siteLoginEmail: null,
    // 'tailored' (default) pre-tailors + tailors per job; 'base' uploads the base
    // resume for every job (no tailoring). Chosen via the popup Tailored/Base toggle.
    resumeMode: settings.resumeMode === 'base' ? 'base' : 'tailored',
    resumeParsed: null,
    baseResume: null,
    jobs: jobs.slice(0, settings.maxJobs).map(j => ({
      url: j.url, title: j.title || null, company: j.company || null,
      dbJobId: j.dbJobId || null,
      applicationId: j.applicationId || null, // set for Module-4 rescue jobs
      resumeId: j.resumeId || null,           // pre-tailored resume for rescue jobs
      status: 'pending', note: null, startedAt: null, finishedAt: null,
    })),
    currentIndex: -1,
    tabId: null,
    lastProgressAt: Date.now(),
    attention: null,
    log: [],
    startedAt: Date.now(),
  };
  pkgCache.clear();
  await log(run, `Run started: ${run.jobs.length} job(s) for ${candidate.name}`);

  // Pull the candidate's portal credentials from the backend (Supabase
  // candidates table). Manual popup password, if provided, wins.
  try {
    const creds = await api.fetchPortalCredentials(candidate.id);
    run.siteLoginEmail = creds.login_email || creds.gmail || candidate.gmail || candidate.email || null;
    if (!run.sitePassword && creds.password) run.sitePassword = creds.password;
    if (run.sitePassword) {
      await log(run, `Portal login loaded (${run.sitePassword ? 'password set' : 'no password'}, email ${run.siteLoginEmail || 'n/a'})`);
    } else {
      await log(run, 'No candidate portal password found (DB or popup) — login walls will flag the human.', 'warn');
    }
  } catch (e) {
    await log(run, `Portal credential lookup failed: ${e.message}`, 'warn');
  }

  // Base resume: parsed JSON is the AI answering context; file is the
  // fallback document if prepare-package fails for a job.
  try {
    const base = await api.getBaseResume(candidate.id);
    if (base) {
      run.baseResume = { id: base.id, file_url: base.file_url };
      run.resumeParsed = base.parsed_json || null;
      await log(run, `Base resume loaded (v${base.version})`);
    } else {
      await log(run, 'No base resume found — AI answers will use profile only; tailored resume still comes from prepare-package.', 'warn');
    }
  } catch (e) {
    await log(run, `Base resume lookup failed: ${e.message}`, 'warn');
  }

  await saveRun(run);
  await updateBadge(run);
  chrome.alarms.create('bd-watchdog', { periodInMinutes: 1 });
  // Pre-tailor the WHOLE queue in the background — the same thing the pipeline
  // does for the main agent. Each job's tailored resume is generated + stored in
  // the DB up front, so by the time the browser reaches a job's upload step
  // ensurePackage FAST PATH 2 finds it and uploads instantly (no per-job live
  // tailoring wait). Fire-and-forget so it never delays the run start. Skipped in
  // BASE mode (user chose base-only via the popup toggle — no tailoring at all).
  if (run.resumeMode !== 'base') preTailorQueue(run).catch(() => {});
  else await log(run, 'Resume mode: BASE — skipping tailoring; uploading base resume for every job.');
  await processNextJob();
  return getRun();
}

// Bulk pre-tailor every queued job (throttled), like the pipeline feeds the main
// agent. Populates the DB (tailored_for_job_id) so per-job ensurePackage hits its
// fast path. Idempotent: skips jobs already tailored or already being prepared.
async function preTailorQueue(run) {
  const candId = run.candidate && run.candidate.id;
  if (!candId) return;
  const idxs = run.jobs.map((_, i) => i)
    .filter((i) => run.jobs[i].dbJobId && !run.jobs[i].resumeId);
  if (!idxs.length) return;
  await log(run, `Pre-tailoring up to ${idxs.length} resume(s) in the background so uploads are instant…`);
  await saveRun(run);
  const CONCURRENCY = 2; // keep the backend / Gemini within a sane request rate
  let cursor = 0;
  let done = 0;
  const worker = async () => {
    while (cursor < idxs.length) {
      const idx = idxs[cursor++];
      const job = run.jobs[idx];
      if (pkgCache.has(idx)) continue; // active-job flow is already preparing this one
      try {
        const have = await api.getExistingTailoredResume(candId, job.dbJobId);
        if (have && have.id) { done++; continue; } // already tailored in the DB
        await api.preparePackage(candId, job.dbJobId, []); // generates + stores in DB
        done++;
      } catch (e) { /* per-job ensurePackage will live-tailor / fall back */ }
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, idxs.length) }, worker));
  const r = await getRun();
  if (r) { await log(r, `Pre-tailoring complete — ${done}/${idxs.length} resume(s) ready in the DB`); await saveRun(r); }
}

async function processNextJob() {
  const run = await getRun();
  if (!run || !['running', 'awaiting_human'].includes(run.status)) return;

  const idx = run.jobs.findIndex(j => j.status === 'pending');
  if (idx === -1) {
    run.status = 'done';
    run.currentIndex = -1;
    const applied = run.jobs.filter(j => j.status === 'applied').length;
    await log(run, `Run complete: ${applied}/${run.jobs.length} applied`);
    await saveRun(run);
    await updateBadge(run);
    chrome.alarms.clear('bd-watchdog');
    notify('BD Indeed Auto-Apply — run complete', `${applied}/${run.jobs.length} applications submitted for ${run.candidate.name}.`);
    return;
  }

  run.currentIndex = idx;
  run.status = 'running';
  run.attention = null;
  const job = run.jobs[idx];
  job.status = 'active';
  job.startedAt = Date.now();
  markProgress(run);
  await log(run, `Job ${idx + 1}/${run.jobs.length}: opening ${job.url}`);

  // Reuse a single automation tab across the whole run.
  let tabOk = false;
  if (run.tabId != null) {
    try {
      await chrome.tabs.update(run.tabId, { url: job.url, active: true });
      tabOk = true;
    } catch { tabOk = false; }
  }
  if (!tabOk) {
    const tab = await chrome.tabs.create({ url: job.url });
    run.tabId = tab.id;
  }

  await saveRun(run);
  await updateBadge(run);

  // DB jobs already have an id — start tailoring immediately so the PDF is
  // ready by the time the wizard reaches the resume step. CSV jobs wait for
  // JOB_META (scraped title/company/description) to register first.
  if (job.dbJobId) ensurePackage(idx).catch(() => {});
}

async function finishJob(idx, status, note, opts = {}) {
  const run = await getRun();
  if (!run) return;
  const job = run.jobs[idx];
  // Normally a job in a terminal state is left alone; an operator "Mark applied"
  // (opts.force) may override a prior failed/blocked/skipped to record a submit.
  if (!job || (!opts.force && ['applied', 'failed', 'skipped'].includes(job.status))) return;
  job.status = status;
  job.note = note || null;
  job.finishedAt = Date.now();
  run.currentIndex = -1;
  markProgress(run);
  await log(run, `Job ${idx + 1} → ${status}${note ? ` (${note})` : ''}`, status === 'applied' ? 'info' : 'warn');
  await saveRun(run);
  await updateBadge(run);

  // Write EVERY successful apply back to the backend as SUBMITTED so the DB
  // reflects reality at the end of the day:
  //   - rescue jobs carry an applicationId → PATCH that row to SUBMITTED
  //     (state machine now permits FAILED/BLOCKED → SUBMITTED);
  //   - fresh Indeed/CSV jobs (a dbJobId but no applicationId) → create the
  //     application row directly as SUBMITTED.
  const candId = run.candidate && run.candidate.id;
  if (status === 'applied' && (job.applicationId || (candId && job.dbJobId))) {
    const res = await api.recordApplicationSubmitted({
      applicationId: job.applicationId || null,
      candidateId: candId,
      jobId: job.dbJobId || null,
      resumeId: job.resumeId || null,
    });
    const ref = job.applicationId ? `app ${String(job.applicationId).slice(0, 8)}`
      : `job ${String(job.dbJobId).slice(0, 8)}`;
    const run2 = await getRun();
    if (run2) {
      await log(run2, res.ok
        ? `Job ${idx + 1}: backend recorded → SUBMITTED (${ref}; ${res.detail})`
        : `Job ${idx + 1}: ⚠ backend NOT updated (${ref}) — ${res.detail}`,
        res.ok ? 'info' : 'warn');
      await saveRun(run2);
    }
  } else if (status === 'applied') {
    await log(run, `Job ${idx + 1}: applied, but no DB job id or application id — cannot record in backend (paste/CSV job that never registered). candidate=${candId ? 'set' : 'MISSING'}, dbJobId=${job.dbJobId || 'MISSING'}`, 'warn');
  }

  if (['running', 'awaiting_human'].includes(run.status)) {
    const settings = await api.getSettings();
    const delay = Math.max(2, Number(settings.jobDelaySec) || 8);
    chrome.alarms.create('bd-next-job', { when: Date.now() + delay * 1000 });
  }
}

// ── tailored resume packages ───────────────────────────────────────────────

async function ensurePackage(idx) {
  const existing = pkgCache.get(idx);
  if (existing && existing.status !== 'failed') return existing;
  pkgCache.set(idx, { status: 'pending' });

  const run = await getRun();
  const job = run?.jobs?.[idx];
  if (!run || !job) {
    pkgCache.set(idx, { status: 'failed', error: 'job not found' });
    return pkgCache.get(idx);
  }

  const filename = `${(run.candidate.name || 'candidate').trim().replace(/\s+/g, '_')}_Resume.pdf`;
  try {
    // BASE MODE — user chose base-only (popup toggle): skip ALL tailoring paths
    // and upload the candidate's base resume for this job.
    if (run.resumeMode === 'base') {
      await loadBaseResume(idx, filename, run);
      return pkgCache.get(idx);
    }
    // FAST PATH 1 — rescue jobs carry the resume Module 4 already tailored.
    if (job.resumeId) {
      await log(run, `Job ${idx + 1}: using resume already tailored by the agent`);
      await saveRun(run);
      const url = await api.resumeViewUrl(job.resumeId);
      const b64 = await api.fetchAsBase64(url);
      pkgCache.set(idx, { status: 'ready', filename, b64, tailored: true });
      return pkgCache.get(idx);
    }
    // FAST PATH 2 — a resume already tailored for this candidate+job exists in
    // the DB (generated earlier by the pipeline). Fetch it directly — seconds,
    // not the 30-60s of live tailoring. This is what the main agent does.
    if (job.dbJobId) {
      try {
        const existing = await api.getExistingTailoredResume(run.candidate.id, job.dbJobId);
        if (existing?.id) {
          await log(run, `Job ${idx + 1}: found pre-tailored resume in DB — using it (fast)`);
          await saveRun(run);
          const url = await api.resumeViewUrl(existing.id);
          const b64 = await api.fetchAsBase64(url);
          pkgCache.set(idx, { status: 'ready', filename, b64, tailored: true });
          return pkgCache.get(idx);
        }
      } catch (e) {
        // fall through to live tailoring
      }
    }
    if (!job.dbJobId) throw new Error('job not registered in backend');
    // LIVE TAILORING — resolved IN THE BACKGROUND (don't block-and-bail).
    // The main agent is instant because the PIPELINE pre-tailors the resume
    // before the agent runs; the extension applies on-demand, so the resume
    // usually isn't pre-tailored and must be generated live (~30-60s). ensurePackage
    // is prefetched at run start (see startRun), so tailoring is typically already
    // done by the time the browser reaches the upload step. We keep the package
    // status 'pending' and let GET_RESUME make the upload step WAIT for the
    // TAILORED resume (the poller tolerates ~175s) — instead of the old behaviour
    // of giving up at 20s and uploading BASE every time. Base is used only on a
    // genuine failure or a generous overall timeout.
    await log(run, `Job ${idx + 1}: tailoring resume (may take up to a minute)...`);
    await saveRun(run);
    let settled = false;
    const useBase = async (why) => {
      if (settled) return; settled = true;
      const r = await getRun(); if (r) { await log(r, `Job ${idx + 1}: ${why}`, 'warn'); await saveRun(r); }
      await loadBaseResume(idx, filename, run);
    };
    const useTailored = async (winner) => {
      if (settled) return;
      if (!winner || !winner.resume_pdf_url) return useBase('tailoring returned no resume — using base');
      try {
        const b64 = await api.fetchAsBase64(winner.resume_pdf_url);
        if (settled) return; settled = true;
        pkgCache.set(idx, { status: 'ready', filename, b64, tailored: true });
        const r = await getRun(); if (r) { await log(r, `Job ${idx + 1}: tailored resume ready (${Math.round(b64.length * 0.75 / 1024)} KB)`); await saveRun(r); }
      } catch (e) { await useBase(`tailored resume fetch failed (${e.message}) — using base`); }
    };
    api.preparePackage(run.candidate.id, job.dbJobId, [])
      .then(useTailored)
      .catch((e) => useBase(`tailoring failed (${e.message}) — using base`));
    // Generous safety net so the upload never hangs forever.
    setTimeout(() => { if (!settled && pkgCache.get(idx)?.status === 'pending') useBase(`tailoring too slow (${Math.round(TAILOR_WAIT_MS / 1000)}s) — using base`); }, TAILOR_WAIT_MS);
    // Return the current ('pending') status; the poller waits for the background
    // resolution above to flip it to a tailored (or base) resume.
    return pkgCache.get(idx);
  } catch (e) {
    // Last resort: base resume so the application can still proceed.
    try {
      const run2 = await getRun();
      if (run2) { await log(run2, `Job ${idx + 1}: resume prep failed (${e.message}); falling back to base resume`, 'warn'); await saveRun(run2); }
      await loadBaseResume(idx, filename, run);
    } catch (e2) {
      pkgCache.set(idx, { status: 'failed', error: `${e.message}; fallback: ${e2.message}` });
    }
  }
  return pkgCache.get(idx);
}

// Generous SAFETY timeout for live tailoring before falling back to base. Not a
// bail-early cutoff — tailoring runs in the background and the upload step waits
// for it (the upload poller tolerates ~175s), so this only guards against a
// genuinely hung tailor. Must stay comfortably under the upload poller's budget.
const TAILOR_WAIT_MS = 100000; // 100s

// Load the candidate's base resume into the package cache (fast path / fallback).
async function loadBaseResume(idx, filename, run) {
  let base = run.baseResume;
  // If the run snapshot has no base resume (e.g. it was fetched before login
  // resolved, or Base mode skipped the earlier load), fetch it directly now.
  if (!base || (!base.id && !base.file_url)) {
    try {
      const b = await api.getBaseResume(run.candidate && run.candidate.id);
      if (b && (b.id || b.file_url)) { base = { id: b.id, file_url: b.file_url }; }
    } catch (e) { /* fall through to the clear error below */ }
  }
  if (!base || (!base.id && !base.file_url)) {
    throw new Error('no base resume in the DB for this candidate — upload one on the candidate record');
  }
  let url;
  try { url = base.id ? await api.resumeViewUrl(base.id) : base.file_url; }
  catch { url = base.file_url; }
  if (!url) throw new Error('base resume has no downloadable URL');
  const b64 = await api.fetchAsBase64(url);
  pkgCache.set(idx, { status: 'ready', filename, b64, tailored: false });
  return pkgCache.get(idx);
}

// ── message handling ───────────────────────────────────────────────────────

async function handleMessage(msg, sender) {
  switch (msg.type) {
    // ---- popup ----
    case 'LOGIN': {
      await api.login(msg.email, msg.password);
      return { ok: true };
    }
    case 'LOGOUT': {
      await api.logout();
      return { ok: true };
    }
    case 'GET_STATE': {
      const [run, authed, settings] = await Promise.all([getRun(), api.isAuthed(), api.getSettings()]);
      const { auth } = await chrome.storage.local.get('auth');
      return { run, authed, settings, email: auth?.email || null };
    }
    case 'SAVE_SETTINGS': {
      const settings = await api.saveSettings(msg.patch || {});
      return { ok: true, settings };
    }
    case 'FETCH_CANDIDATES': {
      return { candidates: await api.listCandidates() };
    }
    case 'FETCH_DB_JOBS': {
      const jobs = await api.listIndeedJobs(msg.candidateId, msg.limit || 100);
      return {
        jobs: jobs.map(j => ({ url: j.source_url, title: j.title, company: j.company, dbJobId: j.id })),
      };
    }
    case 'FETCH_RESCUE_JOBS': {
      return { jobs: await api.listRescueJobs(msg.candidateId, msg.limit || 50) };
    }
    case 'MARK_APPLIED': {
      const run = await getRun();
      if (!run) return { ok: false, error: 'no active run' };
      // Target an explicit job (per-row "Mark applied") or the current one.
      const idx = (typeof msg.jobIndex === 'number' && msg.jobIndex >= 0) ? msg.jobIndex : run.currentIndex;
      if (idx == null || idx < 0 || !run.jobs[idx]) return { ok: false, error: 'no job to mark' };
      const job = run.jobs[idx];
      const candId = run.candidate && run.candidate.id;
      // Write the SUBMITTED status back to the backend (rescue jobs PATCH their
      // applicationId; fresh Indeed/CSV jobs create the row). Works even if the
      // job previously FAILED in the browser — the operator is confirming it.
      let res = { ok: false, detail: 'no application id / DB job id to record' };
      if (job.applicationId || (candId && job.dbJobId)) {
        res = await api.recordApplicationSubmitted({
          applicationId: job.applicationId || null,
          candidateId: candId,
          jobId: job.dbJobId || null,
          resumeId: job.resumeId || null,
        });
      }
      // Update the local job (override any prior failed/blocked) WITHOUT
      // disturbing another job that may be active.
      const run2 = await getRun();
      if (run2 && run2.jobs[idx]) {
        run2.jobs[idx].status = 'applied';
        run2.jobs[idx].note = 'confirmed by operator';
        run2.jobs[idx].finishedAt = Date.now();
        const wasActive = run2.currentIndex === idx;
        if (wasActive) run2.currentIndex = -1;
        markProgress(run2);
        await log(run2, res.ok
          ? `Job ${idx + 1}: marked applied → SUBMITTED in backend (${res.detail})`
          : `Job ${idx + 1}: marked applied locally — backend NOT updated (${res.detail})`,
          res.ok ? 'info' : 'warn');
        await saveRun(run2); await updateBadge(run2);
        if (wasActive && ['running', 'awaiting_human'].includes(run2.status)) {
          setTimeout(() => processNextJob().catch(() => {}), 1500);
        }
      }
      return { ok: res.ok, detail: res.detail };
    }
    case 'START_RUN': {
      const run = await startRun(msg);
      return { ok: true, run };
    }
    case 'PAUSE_RUN': {
      const run = await getRun();
      if (run && ['running', 'awaiting_human'].includes(run.status)) {
        run.status = 'paused';
        await log(run, 'Paused by operator');
        await saveRun(run); await updateBadge(run);
      }
      return { ok: true };
    }
    case 'RESUME_RUN': {
      const run = await getRun();
      if (run && run.status === 'paused') {
        run.status = 'running';
        markProgress(run);
        await log(run, 'Resumed by operator');
        await saveRun(run); await updateBadge(run);
        if (run.currentIndex === -1) await processNextJob();
      }
      return { ok: true };
    }
    case 'STOP_RUN': {
      const run = await getRun();
      if (run) {
        run.status = 'stopped';
        run.currentIndex = -1;
        await log(run, 'Stopped by operator');
        await saveRun(run); await updateBadge(run);
      }
      chrome.alarms.clear('bd-next-job');
      chrome.alarms.clear('bd-watchdog');
      return { ok: true };
    }
    case 'CLEAR_RUN': {
      await chrome.storage.local.remove('run');
      pkgCache.clear();
      await updateBadge(null);
      return { ok: true };
    }
    case 'SKIP_CURRENT_JOB': {
      const run = await getRun();
      if (run && run.currentIndex >= 0) await finishJob(run.currentIndex, 'skipped', 'skipped by operator');
      return { ok: true };
    }

    // ---- content script ----
    case 'TICK':
    case 'CONTENT_READY': {
      const run = await getRun();
      const active = !!(run
        && sender.tab && sender.tab.id === run.tabId
        && ['running', 'awaiting_human'].includes(run.status)
        && run.currentIndex >= 0);
      if (!active) return { act: false };
      const settings = await api.getSettings();
      const job = run.jobs[run.currentIndex];
      if (msg.type === 'CONTENT_READY') {
        markProgress(run); await saveRun(run);
      }
      return {
        act: true,
        status: run.status,
        jobIndex: run.currentIndex,
        job: { url: job.url, title: job.title, company: job.company, hasDbId: !!job.dbJobId },
        candidateName: run.candidate.name,
        autoSubmit: run.manualFill ? false : !!settings.autoSubmit,
        hasSitePassword: !!run.sitePassword, // boolean only — value never leaves the worker except into form fills
        // Persist "this candidate already has an account here" across page
        // reloads (the content script resets each navigation). Per-job.
        forceLogin: run.forceLoginJob === run.currentIndex,
        // Persist "resume already uploaded for this job" across reloads so the
        // engine never re-uploads / re-clicks "Replace resume" (iCIMS re-parse
        // loop). Per-job.
        resumeDone: run.resumeDoneJob === run.currentIndex,
      };
    }
    case 'MARK_RESUME_DONE': {
      // Content uploaded the resume for this job — remember it so a page reload
      // (which resets the in-page flag) doesn't trigger a re-upload/replace loop.
      const run = await getRun();
      if (!run || run.currentIndex !== msg.jobIndex) return { ok: false };
      run.resumeDoneJob = run.currentIndex;
      await saveRun(run);
      return { ok: true };
    }
    case 'MARK_FORCE_LOGIN': {
      // Content discovered the candidate already has an account on this site
      // (registration rejected). Remember it for THIS job so that after the
      // page navigates we prefer signing in over guest-apply / re-registering.
      const run = await getRun();
      if (!run || run.currentIndex !== msg.jobIndex) return { ok: false };
      run.forceLoginJob = run.currentIndex;
      await saveRun(run);
      return { ok: true };
    }
    case 'JOB_META': {
      const run = await getRun();
      if (!run || run.currentIndex !== msg.jobIndex) return { ok: false };
      const job = run.jobs[run.currentIndex];
      if (msg.title && !job.title) job.title = msg.title;
      if (msg.company && !job.company) job.company = msg.company;
      markProgress(run);
      await saveRun(run);
      if (!job.dbJobId) {
        // CSV job — register in the backend so module3 can tailor against the
        // real description, then kick off the package.
        try {
          const created = await api.createJob({
            title: job.title || 'Unknown Title',
            company: job.company || 'Unknown Company',
            source: isIndeedUrl(job.url) ? 'indeed' : 'extension',
            source_url: job.url,
            description: (msg.description || '').slice(0, 20000) || null,
            location: msg.location || null,
          });
          const run2 = await getRun();
          if (run2 && run2.jobs[msg.jobIndex] && created && created.id) {
            run2.jobs[msg.jobIndex].dbJobId = created.id;
            await log(run2, `Job ${msg.jobIndex + 1}: registered in backend (${created.id})`);
            await saveRun(run2);
            ensurePackage(msg.jobIndex).catch(() => {});
          } else if (run2) {
            // No job id back (e.g. an Indeed job that already exists / dedup) —
            // not an error; we'll just apply with the base resume.
            await log(run2, `Job ${msg.jobIndex + 1}: not registered (already exists or no id returned) — using base resume`);
            await saveRun(run2);
          }
        } catch (e) {
          const run2 = await getRun();
          if (run2) { await log(run2, `Job ${msg.jobIndex + 1}: backend registration failed: ${e.message}`, 'warn'); await saveRun(run2); }
        }
      }
      return { ok: true };
    }
    case 'PAGE_STATE': {
      const run = await getRun();
      if (!run || run.currentIndex !== msg.jobIndex) return { ok: false };
      markProgress(run);
      // Arm external-tab adoption: the content script is about to click an
      // "Apply on company site" control that may open a NEW tab whose
      // openerTabId doesn't reliably point back to us.
      if (/following external apply|external listing — clicking apply|choosing manual\/direct apply/i.test(msg.state || '')) {
        run.expectExternalTab = Date.now();
      }
      await log(run, `Job ${msg.jobIndex + 1}: ${msg.state}${msg.detail ? ` — ${msg.detail}` : ''}`);
      await saveRun(run);
      return { ok: true };
    }
    case 'GET_ANSWERS': {
      const run = await getRun();
      if (!run || run.currentIndex !== msg.jobIndex) return { answers: [] };
      markProgress(run);
      await saveRun(run);
      const settings = await api.getSettings();
      const job = run.jobs[run.currentIndex];
      const answerCtx = {
        profile: {
          ...candidateProfile(run),
          sitePassword: run.sitePassword || null,
          siteLoginEmail: run.siteLoginEmail || null,
        },
        resume: run.resumeParsed,
        jobTitle: job.title,
        jobCompany: job.company,
        host: hostFromUrl(msg.pageUrl || job.url),
      };
      const answers = await answerFields(msg.fields || [], answerCtx, settings);
      // LOOP DETECTOR (survives content-script reloads — its own in-page guards
      // reset on every navigation). Some ATS pages (e.g. Taleo privacy
      // agreement) let us "answer" a field, then reload right back to the same
      // page, so the deterministic filler spins forever and never yields to the
      // AI navigator. Track how many times we've been asked for the SAME field
      // set on the SAME page (host+path, query stripped so session tokens don't
      // make every hit unique). After a few repeats, tell the content script to
      // hand the page to the smart AI navigator (scroll T&C → click Accept);
      // after many, to the human.
      const stepSig = pageStepSig(msg.pageUrl || job.url, msg.fields || []);
      const run2 = await getRun();
      let escalate = null;
      if (run2) {
        run2.stepSeen = run2.stepSeen || {};
        run2.stepSeen[stepSig] = (run2.stepSeen[stepSig] || 0) + 1;
        const seen = run2.stepSeen[stepSig];
        if (seen >= 10) escalate = 'human';
        else if (seen >= 3) escalate = 'ai';
        // Once we've decided a page is a human handoff, stop feeding answers so
        // the deterministic filler can't keep re-submitting and re-looping.
        const outAnswers = escalate === 'human' ? [] : answers;
        // Log the fill result only while we're still trying (not once we've given
        // up to the human) — avoids the 200×+ "same page seen" spam.
        if (escalate !== 'human') {
          const bySource = answers.reduce((acc, a) => { acc[a.source] = (acc[a.source] || 0) + 1; return acc; }, {});
          await log(run2, `Job ${msg.jobIndex + 1}: answered ${answers.filter(a => a.answer != null).length}/${answers.length} fields (${Object.entries(bySource).map(([k, v]) => `${k}:${v}`).join(', ')})`);
        }
        // Emit each escalation transition only ONCE per page signature.
        run2.escLogged = run2.escLogged || {};
        if (escalate && run2.escLogged[stepSig] !== escalate) {
          run2.escLogged[stepSig] = escalate;
          await log(run2, `Job ${msg.jobIndex + 1}: same page seen ${seen}× — handing to ${escalate === 'ai' ? 'AI navigator (read/scroll/accept)' : 'the operator'}`, 'warn');
        }
        if (answerCtx.__aiError) {
          await log(run2, `Job ${msg.jobIndex + 1}: ⚠ AI answering failed — ${answerCtx.__aiError}`, 'warn');
        }
        await saveRun(run2);
        return { answers: outAnswers, escalate };
      }
      return { answers, escalate };
    }
    case 'GET_RESUME': {
      const run = await getRun();
      if (!run || run.currentIndex !== msg.jobIndex) return { status: 'failed', error: 'job no longer active' };
      const job = run.jobs[run.currentIndex];
      // Base mode needs no DB job (the base resume is candidate-level) — go
      // straight to it. Only the tailoring path waits for the job to register.
      if (run.resumeMode !== 'base' && !job.dbJobId && Date.now() - (job.startedAt || 0) < 90_000) {
        // Meta not registered yet (still waiting on the JOB_META round trip).
        // After 90 s we give up on tailoring and ensurePackage falls back to
        // the candidate's base resume.
        return { status: 'pending' };
      }
      const pkg = pkgCache.get(msg.jobIndex) || await ensurePackage(msg.jobIndex);
      if (pkg.status === 'pending') return { status: 'pending' };
      if (pkg.status === 'failed') return { status: 'failed', error: pkg.error };
      // Only a delivered resume counts as progress — pending polls must not
      // clear an awaiting_human flag. Re-read: ensurePackage may have taken
      // minutes and written to the run in the meantime.
      const run2 = await getRun();
      if (run2) { markProgress(run2); await saveRun(run2); }
      return { status: 'ready', filename: pkg.filename, b64: pkg.b64, tailored: pkg.tailored };
    }
    case 'JOB_RESULT': {
      const run = await getRun();
      if (!run || run.currentIndex !== msg.jobIndex) return { ok: false };
      // finishJob() writes SUBMITTED back to the backend for rescue jobs.
      await finishJob(msg.jobIndex, msg.status, msg.note);
      return { ok: true };
    }
    case 'LEARN_ANSWERS': {
      // The human filled fields the pipeline couldn't — cache them so future
      // runs (this site or others) answer them automatically.
      const run = await getRun();
      if (!run || run.currentIndex !== msg.jobIndex) return { ok: false };
      const host = hostFromUrl(msg.pageUrl || run.jobs[run.currentIndex]?.url);
      const entries = (msg.entries || []).filter(e => e && e.label && e.answer);
      if (entries.length) {
        await rememberAnswers(run.candidate.id, entries.map(e => ({ ...e, source: 'human' })), host);
        const run2 = await getRun();
        if (run2) { await log(run2, `Job ${msg.jobIndex + 1}: learned ${entries.length} answer(s) from operator → saved to field memory`); await saveRun(run2); }
      }
      return { ok: true, learned: entries.length };
    }
    case 'GET_AI_ACTION': {
      const run = await getRun();
      if (!run || run.currentIndex !== msg.jobIndex) return { action: 'wait' };
      const settings = await api.getSettings();
      if (!settings.geminiKey && !settings.anthropicKey) {
        return { action: 'human', reason: 'no AI key configured' };
      }
      markProgress(run); await saveRun(run);
      try {
        const decision = await aiDecideAction(msg.page, {
          candidateName: run.candidate.name,
          candidate: candidateProfile(run),
          resume: run.resumeParsed,
          resumeUploaded: msg.resumeUploaded,
          forceLogin: run.forceLoginJob === run.currentIndex,
        }, settings);
        const run2 = await getRun();
        if (run2) { await log(run2, `Job ${msg.jobIndex + 1}: AI nav → ${decision.action}${decision.reason ? ` (${decision.reason})` : ''}`); await saveRun(run2); }
        return decision;
      } catch (e) {
        return { action: 'human', reason: `AI nav failed: ${e.message}` };
      }
    }
    case 'GET_CODE': {
      // Email-verification code via the backend's Gmail code-catcher.
      const run = await getRun();
      if (!run || run.currentIndex !== msg.jobIndex) return { code: null };
      const job = run.jobs[run.currentIndex];
      const after = Math.floor((job.startedAt || Date.now()) / 1000);
      const hint = (msg.senderHint || job.company || '').split(/\s+/)[0] || '';
      try {
        const code = await api.fetchVerificationCode(run.candidate.id, after, hint);
        if (code) {
          const run2 = await getRun();
          if (run2) { markProgress(run2); await log(run2, `Job ${msg.jobIndex + 1}: verification code fetched from candidate Gmail`); await saveRun(run2); }
        }
        return { code };
      } catch (e) {
        return { code: null, error: e.message };
      }
    }
    case 'NEEDS_HUMAN': {
      const run = await getRun();
      if (!run || run.currentIndex !== msg.jobIndex) return { ok: false };
      if (run.status !== 'awaiting_human') {
        run.status = 'awaiting_human';
        run.attention = { reason: msg.reason, at: Date.now() };
        await log(run, `Job ${msg.jobIndex + 1}: NEEDS HUMAN — ${msg.reason}`, 'warn');
        await saveRun(run);
        await updateBadge(run);
        notify('BD Indeed Auto-Apply — attention needed', `${msg.reason}\n(${run.jobs[msg.jobIndex].title || run.jobs[msg.jobIndex].url})`);
      }
      return { ok: true };
    }
    case 'LOG': {
      const run = await getRun();
      if (run) { await log(run, msg.msg, msg.level || 'info'); await saveRun(run); }
      return { ok: true };
    }
    default:
      return { error: `unknown message type ${msg.type}` };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleMessage(msg, sender)
    .then(sendResponse)
    .catch(e => {
      console.error('[bd-indeed] handler error', msg.type, e);
      sendResponse({ error: e.message, authError: e instanceof api.AuthError });
    });
  return true; // async
});

// ── web-app handoff (externally_connectable) ───────────────────────────────
// The BD Automator web app talks to the extension via
// chrome.runtime.sendMessage(EXTENSION_ID, msg) from the origins listed in
// manifest.json's externally_connectable. Two messages:
//   PING          → install/auth probe: {installed, version, authed}
//   MANUAL_APPLY  → open ONE job and fill it without submitting (the user
//                   reviews and submits). Payload: {candidateId, jobUrl,
//                   applicationId?, jobId?, resumeId?, title?, company?}

async function startManualApply(msg) {
  if (!msg.candidateId || !msg.jobUrl) {
    return { ok: false, error: 'bad_request', detail: 'candidateId and jobUrl are required' };
  }
  if (!(await api.isAuthed())) {
    return { ok: false, error: 'not_authed', detail: 'Log in from the extension popup first.' };
  }
  const run = await getRun();
  if (run && ['running', 'awaiting_human'].includes(run.status)) {
    return { ok: false, error: 'run_active', detail: 'A run is already in progress — stop it from the popup first.' };
  }
  const candidates = await api.listCandidates();
  const candidate = (candidates || []).find(c => String(c.id) === String(msg.candidateId));
  if (!candidate) {
    return { ok: false, error: 'candidate_not_found', detail: `Candidate ${msg.candidateId} is not visible to this extension login.` };
  }
  const job = {
    url: msg.jobUrl,
    title: msg.title || null,
    company: msg.company || null,
    dbJobId: msg.jobId || null,
    applicationId: msg.applicationId || null, // rescue write-back target
    resumeId: msg.resumeId || null,           // reuse the already-tailored resume
  };
  notify('BD manual apply', `Filling "${job.title || job.url}" for ${candidate.name} — review and submit when ready.`);
  const started = await startRun({ candidate, jobs: [job], manualFill: true });
  return { ok: true, run: started && { id: started.id, status: started.status } };
}

chrome.runtime.onMessageExternal.addListener((msg, sender, sendResponse) => {
  (async () => {
    switch (msg && msg.type) {
      case 'PING':
        return { installed: true, version: chrome.runtime.getManifest().version, authed: await api.isAuthed() };
      case 'MANUAL_APPLY':
        return startManualApply(msg);
      default:
        return { error: `unknown external message type ${msg && msg.type}` };
    }
  })()
    .then(sendResponse)
    .catch(e => {
      console.error('[bd-indeed] external handler error', msg && msg.type, e);
      sendResponse({ ok: false, error: e.message, authError: e instanceof api.AuthError });
    });
  return true; // async
});

// ── alarms ─────────────────────────────────────────────────────────────────

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'bd-next-job') {
    await processNextJob();
  } else if (alarm.name === 'bd-watchdog') {
    const run = await getRun();
    if (!run || run.status !== 'running' || run.currentIndex < 0) return;
    const settings = await api.getSettings();
    const stallMs = Math.max(2, Number(settings.stallMinutes) || 5) * 60 * 1000;
    if (Date.now() - (run.lastProgressAt || 0) > stallMs) {
      run.status = 'awaiting_human';
      run.attention = { reason: `No progress for ${settings.stallMinutes} min — check the tab`, at: Date.now() };
      await log(run, 'Watchdog: run stalled, flagging for human', 'warn');
      await saveRun(run);
      await updateBadge(run);
      notify('BD Indeed Auto-Apply — stalled', 'The current application has not progressed. Please check the tab.');
    }
  }
});

// ── tab lifecycle ──────────────────────────────────────────────────────────

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const run = await getRun();
  if (!run || run.tabId !== tabId) return;
  run.tabId = null;
  if (['running', 'awaiting_human'].includes(run.status) && run.currentIndex >= 0) {
    // Operator closed the automation tab mid-job — treat as skip and move on.
    await log(run, 'Automation tab closed — skipping current job', 'warn');
    await saveRun(run);
    await finishJob(run.currentIndex, 'skipped', 'tab closed');
  } else {
    await saveRun(run);
  }
});

// ── company-site support ───────────────────────────────────────────────────
// The manifest auto-injects the engine only on *.indeed.com. For external
// ATS/company sites we inject programmatically — and ONLY into the run's
// automation tab, never into the operator's normal browsing.

const isIndeedUrl = (u) => { try { return /(^|\.)indeed\.com$/i.test(new URL(u).hostname); } catch { return false; } };

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status !== 'complete') return;
  const run = await getRun();
  if (!run || run.tabId !== tabId || run.currentIndex < 0) return;
  if (!['running', 'awaiting_human'].includes(run.status)) return;
  const url = tab.url || '';
  if (!/^https?:/i.test(url) || isIndeedUrl(url)) return; // indeed injects via manifest
  try {
    // Company/ATS sites are driven by the GENERIC portal engine (generic.js),
    // never the Indeed engine — that isolation is the whole point of the split.
    // allFrames:true so embedded ATS forms (Greenhouse/Lever/Workday iframes on
    // a company careers page) get the engine too — otherwise the top document
    // has no form fields and nothing is filled.
    await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      files: ['content/generic.js'],
    });
  } catch (e) {
    console.warn('[bd-generic] inject failed', url.slice(0, 80), e.message);
    // Retry top-frame only (allFrames can fail if a frame is restricted).
    try { await chrome.scripting.executeScript({ target: { tabId }, files: ['content/generic.js'] }); }
    catch (e2) { console.warn('[bd-generic] top-frame inject failed', e2.message); }
  }
});

// "Apply on company site" often opens a NEW tab — adopt it as the automation
// tab so the engine follows the application there. openerTabId is unreliable
// (Indeed's redirect chain can drop it), so we ALSO adopt when we recently
// armed an external-apply click (expectExternalTab).
chrome.tabs.onCreated.addListener(async (tab) => {
  const run = await getRun();
  if (!run || run.currentIndex < 0) return;
  if (!['running', 'awaiting_human'].includes(run.status)) return;
  const opener = tab.openerTabId === run.tabId;
  const expecting = run.expectExternalTab && (Date.now() - run.expectExternalTab < 25000);
  if (!opener && !expecting) return;
  const oldTabId = run.tabId;
  run.tabId = tab.id;
  run.expectExternalTab = null;
  markProgress(run);
  await log(run, `Job ${run.currentIndex + 1}: external apply opened a new tab — following it${expecting && !opener ? ' (adopted)' : ''}`);
  await saveRun(run);
  // Close the now-stale origin tab so it can't keep re-clicking Apply and
  // spawning duplicate tabs.
  if (oldTabId && oldTabId !== tab.id && !opener) {
    try { await chrome.tabs.remove(oldTabId); } catch { /* already gone */ }
  }
});

// Restore badge on worker wake.
getRun().then(updateBadge).catch(() => {});
