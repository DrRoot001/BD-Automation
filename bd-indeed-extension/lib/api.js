// Backend (Module 1) API client. All calls go through the BD Automator FastAPI
// backend — the extension never talks to Supabase directly except to download
// signed PDF URLs the backend hands back.

import { DEFAULT_CONFIG } from '../config.js';

export const DEFAULT_SETTINGS = {
  apiBase: DEFAULT_CONFIG.apiBase || 'http://localhost:8000/api',
  geminiKey: DEFAULT_CONFIG.geminiKey || '',
  geminiModel: DEFAULT_CONFIG.geminiModel || 'gemini-3.5-flash',
  anthropicKey: DEFAULT_CONFIG.anthropicKey || '',
  autoSubmit: true,
  // 'tailored' = pre-tailor every queued job's resume; 'base' = skip tailoring
  // and upload the candidate's base resume for all jobs (faster, no LLM cost).
  resumeMode: 'tailored',
  maxJobs: 10,
  desiredSalary: '85000',
  desiredHours: '40',
  // Last-resort value for "Current location" when the candidate has no
  // city/state in their resume or profile. Set to a real "City, ST" to make
  // autocompletes match; "United States" is the safe non-blocking default.
  defaultLocation: 'United States',
  // Strong default password for portal account creation / sign-in (meets
  // upper/lower/digit/special/8+ rules that the gmail password usually fails).
  portalPassword: DEFAULT_CONFIG.portalPassword || '',
  jobDelaySec: 8,
  stallMinutes: 5,
  skipGate: true,
  needsCoverLetter: false,
};

export async function getSettings() {
  const { settings } = await chrome.storage.local.get('settings');
  const merged = { ...DEFAULT_SETTINGS, ...(settings || {}) };
  // An empty saved value must not mask a default baked into config.js.
  for (const k of ['apiBase', 'geminiKey', 'anthropicKey', 'geminiModel']) {
    if (!merged[k]) merged[k] = DEFAULT_SETTINGS[k];
  }
  // API KEYS ARE CODE-AUTHORITATIVE: when a key is hardcoded in config.js it
  // WINS over any value saved in the popup, so a stale popup key can never
  // shadow the key in the code. (Leave config.js key empty to use the popup.)
  if (DEFAULT_CONFIG.geminiKey) merged.geminiKey = DEFAULT_CONFIG.geminiKey;
  if (DEFAULT_CONFIG.anthropicKey) merged.anthropicKey = DEFAULT_CONFIG.anthropicKey;
  // Model is also code-authoritative when set in config.js (so the popup can't
  // pin a stale model like gemini-2.5-flash).
  if (DEFAULT_CONFIG.geminiModel) merged.geminiModel = DEFAULT_CONFIG.geminiModel;
  return merged;
}

export async function saveSettings(patch) {
  const current = await getSettings();
  const settings = { ...current, ...patch };
  await chrome.storage.local.set({ settings });
  return settings;
}

export class AuthError extends Error {}

async function getToken() {
  const { auth } = await chrome.storage.local.get('auth');
  return auth?.token || null;
}

// The backend returns Supabase access tokens which expire after ~1 hour.
// Rather than bouncing the BD agent back to the login form mid-run, we keep
// the credentials (internal tool, agent's own machine) and silently
// re-authenticate once on any 401/403, then retry the request.
let reloginInFlight = null;

async function tryRelogin() {
  if (!reloginInFlight) {
    reloginInFlight = (async () => {
      const { auth } = await chrome.storage.local.get('auth');
      if (!auth?.email || !auth?.password) return false;
      try {
        await login(auth.email, auth.password);
        return true;
      } catch {
        return false;
      } finally {
        setTimeout(() => { reloginInFlight = null; }, 0);
      }
    })();
  }
  return reloginInFlight;
}

async function apiFetch(path, opts = {}) {
  const settings = await getSettings();
  const base = settings.apiBase.replace(/\/$/, '');
  const token = await getToken();
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  let res;
  try {
    res = await fetch(`${base}${path}`, { ...opts, headers });
  } catch (e) {
    throw new Error(`Backend unreachable at ${base} (${e.message})`);
  }
  if ((res.status === 401 || res.status === 403) && path !== '/auth/login') {
    if (!opts._retried && await tryRelogin()) {
      return apiFetch(path, { ...opts, _retried: true });
    }
    throw new AuthError(`Not authorized (${res.status}) — log in again from the popup.`);
  }
  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body);
    } catch { detail = await res.text().catch(() => ''); }
    throw new Error(`API ${res.status} on ${path}: ${detail.slice(0, 300)}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

export async function login(email, password) {
  const data = await apiFetch('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  });
  // Password kept locally so the ~1 h Supabase token can be refreshed silently.
  await chrome.storage.local.set({ auth: { token: data.access_token, email, password, at: Date.now() } });
  return data;
}

export async function logout() {
  await chrome.storage.local.remove('auth');
}

export async function isAuthed() {
  return !!(await getToken());
}

export async function listCandidates() {
  return apiFetch('/candidates?limit=200');
}

/**
 * FAST PATH: find a resume already tailored for this candidate+job in the DB
 * (tagged tailored_for_job_id), exactly like the Module 4 agent uses. Returns
 * the resume row or null. Avoids the slow live prepare-package tailoring.
 */
export async function getExistingTailoredResume(candidateId, jobId) {
  if (!candidateId || !jobId) return null;
  const resumes = await apiFetch(`/resumes/${candidateId}`).catch(() => null);
  if (!Array.isArray(resumes)) return null;
  const match = resumes.find(r =>
    r && !r.is_base && r.tailored_for_job_id && String(r.tailored_for_job_id) === String(jobId));
  return match || null;
}

export async function getBaseResume(candidateId) {
  const resumes = await apiFetch(`/resumes/${candidateId}?is_base=true`).catch(() => []);
  if (Array.isArray(resumes) && resumes.length) {
    // Highest version first
    resumes.sort((a, b) => (b.version || 0) - (a.version || 0));
    return resumes[0];
  }
  return null;
}

export async function listIndeedJobs(candidateId, limit = 100) {
  const params = new URLSearchParams({ source: 'indeed', limit: String(limit) });
  if (candidateId) params.set('candidate_id', candidateId);
  const jobs = await apiFetch(`/jobs?${params}`);
  // Belt and braces: only keep rows whose URL is actually on indeed.com
  return (jobs || []).filter(j => /(^|\.)indeed\.com/i.test(hostOf(j.source_url) || ''));
}

export async function createJob(job) {
  const created = await apiFetch('/jobs', { method: 'POST', body: JSON.stringify([job]) });
  return Array.isArray(created) ? created[0] : created;
}

export async function getJob(jobId) {
  return apiFetch(`/jobs/${jobId}`);
}

/**
 * Rescue queue: applications the Module 4 headless agent could not finish
 * (BLOCKED = captcha/login wall, FAILED = crashed/stuck). These are exactly
 * the jobs that need a real browser + a human on standby.
 */
export async function listRescueJobs(candidateId, limit = 50) {
  const apps = [];
  for (const status of ['BLOCKED', 'FAILED']) {
    const params = new URLSearchParams({ status, limit: String(limit) });
    if (candidateId) params.set('candidate_id', candidateId);
    const batch = await apiFetch(`/applications?${params}`).catch(() => []);
    apps.push(...(batch || []));
  }
  // Resolve each application's job to get the URL. Dedupe by job.
  const seenJobs = new Set();
  const out = [];
  for (const app of apps) {
    if (!app.job_id || seenJobs.has(app.job_id)) continue;
    seenJobs.add(app.job_id);
    try {
      const job = await getJob(app.job_id);
      if (!job?.source_url) continue;
      out.push({
        url: job.canonical_url || job.source_url,
        title: job.title,
        company: job.company,
        dbJobId: job.id,
        applicationId: app.id,
        // Module 4 already tailored a resume for this application — reuse it
        // (via the signing /view endpoint) instead of re-running
        // prepare-package (faster + avoids failures on these hard jobs).
        resumeId: app.resume_id || null,
        rescueReason: app.failure_reason || app.status,
      });
    } catch { /* job gone — skip */ }
  }
  return out;
}

/**
 * Fetch the newest ATS verification code from the candidate's Gmail (via the
 * backend's code-catcher). Returns the code string or null (not arrived yet /
 * Gmail not connected). Each call is a short server-side check — poll it.
 */
export async function fetchVerificationCode(candidateId, afterEpoch, senderHint = '') {
  const params = new URLSearchParams({
    after_epoch: String(Math.floor(afterEpoch)),
    sender_hint: senderHint || '',
    timeout_s: '5',
  });
  const data = await apiFetch(`/verification/code/${candidateId}?${params}`);
  return data?.code || null;
}

/**
 * Fetch the candidate's stored job-portal credentials (gmail + password) from
 * the backend. The main candidates API strips the password, so this uses the
 * dedicated extension endpoint. Returns {login_email, gmail, password} with
 * empty strings when unavailable.
 */
export async function fetchPortalCredentials(candidateId) {
  try {
    const data = await apiFetch(`/verification/portal-credentials/${candidateId}`);
    return {
      login_email: data?.login_email || '',
      gmail: data?.gmail || '',
      password: data?.password || '',
    };
  } catch {
    return { login_email: '', gmail: '', password: '' };
  }
}

/** Best-effort: tell the backend a rescued application got submitted. The
 * status FSM may reject the transition — that's fine, the run log still has it. */
// PATCH an existing application to SUBMITTED. If the current state can't go
// straight to SUBMITTED (state-machine), bridge via QUEUED (which reaches
// SUBMITTED from nearly every state). Returns {ok, detail}.
async function patchToSubmitted(applicationId) {
  const patch = (status) => apiFetch(`/applications/${applicationId}/status`, {
    method: 'PATCH',
    body: JSON.stringify({ status, metadata: { via: 'indeed-extension' } }),
  });
  try { await patch('SUBMITTED'); return { ok: true, detail: 'PATCH → SUBMITTED' }; }
  catch (e1) {
    try { await patch('QUEUED'); await patch('SUBMITTED'); return { ok: true, detail: 'PATCH → QUEUED → SUBMITTED' }; }
    catch (e2) { return { ok: false, detail: `PATCH failed: ${e2.message || e1.message}` }; }
  }
}

/**
 * Record a SUCCESSFULLY-applied job as SUBMITTED in the backend, whether or not
 * an application row already exists.
 *   - Rescue jobs pass `applicationId` → PATCH that row to SUBMITTED.
 *   - Fresh Indeed/CSV jobs pass `candidateId` + `jobId` → create the row
 *     directly as SUBMITTED (idempotent: if it already exists, ensure SUBMITTED).
 * Returns {ok, detail} so the caller can log exactly what happened / failed.
 */
export async function recordApplicationSubmitted({ applicationId, candidateId, jobId, resumeId } = {}) {
  try {
    let appId = applicationId || null;
    if (!appId && candidateId && jobId) {
      const created = await apiFetch('/applications', {
        method: 'POST',
        body: JSON.stringify({
          candidate_id: candidateId,
          job_id: jobId,
          status: 'SUBMITTED',
          ...(resumeId ? { resume_id: resumeId } : {}),
        }),
      });
      if (created && created.id) {
        if (created.status === 'SUBMITTED') return { ok: true, detail: 'created application as SUBMITTED' };
        appId = created.id; // pre-existed in another state → PATCH below
      } else {
        return { ok: false, detail: 'create returned no application id' };
      }
    }
    if (!appId) return { ok: false, detail: 'no applicationId and no candidate+job to create one' };
    return await patchToSubmitted(appId);
  } catch (e) {
    return { ok: false, detail: e.message || String(e) };
  }
}

// Back-compat alias (rescue path).
export async function markApplicationSubmitted(applicationId) {
  return recordApplicationSubmitted({ applicationId });
}

export async function preparePackage(candidateId, jobId, screeningQuestions, opts = {}) {
  const settings = await getSettings();
  return apiFetch('/applications/prepare-package', {
    method: 'POST',
    body: JSON.stringify({
      candidate_id: candidateId,
      job_id: jobId,
      needs_cover_letter: opts.needsCoverLetter ?? settings.needsCoverLetter,
      screening_questions: screeningQuestions || [],
      skip_gate: opts.skipGate ?? settings.skipGate,
    }),
  });
}

export async function resumeViewUrl(resumeId) {
  // Backend returns a signed URL (JSON) or redirects straight to the PDF.
  const settings = await getSettings();
  const base = settings.apiBase.replace(/\/$/, '');
  const token = await getToken();
  const res = await fetch(`${base}/resumes/${resumeId}/view`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(`resume view failed (${res.status})`);
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    const body = await res.json();
    return body.url || body.signed_url || body.file_url;
  }
  return res.url; // followed redirect
}

/** Download any URL (signed Supabase link etc.) and return base64. */
export async function fetchAsBase64(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`download failed (${res.status}) for ${url.slice(0, 120)}`);
  const buf = await res.arrayBuffer();
  let binary = '';
  const bytes = new Uint8Array(buf);
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

export function hostOf(url) {
  try { return new URL(url).hostname.toLowerCase(); } catch { return null; }
}
