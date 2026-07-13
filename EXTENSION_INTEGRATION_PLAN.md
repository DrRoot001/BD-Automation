# Extension Integration Plan — Manual-Apply Co-Pilot

**Goal:** When a headless application fails (`FAILED`/`BLOCKED`) and the user clicks **Apply manually** in the web app, hand that one job to the `bd-indeed-extension` Chrome extension. The extension opens the job page, pre-fills the candidate's resume/cover letter and known field values from the DB, and leaves the rest for the user to finish. If the extension isn't installed, the app prompts to install it first.

Both sides already share the Module 1 FastAPI backend. This is a **trigger + handoff** integration, not a rebuild — the extension's page-filling engine already exists.

---

## 1. Current state

### What's already built (extension)
- MV3 extension (`bd-indeed-extension/`), operated from its **own popup**: agent logs in, picks a candidate, loads a job **queue**, clicks Start; a background worker drives one automation tab job-by-job.
- **Two page engines:** `content/indeed.js` (Indeed only) and `content/generic.js` (every other ATS/company site) — the generic engine is the one we reuse.
- **Answer pipeline** (`lib/answers.js`): profile heuristics → operator policy → answer memory → Gemini AI.
- **Rescue queue** (`lib/api.js: listRescueJobs`): already pulls this candidate's `BLOCKED`/`FAILED` applications and retries them, writing `SUBMITTED` back on success. **This is the conceptual match for the manual-apply flow** — it just needs a different trigger.
- **Backend client** (`lib/api.js`): consumes existing endpoints — `/auth/login`, `/candidates`, `/jobs`, `/applications` (+`/status`), `/applications/prepare-package`, `/resumes/{id}/view`.
- **Fill-only capability already exists:** `settings.autoSubmit` toggle — set `false` and the engine fills but stops before submit.

### What's missing (the integration surface)
| Gap | Impact | Where |
|-----|--------|-------|
| **`verification` backend router absent** | Extension calls `/api/verification/code/{id}` and `/api/verification/portal-credentials/{id}` — neither exists. OTP auto-fill + portal login are dead. | `backend/app/routers/` (no `verification.py`); not registered in `main.py` |
| **No app→extension handoff** | Frontend can't tell the extension "fill this job now." No `externally_connectable`, no message listener, no frontend reference to the extension. | `manifest.json`, `background.js`, `frontend/` |
| **No install detection** | App can't tell if the extension is installed, can't prompt install. | `frontend/` |
| **Trigger model** | Extension is popup-queue-driven; vision is single-job app-driven. | `background.js`, `popup/` |
| **Live secrets in `config.js`** | Real Gemini + Anthropic keys + portal password, dir untracked & not gitignored → leak on commit. | `bd-indeed-extension/config.js`, `.gitignore` |

---

## 2. Target architecture

```
┌─────────────────────┐   1. click "Apply manually" on a FAILED app
│  Web app (Next.js)  │
│  applications/[id]  │   2. ping extension (installed?)
└──────────┬──────────┘        │
           │ installed?        │ not installed
           │ yes               ▼
           │            ┌──────────────────┐
           │            │ Install CTA page │  (Chrome Web Store / unpacked)
           │            └──────────────────┘
           ▼
   chrome.runtime.sendMessage(EXT_ID, {           3. handoff payload:
     type: 'MANUAL_APPLY',                            candidate_id, application_id,
     candidate_id, application_id, job_id, job_url,   job_id, job_url, mode:'fill-only'
   })
           │
           ▼
┌──────────────────────────────────────────┐
│ Extension background.js                    │  4. onMessageExternal handler
│  - auth (reuse stored backend token)       │  5. prepare-package / reuse tailored resume
│  - open job_url in a tab                   │  6. inject generic.js, autoSubmit=false
│  - run generic engine (fill-only)          │  7. fill fields + attach resume/cover letter
└──────────────────────────────────────────┘  8. user completes + submits manually
           │
           ▼  (optional) user clicks "Mark applied" → PATCH /applications/{id}/status → SUBMITTED
```

**Handoff mechanism:** `externally_connectable` + `chrome.runtime.sendMessage(EXTENSION_ID, msg)` from the app's origin. Requires:
- A **stable extension ID** — set a `"key"` in `manifest.json` so the ID is fixed across machines (needed for the frontend to target it).
- `externally_connectable.matches` listing the app origins (`http://localhost:3000/*` + the production frontend origin).
- An `onMessageExternal` listener in `background.js`.

---

## 3. Phased work breakdown

### Phase 0 — Secure & commit the extension *(prereq, small)*
- Rotate the exposed Gemini + Anthropic keys (they sat in an untracked file — treat as compromised).
- Blank the keys/password in `config.js` (the `getSettings()` fallback in `lib/api.js` already reads from `chrome.storage` when config is empty).
- Add `bd-indeed-extension/config.js` to `.gitignore`; commit a `config.example.js` template.
- Then commit `bd-indeed-extension/` to the repo.
- *(Tracked as background task `task_ef6b7152`.)*

### Phase 1 — Backend `verification` router *(unblocks already-written extension code)*
- Add `backend/app/routers/verification.py` with:
  - `GET /api/verification/code/{candidate_id}?after_epoch&sender_hint&timeout_s` → wraps `browser_automation/verification/code_fetcher.py` (Gmail OTP catcher). Returns `{code}`.
  - `GET /api/verification/portal-credentials/{candidate_id}` → returns `{login_email, gmail, password}` (the main `/candidates` API strips `password`; this is the deliberate exception, auth-scoped).
- Register the router in `backend/app/main.py` (mirror the other `app.include_router(...)` calls; prefix `/api/verification`).
- Reuse the existing auth dependency + role scoping used by `candidates`/`applications` routers.
- **Verify:** extension's `fetchVerificationCode` + `fetchPortalCredentials` succeed against a running backend.

### Phase 2 — Extension: accept an externally-triggered single job
- `manifest.json`: add stable `"key"`, `"externally_connectable": { "matches": ["http://localhost:3000/*", "<prod-frontend-origin>/*"] }`.
- `background.js`: add `chrome.runtime.onMessageExternal` handler for `{type:'MANUAL_APPLY', candidate_id, application_id, job_id, job_url}`:
  1. Ensure backend auth (reuse stored token; the app can also pass a short-lived token if the extension isn't logged in — **open decision**).
  2. Resolve the resume: reuse the application's already-tailored resume (`app.resume_id` → `/resumes/{id}/view`) exactly like the rescue path; fall back to `prepare-package` only if none.
  3. Open `job_url` in a new tab, inject `generic.js`, run in **fill-only mode** (`autoSubmit=false`).
  4. Surface progress/needs-human via the existing badge + notification path.
- Add a lightweight `{type:'PING'}` responder for install detection (respond `{installed:true, version}`).
- Reuse the rescue-queue engine — do **not** fork a new page driver.

### Phase 3 — Frontend: trigger + install detection
- `applications/[id]` (and the applications list): wire the existing **Apply manually** action for `FAILED`/`BLOCKED` rows to:
  1. `chrome.runtime.sendMessage(EXT_ID, {type:'PING'})` with a short timeout.
  2. If no response → show **Install CTA** (Web Store link or unpacked-load instructions).
  3. If installed → send the `MANUAL_APPLY` payload; show an inline "Filling in your browser…" state.
- Add `NEXT_PUBLIC_EXTENSION_ID` env var (`frontend/lib/`), so the ID isn't hard-coded.
- Optional: after the user finishes, offer **Mark applied** → `PATCH /applications/{id}/status` → `SUBMITTED` (bridge via `QUEUED` if the FSM rejects the direct transition, as the extension already does).

### Phase 4 — Polish
- Cover-letter attach in fill-only mode (extension already downloads PDFs via `/resumes/{id}/view` + `fetchAsBase64`).
- Decide submit flow: fully manual vs. "fill then user clicks submit" vs. "auto-submit after user confirms."
- Docs: fold install + usage into the extension README; note the new backend router.

---

## 4. Open decisions (need your call)

1. **Extension auth.** Reuse the extension's own popup login (agent logs in once), or have the app pass a scoped token on handoff so the user never sees the popup? Popup login is simpler; app-passed token is smoother UX.
2. **Distribution.** Chrome Web Store listing (clean install + stable ID + auto-update) vs. internal unpacked load (no store review, but manual install + `chrome://extensions` developer mode). Affects the install-CTA UX.
3. **Submit ownership.** Confirm the intended flow is strictly "extension fills, user reviews & submits" — i.e. `autoSubmit=false` always for this path (vs. the batch popup flow which auto-submits).
4. **Scope of manual-apply.** Only `FAILED`/`BLOCKED` applications, or any job the user wants to apply to manually?

---

## 5. Effort / sequencing

| Phase | Depends on | Rough size |
|-------|-----------|-----------|
| 0 — secure secrets | — | S |
| 1 — verification router | 0 | S–M |
| 2 — extension trigger | 1 | M |
| 3 — frontend trigger + detect | 2 | M |
| 4 — polish | 3 | S |

Start with **Phase 1** (smallest, unblocks code that's already written and independently testable), then 2→3 which are the actual "next phase" work.
