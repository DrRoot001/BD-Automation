# Handoff v8 — Module 4 (Browser Automation): full state, file map, and rules of engagement

> Written 2026-07-16 by the outgoing senior AI-automations engineer for the next
> agent (this file is meant to be handed to the Antigravity IDE / any AI coding
> assistant). Read it **top to bottom before touching any code**. It supersedes
> handoff_v7.md. Everything here is grounded in the live codebase and verified
> runs from this session — where something is unproven or uncertain it says so
> explicitly.

---

## 0. RULES OF ENGAGEMENT — read before you write a single line

**The #1 requirement from the operator: DO NOT HALLUCINATE. DO NOT ASSUME.**
This module drives a *real browser* that submits *real job applications on real
candidates' behalf*. A wrong guess wastes money (LLM + captcha + proxy) and can
mis-submit or trip anti-bot walls. Follow these rules literally:

1. **Verify before you change.** Read the actual file (and the functions it
   calls) before editing it. Never edit a file you have not opened in this
   session. The Edit will fail if the string doesn't match — that's a feature,
   not an obstacle; do not work around it by guessing.
2. **Never invent APIs, selectors, env vars, file paths, function names, or DB
   columns.** If you need a selector for a live form, INSPECT the real page with
   a read-only Playwright script (see `scratchpad/inspect_*.py` pattern in §11) —
   do not guess CSS. If you need a DB column, query `information_schema.columns`
   first (the schema is NOT what you'd assume — e.g. the URL column is
   `jobs.source_url`/`jobs.canonical_url`, not `job_url`; the applications table
   has `resume_id`, not `resume_url`; `applications.paused` is an integer).
3. **Ground every claim in evidence.** "The form has field X" must come from a
   DOM dump, a screenshot, or a log line — not from the ATS's generic reputation.
   The previous handoff (v7) asserted this job was "Recruitee" and that the
   résumé field was a "paste-URL text field"; **both were wrong** — offline DOM
   inspection proved it's TeamTailor and the field is the real Dropzone file
   input. Inspect first, then act.
4. **Root-cause before you patch.** Reproduce the failure (a read-only inspector
   or a `DRY_RUN_NO_SUBMIT=true` run), find the actual cause, then fix. Do not
   pattern-match a fix onto a symptom.
5. **Keep the legacy engine (`USE_AGENT_LOOP=true`) as the production default.**
   It is battle-tested. The perception framework (`USE_AGENT_LOOP=false`) is R&D
   and is NOT production-ready. Do not flip the default.
6. **Test what you change.** Run `pytest backend/app/browser_automation/tests/ -q`
   (221 passing as of this handoff). Add a fixture test for any widget/logic you
   change. For anything that fills a live form, do ONE `DRY_RUN_NO_SUBMIT=true`
   run and read the logs before ever doing a real submit.
7. **Do not live-grind.** Building/fixing an adapter is an OFFLINE job: inspect
   the real DOM read-only, encode it, unit-test with fixtures, then ONE live
   verify run. Do not burn LLM/captcha budget looping one live job at a time.
8. **Memory files are runtime data, not code.** `learned_fixes/*.json`,
   `learned_fixes/portals/*.json`, `data/field_memory/*.json`, and
   `data/sessions/*.json` are written by the running system. Do not hand-edit
   them as a "fix"; fix the code that writes them.
9. **Secrets never enter code, logs, or the LLM prompt.** `.env` is gitignored.
   The candidate password is injected only at password-field fill time (see §7).
   Never print it, never commit it, never put it in `candidate_profile`.
10. **When unsure, say so and stop** — surface the uncertainty to the operator
    rather than guessing. "I don't know yet, here's how I'd find out" is correct;
    a confident wrong answer is not.

---

## 1. THE MISSION (north star)

A **picture-perfect autonomous job-application engine**. The operator hits **Auto
Apply** (frontend) or dispatches a job, and for each matched job the browser
opens, fills the form end-to-end (identity, screening questions, EEO, salary,
custom radios/checkboxes/dropdowns/comboboxes, résumé upload), logs in with the
candidate's own credentials when a portal requires it, clears any captcha,
submits, and records a verified `SUBMITTED` — across Greenhouse, Lever, Ashby,
Workday, iCIMS, Dice, CareerPlug, Careers-Page (Manatal), TeamTailor, and any
unknown ATS a job resolves to. Failures must be **rare, honest, and terminal** —
never a silent half-submit, never a retry-storm, never a fabricated field.

We are NOT fully there. The systemic blockers are fixed and the path is a known,
finite pattern (per-ATS adapters + memory), not a research problem. One class of
site (see §9, TeamTailor's realtime anti-automation submit wall) is a genuine
hard wall we have not beaten.

---

## 2. WHERE WE ARE RIGHT NOW (verified status)

- **Production engine = the legacy vision `AgentLoop`** (`agent/loop.py`, ~8.5k
  lines), selected by `USE_AGENT_LOOP=true` (the default). It does deterministic
  prefill (identity/EEO/policy/file-upload with NO LLM) and uses the LLM only for
  judgment. It is what wins today. **Keep it default.**
- **Perception framework** (`perception/` + `reasoning/` + `autonomous/`) is a
  clean, evidence-grounded rewrite wired behind `USE_AGENT_LOOP=false`. It is
  **R&D, not production** — it has no deterministic prefill and got stuck live on
  a plain Ashby form the legacy loop submits cleanly. Do not promote it yet.
- **Proven live this session:** a real `SUBMITTED` for Mark Anderson on **Lever**
  ("D365 CE, Power Platform Technical Engineer @ MCA Connect"), retry_count=0,
  ~5.5 min, hCaptcha auto-solved via Anti-Captcha, db_persisted=True.
- **Tests: 221 passing** (`pytest backend/app/browser_automation/tests/ -q`).
- **Infra is reachable** from the operator's box (Supabase Postgres via the
  TRANSACTION pooler, Upstash Redis) — but the local network has intermittent
  DNS blips to `upstash.io` / `github.com` (`getaddrinfo failed`); the stack is
  now resilient to them (see §8).

---

## 3. HOW AN APPLY ACTUALLY FLOWS

```
Frontend "Auto Apply"  →  Celery task:dynamic_apply (main worker)
  → /jobs/for-matching (pgvector) picks the candidate's closest jobs
  → run_matching_for_candidate: LLM fit-score → gate (APPLY_SCORE_THRESHOLD)
  → M3 orchestrator: tailor résumé + cover letter, upload to Supabase
  → execute_application.delay(package)   (browser worker, queue:application_execution)
  → ApplicationExecutor.execute():
      pre-flight (idempotency, spam-backoff, résumé download, liveness, robots,
                  ACCOUNT-WALL creds check, rate-limit)
      get_adapter(platform) → adapter.navigate_to_application (resolve URL,
                  log in if account-walled, open the form / reveal it)
      adapter.set_candidate_credentials(creds)   ← DB gmail+password, secure
      PageAgent.classify_page (vision: FORM/LISTING/BLOCKED/…)
      ── AgentLoop.run()   ← THE FILL ENGINE (USE_AGENT_LOOP=true)
      portal_memory.record(outcome, actions)     ← self-learned playbook
      adapter.verify_success → transition_status(SUBMITTED) → screenshot
```

- `services/executor.py` (~1.8k lines) is the orchestrator.
- `agent/loop.py` is the AI fill loop.
- `adapters/*` are per-ATS nav/quirks; `adapters/hints.py` is the per-ATS
  cheat-sheet fed to the LLM; `adapters/registry.py` routes a job's
  `source`/host → the right adapter.

---

## 4. FILE-BY-FILE MAP of `backend/app/browser_automation/`

> Descriptions are the files' real purpose. `agent/loop.py`, `services/executor.py`,
> and `adapters/hints.py` are the three that matter most — start there.

### Top level
| File | What it is |
|---|---|
| `frame_utils.py` | Helpers to resolve/wait-for a live iframe (`get_live_frame`, `wait_for_stability`) — Greenhouse/Workday re-inject their form iframe; this keeps the loop scoped to the live frame. |
| `hosts.py` | **Single source of truth** for host classification. `BOT_WALLED_HOSTS` (need the residential proxy + pre-flight skip), `LOGIN_WALLED_HOSTS`, `is_bot_walled()`, `should_skip_preflight()`. Add a host here to route it through the proxy. |

### `agent/` — the production fill engine
| File | What it is |
|---|---|
| `agent/loop.py` | **THE production engine.** Vision-driven observe→decide→act loop. Deterministic prefill (`_deterministic_prefill`, `_deterministic_file_upload`), custom-widget commit (`_commit_radio_group`, `_commit_checkbox`), async résumé-upload wait (`_await_async_upload`), DOM captcha classifier, pre-submit completeness gate, post-submit verification, system-prompt builder (`_build_system_prompt`), mid-run platform reclassify (`_maybe_reclassify_platform`, `_detect_ats_from_dom`), and the **global SSO/manual-login guards** (`_is_sso_text`/`_is_sso_href`, click + navigate refusal). Huge; use grep, edit surgically. |
| `agent/page_agent.py` | Vision page classifier — one Gemini call to bucket the current page (FORM/LISTING/SUCCESS/BLOCKED/ERROR/UNKNOWN) before the loop commits. |
| `agent/portal_memory.py` | **Per-host self-learned playbook** (`learned_fixes/portals/<host>.json`). Records working selectors + flow AND (new this session) the **failed selectors to AVOID**; renders a "USE-FIRST / DO NOT REPEAT" block into the next run's prompt. Best-effort, gated by `PORTAL_MEMORY_ENABLED`. |
| `agent/learned_fixes.py` | Per-ATS selector cache (`learned_fixes/<ats>.json`) for apply/submit/résumé selectors — tried before hardcoded lists. |
| `agent/failure_diagnoser.py` | Post-failure analysis helper (Phase-5 R&D); classifies why a run failed. |

### `adapters/` — per-ATS nav/quirks (the fill is shared; adapters only do platform-specific nav/auth)
| File | What it is |
|---|---|
| `adapters/base.py` | `BasePlatformAdapter` ABC + shared credential plumbing: `set_candidate_credentials`, `_login_credential(field, *env_fallbacks)` (per-candidate value wins, env is the fallback). |
| `adapters/autonomous_base.py` | `AutonomousAdapter` — the base most adapters subclass. Drives the shared perception loop; adapters override only `_resolve_target_url`, `prepare`, `_resolve_frame`. Declares `hints_key`, `iframe_selector`, `login_gated`. |
| `adapters/handlers.py` | Shared verification + captcha handler builders for the autonomous adapters. |
| `adapters/registry.py` | **`get_adapter(platform)` router** — maps a job's `source`/host/URL substring → the right adapter class. Keep in sync with `hints.get_platform_hints`. TeamTailor is registered here with a `recruitee` alias. |
| `adapters/hints.py` | **Per-ATS cheat-sheet** (`get_platform_hints`) injected into the LLM prompt: apply/submit selectors, success patterns, url_hint, and detailed `quirks` per platform (verified against real DOMs). ~1.2k lines. |
| `adapters/generic.py` | Fallback adapter for unknown ATSes — pure perception loop, no quirks. |
| `adapters/greenhouse.py` / `lever.py` / `ashby.py` / `workday.py` / `icims.py` / `smartrecruiters.py` / `jobvite.py` | Classic ATS adapters (nav + iframe/login quirks). |
| `adapters/dice.py` | Dice Easy Apply — **account-walled**; scripted email+password login (`_LOGIN_URL`), wizard flow, external-redirect passthrough. |
| `adapters/glassdoor.py` | Glassdoor — Easy Apply modal OR external-ATS passthrough; **account-walled** scripted email+password login; Turnstile handling; delegates to inner ATS when the posting redirects out. |
| `adapters/linkedin.py` / `indeed.py` / `ziprecruiter.py` / `builtin.py` | Account-walled / login-gated boards; each surfaces a clear `LOGIN_REQUIRED` when creds are missing. BuiltIn uses a magic-link/OTP flow. |
| `adapters/talent.py` / `himalayas.py` / `remoterocketship.py` / `remote100k.py` | **Aggregators / passthroughs** — the listing has no form; they scrape the real ATS apply URL off the listing and delegate to the inner ATS. `remoterocketship._detect_ats_from_url` is the shared URL→ATS mapper (used by many adapters + the loop's reclassify). |
| `adapters/careerplug.py` | CareerPlug (Rails ATS) — normalizes to `/apps/new`; reCAPTCHA at submit. |
| `adapters/careerspage.py` | Careers-Page / Manatal — single-page form; deterministically fixes the salary currency/frequency `<select>`s in `prepare()`. |
| `adapters/teamtailor.py` | **TeamTailor** (the ATS handoff_v7 mislabelled "Recruitee"). White-labelled onto employer domains; DOM-signature detection (`is_teamtailor_dom`), deterministic form reveal (`reveal_teamtailor_form`). **NOTE:** its submit is gated behind a Pusher realtime anti-automation wall — see §9. |
| `adapters/session_utils.py` | Account-walled session helpers: `load_candidate_credentials(candidate_id)` → `{login_email, password, gmail}` from the DB; `invalidate_session_file`; session-file paths. |

### `autonomous/` + `perception/` + `reasoning/` — the R&D perception framework (USE_AGENT_LOOP=false; NOT production)
| File | What it is |
|---|---|
| `perception/collector.py` (+ `models.py`, `scripts.py`) | `BrowserStateCollector` — one reusable perception pass that builds a structured `BrowserState` from screenshot + scoped DOM via injected JS. |
| `reasoning/engine.py` (+ `models.py`, `memory.py`, `prompt.py`) | `DecisionEngine` — evidence-grounded reasoning over a `BrowserState`; produces a typed `NextAction`. |
| `autonomous/agent.py` | `AutonomousAgent` — the observe→reason→act loop that ties collector + engine + executor. |
| `autonomous/action_executor.py` | Turns a `NextAction` into a Playwright op. Has the radio/combobox commit and (new) the **SSO click guard**. |
| `autonomous/conditions.py` | Deterministic detection of blocking/terminal page conditions (OTP, MFA, duplicate, captcha, nav-fail, validation, success, required-fields). |
| `autonomous/recovery.py` | `RecoveryController` — deterministic resilience reflexes (dismiss overlays, retry, wait, adopt popups, unstick stalls) with bounded budgets. |
| `autonomous/models.py` | Result/status types for the perception agent. |

### `browser/` — Chromium + stealth + proxy
| File | What it is |
|---|---|
| `browser/context_manager.py` | Launches Chromium (real Chrome channel, `HEADLESS=false`), applies stealth, decides the **residential proxy by TARGET host** (`is_bot_walled`), and restores/persists the per-candidate browser **session cache in Redis** — now **fail-open** (a Redis blip degrades to a fresh context instead of crashing the run). |
| `browser/stealth_config.py` | `get_stealth_config` + `build_stealth_init_script` — webdriver mask + viewport/timezone/locale randomization. |
| `browser/flaresolverr.py` | Optional FlareSolverr client for Cloudflare clearance. |

### `captcha/`
| File | What it is |
|---|---|
| `captcha/service.py` | Dispatches to the configured provider (`CAPTCHA_PROVIDER`, currently `anticaptcha`). Solves reCAPTCHA v2 / hCaptcha / (proxied) Cloudflare Turnstile. `solve_cloudflare_challenge` has an interstitial guard (`_is_cloudflare_interstitial`) that **prevents the phantom-Turnstile reload loop**. |
| `captcha/ai_solver.py` / `audio_solver.py` / `models.py` | AI-vision solver, Whisper-backed reCAPTCHA audio solver, result types. |

### `forms/` — deterministic form primitives (used by the loop + legacy paths)
| File | What it is |
|---|---|
| `forms/detector.py` | Form/field detector. |
| `forms/filler.py` / `llm_filler.py` | Deterministic filler + LLM-driven filler. |
| `forms/memory.py` | **Field Memory** — per-candidate answer cache (`data/field_memory/<candidate_id>.json`). Identity fields always come from the candidate's own file; global cross-candidate recall is gated by `STRICT_MEMORY_ISOLATION` (default true = no cross-candidate bleed). |
| `forms/uploader.py` / `models.py` | Résumé/cover file upload helper + models. |

### `llm/`
| File | What it is |
|---|---|
| `llm/claude_client.py` | The LLM client for the agent loop. **Provider priority is resolved via `runtime_config.llm_key_priority()`** (admin toggle → env `LLM_KEY_PRIORITY` → default order). **Gemini is the primary provider** (see §8/§10). Password never enters here. |
| `llm/telemetry.py` | Per-session token/cost telemetry. |

### `services/` — orchestration + supporting services
| File | What it is |
|---|---|
| `services/executor.py` | **`ApplicationExecutor.execute()`** — the orchestrator: pre-flight gates, adapter dispatch, credential injection, engine toggle, portal-memory recording, status transitions, screenshots. |
| `services/state_machine.py` | Application status FSM — every status transition is validated here (`FOUND→…→SUBMITTED→CONFIRMED→…`, plus `FAILED/BLOCKED/REJECTED/…`). |
| `services/rate_limiter.py` | Per-candidate/per-platform rate limiting (Redis). Fails OPEN if Redis is unavailable. |
| `services/robots_validator.py` | robots.txt gate (skippable via `DISABLE_ROBOTS_CHECK=true`). |
| `services/screenshot.py` | Screenshot capture + Supabase upload. |
| `services/resume_enricher.py` | Extracts candidate facts from the résumé PDF to fill profile gaps. |
| `services/candidate_pdf.py` | Identity-safe résumé/cover PDF generation. |
| `services/platform_review.py` | Aggregates per-platform run outcomes/errors (`learned_fixes/platform_reviews.json`). |
| `services/models.py` | `ApplicationPackage`, `ApplicationResult`, `LoopResult`, etc. |

### `verification/`
| File | What it is |
|---|---|
| `verification/code_fetcher.py` (+ `__init__.py`) | Gmail-OAuth-backed verification-code fetcher for ATS submit-flow email walls (the "we emailed you a code" step). |

### `tests/` — 221 passing
Widget-commit, routing, memory, hosts, perception, reasoning, recovery, adapter,
stealth, and policy fixtures. New this session: `test_teamtailor_adapter.py`,
`test_portal_memory_avoid.py`, `test_sso_login_policy.py`,
`test_candidate_credentials.py`, `test_hosts_classification.py`. **Run the whole
suite after any change here.**

---

## 5. THE TWO ENGINES

- **Legacy `AgentLoop` (`agent/loop.py`, `USE_AGENT_LOOP=true`, DEFAULT, PRODUCTION):**
  deterministic prefill + LLM judgment. Resilient when the LLM degrades. This is
  the one that works.
- **Perception framework (`perception/`+`reasoning/`+`autonomous/`, `USE_AGENT_LOOP=false`, R&D):**
  cleaner design for *unknown* sites long-term, but no deterministic prefill →
  slow, token-heavy, brittle on custom widgets. **Not production.** Promoting it
  needs a deterministic-prefill floor first.

---

## 6. MEMORY LAYERS (three of them — know which does what)

1. **Field Memory** (`forms/memory.py`, `data/field_memory/<candidate_id>.json`) —
   per-candidate answer cache (name/email/phone/screening answers). Speeds up
   KNOWN labels; keyed per candidate. `STRICT_MEMORY_ISOLATION=true` (default)
   prevents cross-candidate global recall.
2. **Learned Fixes** (`agent/learned_fixes.py`, `learned_fixes/<ats>.json`) —
   per-ATS apply/submit/résumé selector cache.
3. **Portal Memory** (`agent/portal_memory.py`, `learned_fixes/portals/<host>.json`) —
   per-host self-authored playbook injected into the next run's prompt. This
   session it was upgraded to **learn from MISTAKES**: it records the failed
   selectors (dead-ends) per host and renders a "❌ DO NOT REPEAT" block, so
   repeat applies to the same portal stop making the same wrong move (and go
   faster). Self-prunes a selector that later works.

**Honest limitation (do not over-promise to the operator):** the engine is a
vision agent that RE-REASONS every step; memory gives strong hints but does NOT
replay a saved macro. The next real speed win is a **deterministic replay** layer
(replay a host's proven flow, fall back to vision on divergence) — NOT yet built.

---

## 7. LOGIN / CREDENTIALS — global policy (enforced everywhere)

- **Manual email+password login ONLY. Never "Continue with Google"/Apple/
  Facebook/Microsoft/LinkedIn or any OAuth flow.** Enforced globally by:
  - the legacy loop's hard **click guard** (refuses SSO by button text + resolved
    element text/href) and **navigate_url guard** (refuses OAuth destinations);
  - the system-prompt **GLOBAL LOGIN POLICY** + **login block** (which hands the
    agent the candidate's login email and instructs manual login);
  - the new-framework executor's SSO click guard;
  - per-ATS `quirks` in `hints.py` that reinforce "no Google/SSO".
- **Credentials come from the DB per candidate** via
  `session_utils.load_candidate_credentials()`: `login_email = gmail or email`,
  `password = candidates.password`. Injected into the adapter with
  `set_candidate_credentials()` and, in the loop, substituted **only at
  password-field fill time** — the password never enters the LLM prompt/history/logs.
- **Data dependency:** many candidate rows have EMPTY `gmail`/`password`. An
  account-walled apply (Dice/Glassdoor/iCIMS/Workday/ZipRecruiter) correctly
  fails "requires credentials" when the password is empty. A blank edit-form
  field can no longer WIPE stored creds (guard added in
  `routers/candidates.py::update_candidate`). If a login blocks, FIRST check the
  candidate actually has `gmail`+`password` in the DB — don't assume it's a bug.

---

## 8. WHAT WAS FIXED THIS SESSION (with file refs)

- **TeamTailor adapter built** (`adapters/teamtailor.py`, `hints.py`,
  `registry.py`, `remoterocketship.py` host table). DOM-signature detection +
  deterministic form reveal.
- **Async Dropzone résumé upload race fixed** (`agent/loop.py::_await_async_upload`,
  wired into `_deterministic_file_upload`; pre-submit gate re-attaches a missing
  required résumé). TeamTailor/Recruitee/Workable upload the file to S3 in the
  background; submitting before it commits = silent "résumé required" reject.
- **Mandatory-question detection** (`agent/loop.py`): `data-question-mandatory`
  containers + a required range slider at min/0 counts as unfilled.
- **Redis session-cache fail-open** (`browser/context_manager.py`): a Redis blip
  degrades to a fresh browser context instead of crashing the run.
- **Gemini is the primary LLM by default** (`services/runtime_config.py`
  `llm_primary_provider` default = `"gemini"`; `LLM_KEY_PRIORITY` also
  Gemini-first). This matches CLAUDE.md ("Gemini is the default").
- **DB uses the Supabase TRANSACTION pooler** (`.env` `DATABASE_URL` port
  `6543`, code already sets `statement_cache_size=0`; comment updated in
  `app/database.py`) — fixes `EMAXCONNSESSION` (session-mode 15-client cap) that
  500'd Auto Apply under the full stack.
- **Celery result-backend resilience** (`app/celery_app.py`
  `result_backend_always_retry=True`) — a transient Upstash DNS blip no longer
  fails a completed task.
- **ATS submit-wall fails fast** (`app/tasks/browser_automation.py`): a
  "Submit clicked Nx without success" outcome is now TERMINAL
  (`failure_reason=ATS_SUBMIT_WALL`) — no retry-storm / re-open-refill loop.
- **Portal memory learns from mistakes** (`agent/portal_memory.py`) — see §6.
- **Credential-wipe guard** (`routers/candidates.py`) — see §7.
- **Global SSO/manual-login enforcement gaps closed** (`agent/loop.py`
  navigate_url guard; `autonomous/action_executor.py` click guard) — see §7.

All committed on branch `rehan-m4`, PR #2 on the `sabih-haider1/BD-Automation`
remote. 221 tests pass.

---

## 9. KNOWN WALLS & OPEN ITEMS (honest — do not paper over)

- **TeamTailor submit wall (UNSOLVED).** On `careers.westerncomputer.com` (and
  any TeamTailor site), even a 100%-correct fill (all mandatory answered, résumé
  `dz-success`, submit button enabled) is **silently rejected**: the submit POST
  returns `200` with a Turbo-Stream `tt_redirect_to /applications/new` (bounce to
  a blank form, no error). Proven independent of: field completeness, datacenter
  vs residential IP, stealth vs not. **No captcha exists on the form.** The form
  is gated behind a **Pusher realtime presence** connection that never establishes
  in Playwright (zero WebSockets). This is a real anti-automation wall, not a fill
  bug. The engine now fails fast (`ATS_SUBMIT_WALL`) instead of retry-storming.
  Anyone attempting this: it needs establishing the Pusher/Turbo session, and may
  not be feasible from Playwright — inspect, don't assume it's a quick fix.
- **Memory ≠ replay.** Repeat applies are faster/cleaner now (avoid-list) but the
  engine still re-reasons each step. Deterministic replay is the next speed lever.
- **Candidate credential data.** Many candidates lack `gmail`/`password`; fill
  them for account-walled applies. The wipe-guard prevents future loss.
- **Local network DNS blips** to Upstash/GitHub (`getaddrinfo failed`) —
  environmental; the stack tolerates them, retries succeed.
- **Per-ATS long tail.** As new ATSes appear, build adapters OFFLINE (inspect →
  encode → fixture-test → one live verify). Do not live-grind.

---

## 10. ENV / INFRA REALITIES

- **LLM:** `GEMINI_API_KEY` is primary (`gemini-3.5-flash` default). Keep it
  funded — it hits its Google-AI-Studio monthly spend cap under load and then
  applies degrade. `LLM_KEY_PRIORITY`/admin toggle can reorder providers.
- **DB:** `DATABASE_URL` MUST use the Supabase **transaction pooler (port 6543)**,
  not session mode (5432). Code is configured for it (`statement_cache_size=0`).
- **Redis:** Upstash (`rediss://`), broker + result backend. Session cache fails
  open; result backend retries on blips.
- **Captcha:** `CAPTCHA_PROVIDER=anticaptcha` (funded). Solves reCAPTCHA v2 /
  hCaptcha / proxied Turnstile.
- **Proxy:** IPRoyal residential (`PROXY_URL`), scoped to **bot-walled target
  hosts** (`hosts.py`). `PROXY_ALL_HOSTS=true` forces it everywhere.
- **Toggles:** `USE_AGENT_LOOP=true` (legacy, default). `HEADLESS=false` (real
  Chrome). `DISABLE_ROBOTS_CHECK=true`. `APPLY_SCORE_THRESHOLD`. `STRICT_MEMORY_ISOLATION=true`.
  `PORTAL_MEMORY_ENABLED=true`. `DRY_RUN_NO_SUBMIT=true` to fill without submitting.
- **Queues (per-dev suffix, default OS username, e.g. `_rehan`):** main worker on
  `celery_<sfx>,queue:job_discovery_<sfx>,queue:job_processing_<sfx>,queue:resume_generation_<sfx>,queue:email_scan_<sfx>`;
  BROWSER worker on `queue:application_execution_<sfx>` (`--pool=threads`).
- **Candidates with creds:** Mark Anderson (`bd4c0149`), Franklin Davis
  (`f6be0e5a`), Sabih Haider (`76a9f624`), Dexter, James, Ammar. Many others have
  EMPTY creds. `automation_paused=1` silently skips a candidate in the matcher.

---

## 11. HOW TO RUN, TEST, VERIFY

**Tests (no infra needed):**
```
python -m pytest backend/app/browser_automation/tests/ -q      # 221 pass
```

**Full stack (Windows):** `run_all.bat` (backend :8000 + main worker + browser
worker + beat + frontend). `stop_all.bat` to stop. NOTE: beat auto-dispatches
matching + Auto-Apply on a timer — pause candidates you don't want auto-applied,
or run only backend + browser worker for manual control.

**Dispatch ONE job directly (bypasses matcher/gate — best for verifying a fix):**
```python
from app.tasks.browser_automation import execute_application
from app.celery_app import EXECUTION_QUEUE
# First reset the app row: status='QUEUED', failure_reason=NULL, retry_count=0
execute_application.apply_async(
    args=[{"application_id": "<uuid>", "candidate_id": "<uuid>"}], queue=EXECUTION_QUEUE)
```

**Offline DOM inspection (the adapter-building workflow — DO THIS FIRST):** write a
plain read-only Playwright script (`scratchpad/inspect_*.py`) that navigates the
real form and dumps inputs/selects/labels/checkboxes/file-inputs/captcha. No LLM,
no DB, no submit. This is how you build accurate hints/adapters BEFORE writing code.

**Dry-run a fill without submitting:** run the executor with
`DRY_RUN_NO_SUBMIT=true` (the loop fills everything, stops before the real submit).

---

## 12. PER-ATS STATUS (quick reference)

| ATS | Status |
|---|---|
| Lever | ✅ Real SUBMITTED proven (hCaptcha auto-solved). |
| Greenhouse / Ashby | ✅ Full fill proven; standard forms. |
| CareerPlug / Careers-Page (Manatal) | Adapters + hints built & DOM-verified; real-submit not yet re-confirmed. |
| TeamTailor (careers.westerncomputer.com et al.) | ❌ Fills 100% but submit is walled by realtime anti-automation (see §9). Fails fast now. |
| Dice / Glassdoor / LinkedIn / iCIMS / Workday / ZipRecruiter / BuiltIn | Account-walled — need candidate `gmail`+`password` in the DB; manual login only. |
| RemoteRocketship / Remote100K / Himalayas / Talent | Aggregators — resolve the inner ATS and delegate. |

---

## 13. ONE-PARAGRAPH STATUS FOR A HUMAN

The engine fills forms correctly and solves captchas; the systemic bugs are fixed
and it lands real SUBMITTED applications on standard ATSes (proven on Lever this
session). Infra is on the Supabase transaction pooler + Upstash with fail-open
resilience, Gemini is the primary LLM, login is manual-email-password-only across
every adapter, and portal memory now learns from its own mistakes. The remaining
work is the finite per-ATS long tail plus two known items: TeamTailor's realtime
anti-automation submit wall (genuinely hard, currently fails fast rather than
looping) and a deterministic-replay layer for maximum repeat-apply speed. Build
adapters OFFLINE, verify against the real DOM, never assume, keep
`USE_AGENT_LOOP=true`, and keep Gemini funded.
```
