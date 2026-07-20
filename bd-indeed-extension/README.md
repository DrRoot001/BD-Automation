# BD Indeed Auto-Apply Extension

Chrome MV3 extension whose **sole purpose** is automating Indeed (Easy Apply /
SmartApply) job applications for candidates managed in the BD Automator
backend. It is a companion tool operated by a BD agent who monitors the run
and steps in when the automation flags something it can't do (CAPTCHA, Indeed
login, an unanswerable required question).

It lives inside the BD-Automator-Agent repo but **does not modify any project
code** — it only consumes the existing backend API.

---

## How a run works

1. **BD agent signs in** (popup) with their backend account
   (`POST /api/auth/login`).
2. **Selects a candidate** — fetched from the backend (`GET /api/candidates`,
   scoped by the agent's role exactly like the frontend).
3. **Loads jobs**:
   - *Database first:* `GET /api/jobs?source=indeed&candidate_id=…` — Indeed
     jobs discovered by Module 2, relevance-ordered against the candidate's
     base resume, already-applied jobs excluded.
   - *CSV fallback:* upload a CSV (or plain list) of Indeed job URLs. Rows
     already present in the DB list are deduped by `jk` key.
4. **Start.** The background worker opens one automation tab and works the
   queue job by job:
   - On the job page it scrapes title/company/description. CSV-only jobs are
     registered in the backend (`POST /api/jobs`) so tailoring has the real
     description.
   - It requests a **tailored resume** via `POST /api/applications/prepare-package`
     (the Module 3 Gemini pipeline). If that fails, it falls back to the
     candidate's base resume (`GET /api/resumes/{id}/view`).
   - It clicks **Apply with Indeed** and walks the SmartApply wizard:
     contact info → location → work-auth questions → resume upload → employer
     questions → voluntary self-ID → review → submit.
5. **Every form field** goes through the answer pipeline (see below).
6. On the review page it auto-submits (toggle in the popup to pause for a
   manual review instead), verifies the *"Your application has been
   submitted"* confirmation, then moves to the next job.

## Answer pipeline (how it knows what to fill)

Per scraped field, in order — first hit wins:

1. **Profile heuristics** — deterministic mapping from the candidate record
   (name, email, phone, LinkedIn, work authorization, years of experience,
   address parsed from the resume).
2. **Operator policy** — fixed rules for compliance/demographic questions
   (18+, sponsorship → No, background check → Yes, availability →
   Immediately, salary/hours from settings, self-ID answers per the operator
   demographic policy).
3. **Answer memory** — answers previously used for this candidate (persisted
   in `chrome.storage.local`, reused across jobs and runs).
4. **AI** — remaining questions are batched to **Gemini** (Anthropic
   fallback) with the candidate profile + parsed base resume as context.
   Option questions must be answered with one option verbatim; number fields
   digits-only. AI answers are saved back into answer memory.
5. Unresolved **optional** fields are left blank (`N/A` for free text);
   unresolved **required** fields flag the BD agent.

## Human-in-the-loop (BD agent monitoring)

The extension never tries to bypass CAPTCHAs, login walls, or verification
gates. When it hits one — or a required question it can't answer, or a stall
(watchdog, default 5 min) — it:

- shows a **red `!` badge** on the toolbar icon,
- fires a **desktop notification** with the reason,
- keeps the page untouched and **keeps polling**.

The BD agent solves the CAPTCHA / logs in / fills the field and clicks
Continue; the automation detects the page moved and resumes by itself. The
popup also has Pause / Skip job / Stop controls and a live activity log.

## Install

1. Copy `config.example.js` → `config.js` (`config.js` is gitignored) and
   *(optional, for zero-config installs)* paste your Gemini key into it — it
   seeds the defaults so agents never have to open settings. Never commit a
   real key.
2. Chrome → `chrome://extensions` → enable **Developer mode**.
3. **Load unpacked** → select this `indeed-extension/` folder.
4. Open the popup → ⚙ settings (skippable if `config.js` is pre-filled):
   - **Backend API base** — default `http://localhost:8000/api`.
   - **Gemini API key** — used only for screening questions the
     backend/profile can't answer (same key as `GEMINI_API_KEY` works).
   - Optional Anthropic key as AI fallback; desired salary / hours defaults.
5. Make sure the backend stack is running (`./dev.sh`) and the BD agent's
   Chrome profile is **logged into Indeed** (applications are submitted
   through the logged-in Indeed account).

> Hosting the backend or Supabase storage on a domain other than
> `localhost` / `*.supabase.co`? Add it to `host_permissions` in
> `manifest.json` and reload the extension.

## Packaging for Distribution

You can package the extension into a ZIP file for easy distribution and deployment.

Run the packaging script from the extension directory:
```bash
python3 package_extension.py
```
This generates a `bd-indeed-extension.zip` containing all the extension code. By default, it replaces the local config with a clean `config.example.js` mapping to `config.js` so that your private API keys or settings are **not** leaked in the zip file.

To force including your local `config.js` with your active configuration, use the `--local` flag:
```bash
python3 package_extension.py --local
```

## CSV format

Any of these work:

```
https://www.indeed.com/viewjob?jk=abc123
https://www.indeed.com/viewjob?jk=def456
```

```csv
title,company,url
Senior Data Engineer,Acme,https://www.indeed.com/viewjob?jk=abc123
```

Only `indeed.com` URLs are accepted (the column is auto-detected).

## Session behaviour

- **Login persists.** The backend issues Supabase tokens that expire after
  ~1 hour; the extension stores the BD agent's credentials in
  `chrome.storage.local` (local machine only) and silently re-authenticates
  on any 401 — no repeated login prompts.
- **Setup persists.** The selected candidate, fetched DB jobs, and the parsed
  CSV survive closing the popup — no re-uploading the CSV between opens.
  (The parsed rows are stored, not the file itself.)

## Web-app handoff ("Apply Manually")

The BD Automator web app's **Apply Manually** buttons (dashboard rows, the
application drawer, and the application detail page — shown on
`FAILED`/`BLOCKED` applications) hand the job to this extension instead of
just opening the page:

1. The app pings the extension (`PING` via `chrome.runtime.sendMessage` to the
   pinned extension ID — the manifest `key` keeps the ID stable on every
   machine). Not installed → the job page opens normally plus an install tip.
2. Installed → the app sends `MANUAL_APPLY {candidateId, jobUrl,
   applicationId, jobId, resumeId, title, company}` (origins whitelisted via
   `externally_connectable`).
3. The extension starts a one-job run in **fill-only mode** (`manualFill`):
   the engine opens the job, attaches the already-tailored resume, and fills
   every field it can — but **never submits**. The user reviews, completes
   anything left, and clicks submit themselves. Use the popup's
   **Mark applied** to write `SUBMITTED` back to the backend.

Requirements: the BD agent must be logged in from the popup, and no batch run
may be active (the handoff refuses with a clear reason otherwise). The
frontend reads the extension ID from `NEXT_PUBLIC_EXTENSION_ID`.

## Hard-site co-pilot (company/ATS sites)

Module 4's headless agent handles the well-behaved ATSs (Greenhouse, Lever,
…) at scale. This extension is the **co-pilot for the sites the agent can't
finish** — captcha walls, login gates, odd widgets — because it runs in the
BD agent's real Chrome with a human on standby. Three ways those jobs enter
the queue:

1. **Rescue blocked** (popup button) — fetches this candidate's applications
   the backend marked `BLOCKED`/`FAILED` and retries them here. On a successful
   apply the extension writes the outcome back to the backend
   (`PATCH /applications/{id}/status` → `SUBMITTED`), so a rescued application no
   longer shows as failed. The state machine permits `FAILED`/`BLOCKED → SUBMITTED`
   for exactly this rescue case. Only successes are written back — a rescue the
   extension couldn't finish leaves the status untouched.
2. **Indeed external-apply** — "Apply on company site" jobs are no longer
   skipped; the extension follows the redirect (new tab is adopted
   automatically) and works the company form.
3. **CSV** — rows may be any application-page URL, not just indeed.com.
4. **Paste URLs** — paste one URL per line directly in the popup (Indeed or
   company sites); no file needed.

On company sites the same engine runs: fill labelled fields via the answer
pipeline, attach the tailored resume inline, click submit. ATS entry dialogs
("Autofill with Resume / Apply Manually / Use my last application") are
handled — autofill preferred, "last application" never. Login/signup walls
are filled with the candidate's email + the **Candidate site password** from
the popup (never sent to AI). Captcha widgets flag the human immediately.
Because confirmation pages vary wildly, if no success message is detected
after submitting, the extension asks the human to verify and use
**Mark applied** / **Skip job** in the popup. The engine is injected ONLY
into the run's automation tab — never into normal browsing.

**AI page navigator (company sites).** Company/ATS pages have unpredictable
layouts and button labels. The deterministic engine runs first (fill fields
→ click a known Continue/Apply/Submit control); when it can't find a way
forward on a non-Indeed page, it falls back to **Gemini**: the visible
interactive elements are sent to the model, which picks the single next
control to click (or decides the application is submitted / needs a human).
Loop guards cap it at 20 AI steps per page and stop if it repeats the same
click. This only runs on external sites, only when heuristics are stuck, and
never on Indeed (which has deterministic buttons).

**Email-verification codes are auto-fetched.** When an ATS shows a "we sent
you a code" screen, the extension polls
`GET /api/verification/code/{candidate_id}` — a small additive backend
endpoint (`backend/app/routers/verification.py`, the only backend change this
extension ships) that wraps the existing Module 4 Gmail code-catcher. For
candidates with Gmail connected, the OTP is pulled from their inbox and
filled automatically; otherwise the human is flagged to type it.

## Scope and limitations

- LinkedIn is intentionally not supported.
- One automation tab per run; closing it skips the current job (use the
  popup's Skip/Stop buttons instead of closing the tab).
- `prepare-package` is synchronous and can take 1–3 minutes per job; the
  extension starts it as soon as the job is known so the PDF is usually ready
  by the resume step, and falls back to the base resume on failure/timeout.
- Company-site coverage is heuristic: simple single-page ATS forms fill
  end-to-end; heavily custom multi-step portals may need more human assists.

## Portal architecture (Indeed vs everything else)

The page engine is split into two **independent, host-gated** files so changes
for company sites can never break the working Indeed flow:

- **`content/indeed.js`** — the INDEED specialist. Injected by the manifest on
  `*.indeed.com` (all frames). Acts only on indeed.com pages/frames (job pages,
  SmartApply, and the Indeed-Apply widget iframe embedded on company pages).
  **Frozen for Indeed — do not add company-site logic here.**
- **`content/generic.js`** — the GENERIC engine for **every non-Indeed portal**
  (Lever, Greenhouse, Workday, Ashby, iCIMS, Taleo, and any unknown site). The
  background worker injects it (all frames) on non-indeed pages only. **Edit
  this file for any company/ATS behaviour.**

Each is a self-contained IIFE with its own copy of the primitives and a host
gate (`indeed.js` bails on non-indeed; `generic.js` bails on indeed), so the
two never drive the same page. This is deliberate isolation over DRY: the
Indeed flow is protected from company-site iteration.

## File map

```
manifest.json          MV3 manifest (indeed.com content script, module worker)
background.js          Orchestrator: run state machine, job queue, tab driving,
                       prepare-package + PDF download, notifications, watchdog;
                       injects generic.js on non-indeed sites
content/indeed.js      INDEED portal engine (indeed.com only)
content/generic.js     GENERIC portal engine (all other company/ATS sites)
lib/api.js             Backend REST client (auth, candidates, jobs, packages)
lib/answers.js         Answer pipeline: heuristics → policy → memory → AI
lib/ai.js              Gemini / Anthropic clients for screening questions
lib/csv.js             CSV / URL-list parsing for the fallback file
popup/                 Login, candidate & job selection, run monitor, settings
icons/                 Toolbar + notification icons
```
