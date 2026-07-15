# Browser Automation (Module 4) — Architecture Report & Refactoring Plan

> Status: **Analysis only — no code changed.** Produced from a full read of
> `backend/app/browser_automation/`. All references are `file:line` against the
> tree at the time of writing.

---

## 1. Executive summary

Module 4 is a **dual-brain** browser automation engine. Every application run
tries an autonomous **vision-driven AI loop** (`agent/loop.py`, ~8,100 lines)
first, and falls back to a **deterministic scripted pipeline** (per-platform
adapters + `forms/` detector/filler) only when the loop declines or fails in a
DOM-safe way. Orchestration, pre-flight gating, captcha handling, and status
persistence all live in one 1,840-line method: `ApplicationExecutor.execute`
(`services/executor.py:458`).

The system *works* across many ATSes, but its intelligence is **spread across
three uncoordinated layers**:

1. **A 200+ line hard-coded system prompt** (`loop.py:222`–`~700`) that encodes
   candidate policy, demographic answers, ATS-specific quirks, and workflow
   ordering as English rules the LLM is asked to follow.
2. **Deterministic Python/JS reflexes** inside the loop (`_deterministic_prefill`,
   `_deterministic_file_upload`, `_settle_turnstile`, `_dismiss_overlays`,
   memory prefill) that fill/enforce the *same* policies the prompt describes —
   sometimes racing the AI on the same widgets.
3. **20 platform adapters** that each re-implement navigate → detect → fill →
   submit → verify with their own selector lists, login flows, and success
   patterns — but which are now **mostly bypassed** because the AgentLoop runs
   first and, on success, skips steps 6–10 entirely.

The central architectural problem: **the same decision is encoded in three
places, and the boundary between "AI decides" and "code decides" is drawn
differently in every module.** Browser *state* (what actually happened after an
action) is frequently inferred from `sleep()` + text-pattern scraping rather
than observed from the page, and many "decisions" are static English or
regex that cannot adapt.

---

## 2. Component inventory

| Layer | File | Lines | Role |
|---|---|---:|---|
| **Orchestration** | `services/executor.py` | 1,839 | The entry point. Pre-flight gates, browser lifecycle, runs AgentLoop then scripted fallback, captcha, status transitions. |
| **AI loop** | `agent/loop.py` | 8,132 | Perception→decision→action loop, system-prompt assembly, deterministic reflexes, OTP/verification, turnstile, memory prefill. |
| **Vision classifier** | `agent/page_agent.py` | 270 | One-shot Gemini page classification (FORM/LISTING/SUCCESS/BLOCKED/…) + selector suggestion. |
| **Failure learning** | `agent/failure_diagnoser.py` | 194 | On fill/submit miss, asks LLM to diagnose and writes to learned_fixes. |
| **Selector cache** | `agent/learned_fixes.py` | 138 | Per-ATS JSON of winning selectors, tried before hardcoded lists. |
| **Portal playbook** | `agent/portal_memory.py` | 296 | Per-host self-authored playbook (flow, working selectors, success signal). |
| **Adapters (20)** | `adapters/*.py` | ~6,500 | Per-ATS navigate/detect/fill/submit/verify + login/captcha where needed. |
| **Adapter routing** | `adapters/registry.py` | 99 | Maps platform slug / URL substring → adapter class. |
| **Static hints** | `adapters/hints.py` | 1,049 | Per-platform cheat sheet (selectors, quirks, success patterns) injected into the AI prompt. |
| **Deterministic forms** | `forms/detector.py` | 661 | DOM → `DetectedForm` (fields, types, required, captcha). |
| | `forms/filler.py` | 953 | Deterministic field filling. |
| | `forms/llm_filler.py` | 841 | Gemini-driven field filling (default path when `USE_LLM_FILLER=true`). |
| | `forms/memory.py` | 377 | Per-candidate field-answer memory. |
| **Browser** | `browser/context_manager.py` | 574 | Playwright context lifecycle, session persistence, stealth. |
| | `browser/stealth_config.py` | 331 | Anti-detection config. |
| | `browser/flaresolverr.py` | 105 | Cloudflare bypass helper. |
| **Captcha** | `captcha/service.py` | 1,560 | Provider dispatch (capsolver/2captcha/anticaptcha/nopecha), solve flows. |
| **Verification** | `verification/code_fetcher.py` | 912 | Gmail OTP/verification-code retrieval. |
| **Services** | `services/platform_review.py` | 330 | Circuit breaker + spam-backoff per platform. |
| | `services/state_machine.py` | — | Status PATCH to M1 API. |
| | `services/resume_enricher.py` | 332 | Backfill profile from resume PDF. |

---

## 3. Current architecture diagram

```
 Celery task:execute_application (backend/app/tasks/browser_automation.py)
        │  ApplicationPackage {candidate, job_url, platform, resume_url, ...}
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ ApplicationExecutor.execute()               services/executor.py:458      │
│                                                                           │
│  PRE-FLIGHT GATES (all before Chrome launches)                            │
│   1 idempotency guard (already-submitted?)         :515                   │
│   2 platform circuit breaker + spam backoff        :541                   │
│   3 resolve resume/cover PDFs to local cache       :580                   │
│   4 resume-enrich profile                          :598                   │
│   5 is_job_url_active() httpx pre-flight           :618                   │
│   6 robots.txt gate (DISABLE_ROBOTS_CHECK)         :628                   │
│   7 account-walled ATS creds check                 :656                   │
│   8 rate limiter (Redis)                            :714                   │
│                                                                           │
│  get_adapter(platform) ──► registry.py (slug/URL substring match)         │
│  BrowserContextManager.get_context() + playwright-stealth                 │
│  adapter.navigate_to_application(page, url)         :778                   │
│         (aggregators scrape inner ATS URL here; set _resolved_url/_inner) │
│                                                                           │
│  ┌── STEP 5.5  PageAgent.classify_page() (Gemini)   :796 ───────────────┐ │
│  │   BLOCKED → captcha detect + CaptchaService.solve                    │ │
│  │   LISTING → click Apply (suggested + learned selectors)              │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                           │
│  ┌── STEP 5.7  AgentLoop.run(page, frame)  ◄══ PRIMARY BRAIN  :953 ─────┐ │
│  │   perceive → LLM decides ONE action → execute → repeat               │ │
│  │   returns SUBMITTED / ABORTED / WRONG_PAGE / MAX_STEPS / STUCK /      │ │
│  │           LLM_UNAVAILABLE / VERIFICATION_FAILED                        │ │
│  │   records portal_memory + spam-backoff outcome                        │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│        │ SUBMITTED           │ terminal (abort/verify) │ non-terminal      │
│        ▼                     ▼                          ▼                  │
│   fast path             raise (no fallback)      SCRIPTED FALLBACK          │
│   (skip 6-10)                                    (only if DOM untouched     │
│                                                   OR LLM never ran)         │
│                                                                           │
│  SCRIPTED PIPELINE (steps 6-10)  — mutually exclusive with AgentLoop      │
│   6  detect_form(ctx)          forms/detector.py     :1209                 │
│   6.5 M3 prepare-package for screening answers       :1233                 │
│   7  fill_form_with_llm() → adapter.fill_application  :1299                │
│   8  captcha (form.has_captcha)                      :1422                 │
│   9  transition FORM_COMPLETED                        :1495                │
│   10 adapter.submit() → (vision fallback) → verify_success :1498          │
│                                                                           │
│  STEP 11-13  screenshot → transition SUBMITTED/FORM_COMPLETED → teardown  │
└─────────────────────────────────────────────────────────────────────────┘
        │
        ▼  ApplicationResult {status, screenshot_url, confirmation_text, ...}

  Cross-cutting memory/learning (read at prompt-build, written after run):
   • hints.py           static per-ATS cheat sheet     (read)
   • learned_fixes/*    per-ATS winning selectors       (read+write)
   • portals/<host>     per-host playbook               (read+write)
   • forms/memory       per-candidate field answers     (read+write)
```

### 3.1 The AgentLoop internals (perception → decision → action)

```
AgentLoop.run()  loop.py:5361
  pre-loop: JS cookie/modal dismissal (:5461) · SPA hydrate wait (:5537)
  FOR step in 1..max_steps:
     ├ wall-clock budget check (240s + 330s post-submit grace)   :5597
     ├ multi-tab/popup switch                                     :5620
     ├ frame transition settle                                    :5682
     ├ _maybe_reclassify_platform (URL→ATS, swap hints)           :5700
     ├ IF submit_fired → POST-SUBMIT VERIFICATION PHASE (no LLM)  :5717
     │     detect_code_input → fetch Gmail code → fill → verify
     │     else 2 quiet turns → rejection scan → visual classify → SUBMITTED
     ├ MID-FLOW OTP phase (split code widget)                     :5989
     ├ _settle_turnstile (deterministic)                          :6059
     ├ _dismiss_overlays reflex                                   :6069
     ├ DETERMINISTIC REFLEXES (no LLM):                           :6107
     │     _deterministic_file_upload · _deterministic_prefill
     │     _try_memory_prefill
     ├ PERCEIVE: _dom_snapshot (JS) + _dom_hash + screenshot      :6166
     │     (screenshot skipped if DOM unchanged after mutation)
     ├ STUCK detector (hash unchanged) → one-shot stall recovery  :6212
     ├ DECIDE: llm.generate_json(system_prompt, user_turn, image) :6296
     ├ _parse_action                                              :6323
     ├ pre-submit completeness gate (scroll+required scan, cap 3) :6335
     └ _execute_action(...)                                       :1841
```

---

## 4. Execution flow — Generic Adapter trace

`GenericFormAdapter` (`adapters/generic.py`) is the fallback for any unmatched
platform. **Important:** in the live pipeline the adapter's own methods only run
if the AgentLoop declines/fails DOM-safely — otherwise the AI drives everything
and only `navigate_to_application` runs.

1. **`navigate_to_application`** (`generic.py:77`)
   - `page.goto(url, domcontentloaded, 25s)`, then a human delay.
   - If any `input/textarea/select` is attached within 2.5s → assume form present, return.
   - Else iterate `learned_fixes("generic").apply_button` + a static
     `_APPLY_SELECTORS` list; click first visible; wait for load; **learn** the
     winning selector. **No verification the click revealed a form** — the next
     stage (PageAgent/AgentLoop) re-observes.

2. **`detect_application_type`** (`generic.py:104`) → `detect_form().form_type`.

3. **`fill_application`** (`generic.py:108`) — scripted fallback only:
   - `detect_form()` → `fill_form()` (deterministic).
   - Then a **label-substring heuristic** routes uploads: `"cover" in label` →
     cover letter; `"resume"/"cv"` or "no cover/other keyword" → resume
     (`generic.py:120`). This is a pure string guess with no confirmation.

4. **`submit`** (`generic.py:161`) — three escalating attempts:
   - (a) learned + static `_SUBMIT_SELECTORS` across page **and every child
     frame** (`_search_contexts`), clicking first visible+enabled;
   - (b) scroll to bottom, retry;
   - (c) bounded 4× "advance multi-step via Next/Continue" then retry submit.
   - Learns the winning submit selector. **Success = "a click happened"**, not
     "the server accepted".

5. **`verify_success`** (`generic.py:215`) — `page.content()` substring match
   against 8 `_SUCCESS_PATTERNS`. Deliberately excludes bare "thank you" to
   avoid false positives — but is still **pure text scraping of the DOM**, blind
   to HTTP status, network responses, or a rejection banner.

**Generic-adapter assumption chain:** presence of inputs = form ready · a
visible Apply/Submit click = progress · label substring = file type · success
text in DOM = submitted. None are confirmed against actual browser/network state.

---

## 5. Execution flow — every other adapter (by archetype)

All adapters implement the same 5-method `BasePlatformAdapter` contract
(`adapters/base.py:56`). They cluster into four archetypes:

### A. Plain-form adapters — `lever`, `greenhouse`, `ashby`, `himalayas`, `smartrecruiters`, `jobvite`
- **Lever** (`lever.py`): appends `/apply` to the URL to skip the Apply click
  (`:57`); `container="#application-form"`; `detect_form`+`fill_form`; upload by
  label substring; submit via static selector list; verify via text patterns.
  This is the template the others follow.
- **Greenhouse** (`greenhouse.py:630`): richest scripted adapter — canonical-URL
  rewrite, domain warm-up, iframe handling (`#grnhse_iframe`), a dedicated
  `_upload_greenhouse_files` (hidden file inputs → filename-display confirmation).
- **Ashby** (`ashby.py`): full-page React form, no stable container; required
  detection keys on `[data-field-path]` + `_required_*` module classes (the
  quirk hard-coded in the DOM-snapshot JS and prompt).
- **SmartRecruiters/Jobvite**: multi-step "oneclick-ui"; rely on the loop's or
  generic's Next/Continue progression.

### B. Login-gated ATS — `dice`, `workday`, `icims`, `ziprecruiter`, `glassdoor`, `builtin`
- Add `_ensure_logged_in`/`_perform_login`/`_wait_logged_in` plus captcha
  helpers (`_maybe_solve_captcha`, `_maybe_solve_turnstile`, `_maybe_solve_hcaptcha`).
- Credentials injected via `set_candidate_credentials` (`base.py:12`) — kept off
  `candidate_profile` so the password never reaches the LLM.
- The executor **pre-flight-skips** these entirely if no creds are configured
  (`executor.py:656`) → `LOGIN_REQUIRED`.
- **Dice** (`dice.py:946`): the heaviest — login, wizard prep, easy-apply vs
  external-apply branch, chooser-based upload, resume replacement (account
  default resume swapped for the tailored file), settle-after-submit.
- **BuiltIn** (`builtin.py`): passwordless magic-link login — clicks through to
  the underlying ATS, reads the login link from Gmail via the verification layer.

### C. Aggregator / passthrough — `remoterocketship` (also `remoteok`/`adzuna`/`hiringcafe`/`remote100k`), `talent`, `indeed`
- **RemoteRocketship** (`remoterocketship.py`): `_scrape_apply_url` extracts the
  real ATS link off the listing, navigates to it, and sets `_resolved_url` /
  `_inner` (an inner adapter created via `_spawn_delegate`, `base.py:22`, which
  propagates credentials). The executor then uses the **inner** platform's hints
  and charges failures to the inner ATS (`executor.py:975`, `:1696`).
- **Talent** (`talent.py:645`): deterministic pre-form gate — fills email,
  clicks Continue to trigger an emailed OTP, handles WAF/reCAPTCHA interstitials,
  then hands to the loop; the loop's mid-flow OTP phase fetches the code.
- **Indeed** (`indeed.py`): `_classify_branch` + `_follow_external_apply` to
  leave Indeed's SmartApply for the employer ATS; login-gate detection.

### D. Modal Easy-Apply — `linkedin`, `glassdoor`, `ziprecruiter`
- Container-scoped to a modal (`.jobs-easy-apply-modal`, `.modal-content`);
  step-through Next buttons; `_click_one`/`_click_first` helper patterns.

**Common structural traits across all adapters:**
- Every adapter carries its **own** static `_APPLY_SELECTORS` / `_SUBMIT_SELECTORS`
  / `_SUCCESS_PATTERNS` lists that duplicate `hints.py` and each other.
- Every adapter's `verify_success` is **DOM text-scraping**; none consult HTTP
  status or network responses.
- Login/captcha logic is copy-pasted with per-site tweaks (6 near-identical
  `_perform_login`/`_wait_logged_in` implementations).
- Helpers like `_fill_first`, `_click_first`, `_wait_visible`, `_dismiss_overlays`,
  `_safe_content` are re-implemented in nearly every gated adapter.

---

## 6. Decision flow diagram

```
                    ┌───────────────────────────┐
                    │  navigate_to_application   │
                    └────────────┬──────────────┘
                                 ▼
                    ┌───────────────────────────┐
                    │ PageAgent.classify_page    │  (Gemini, 1 shot)
                    └───┬─────────┬─────────┬────┘
             BLOCKED    │  LISTING│   FORM/ │ UNKNOWN
                        │         │  other  │
             ┌──────────▼──┐  ┌───▼─────┐   │
             │captcha solve│  │click    │   │
             │ or RAISE    │  │Apply    │   │
             └─────────────┘  └────┬────┘   │
                                   └────────┼──────────┐
                                            ▼          │
                          ┌─────────────────────────┐  │
                          │   AgentLoop.run()        │◄─┘   USE_AGENT_LOOP
                          │   (up to 60 steps)       │      (default true)
                          └───────────┬──────────────┘
        ┌───────────────┬─────────────┼───────────────┬───────────────┐
   SUBMITTED       submit_fired   ABORTED/WRONG    VERIFICATION    MAX_STEPS/
        │           but not conf.  _PAGE            _FAILED         STUCK/LLM_
        │               │             │                │           UNAVAILABLE/
        │               │             │                │           ERROR
        ▼               ▼             ▼                ▼               │
   fast path       record as      raise           raise BLOCKED    ┌──▼─────────┐
   verified=True   SUBMITTED      (terminal)       (terminal)      │DOM touched?│
                   (skip scripted  no fallback     no retry        └──┬─────┬───┘
                    fallback)                                    no  │     │ yes
                                                    (LLM never ran)  │     │
                                                                     ▼     ▼
                                                            SCRIPTED    RAISE
                                                            FALLBACK    "prevent
                                                            (steps 6-10) conflict"
                                                                        (fail app)
```

**Key decision points and where they live:**

| Decision | Location | Basis |
|---|---|---|
| Which adapter? | `registry.py:50` | Static slug map + URL substring `if/elif` chain |
| Is job still live? | `executor.py:93,187` | httpx status + redirect heuristic + expired-text list |
| AI or scripted? | `executor.py:961` | `USE_AGENT_LOOP` env + loop result status |
| Fall back after loop? | `executor.py:1146` | Was the DOM touched? (passive-action set) |
| Page state | `page_agent.py:142` | Single Gemini call, JPEG q50 + 5k DOM |
| Next action (per turn) | `loop.py:6296` | Gemini over system prompt + DOM snapshot + screenshot |
| Field value | system prompt rules + `_deterministic_prefill` + memory + `llm_filler` | Hard-coded policy, regex, memory, LLM — **4 sources** |
| Form complete? | `loop.py:1451`, `:6439` | Regex on "FORM STATUS: n/m" + required-field JS scan |
| Submit accepted? | `loop.py:5853`+, `verify_success` | Absence of OTP wall + text patterns + ≤2 visual classify |
| Retry / block / fail? | `executor.py:1682`+ | String matching on the error message |

---

## 7. Perception pipeline (as it exists today)

The system has **four parallel, uncoordinated perception mechanisms**:

1. **`_DOM_SNAPSHOT_JS`** (`loop.py:714`) — the primary AI perception. In-page JS
   that:
   - pierces open shadow roots (`deepQueryAll`, depth ≤12);
   - collects visible validation errors (`checkVisibility`) into an "errors" block;
   - filters out phone widgets, search bars, and cookie/consent controls via
     hard-coded selector lists (`inPhoneWidget`, `isSearchInput`, `inConsentBanner`);
   - resolves a human label per field (aria-label → label[for] → ancestor walk →
     placeholder/name);
   - infers `required` from `required`/`aria-required`/ancestor `[class*=required]`/
     `[data-field-path]` module class/`*` in label;
   - computes a `formStatus` (filled/total required) rendered as
     `FORM STATUS: n/m required filled … READY FOR SUBMIT`.
   - Capped at **250 fields / 10 errors**.
   Output is **text**, joined line-by-line, handed to the LLM.

2. **Screenshot** (`loop.py:6197`) — full-page JPEG, quality 28–35 by provider,
   **skipped** when the DOM hash is unchanged after a mutating action
   (token optimization). Vision + DOM text are the two model inputs.

3. **`_dom_hash`** (`loop.py:1419`) — SHA1 over input values + form/button/label
   text + node count (shadow-piercing). Sole basis for the **stuck detector**.

4. **`detect_form`** (`forms/detector.py`) — the *deterministic* pipeline's
   separate perception, producing a typed `DetectedForm`. Overlaps heavily with
   `_DOM_SNAPSHOT_JS` but shares no code with it.

Plus **`PageAgent.classify_page`** (a fifth: a separate Gemini call over its own
screenshot + `page.content()[:5000]`).

**What perception does NOT observe today:**
- HTTP response status of the navigation or the submit POST (network is only
  tapped in a narrow spot near `loop.py:7108`).
- Whether an XHR/fetch triggered by a click succeeded or failed.
- Client-side JS validation state beyond visible error text.
- Real element geometry / occlusion (relies on `getComputedStyle`/`checkVisibility`).
- Post-submit server truth — inferred from *absence* of an OTP screen + text
  patterns + a capped visual classification (`loop.py:5868`–`5977`).

---

## 8. Assumption points

### 8.1 Hard-coded decisions (values baked into prompt/code, not derived)

| # | Assumption | Location |
|---|---|---|
| H1 | Candidate **lives in the US and is US work-authorized** → "Yes" to US auth, "No" to sponsorship | `loop.py:363`, `:3158` |
| H2 | **Demographics are fixed**: Race = "South Asian (else Asian)", Orientation = "Heterosexual", Transgender/Disability/Veteran = "No" | `loop.py:407`–`489`, `:3166`; `hints.py:68` |
| H3 | **Gender inferred from first name** ("Harmain/Ahmed → Male", "Sarah → Female", ambiguous → Male) | `loop.py:411`, `:3165`, `_infer_gender` `:4049` |
| H4 | LinkedIn URL policy = "N/A" unless `ALLOW_REAL_LINKEDIN` | `loop.py:48`, `:3149` |
| H5 | Greenhouse/Vercel EEO block is **exactly 6 questions in a fixed order** with specific numeric IDs (`4015780004`…) | `loop.py:395`, `:481`; `hints.py:71` |
| H6 | Country answer is always **"United States"** for any country-list dropdown | `loop.py:445` |
| H7 | Per-platform **step budgets** (dice/icims/ziprecruiter/workday = 90) | `executor.py:71` |
| H8 | Per-platform **rate limits** (linkedin 10, indeed 20, …) | `executor.py:255` |
| H9 | **Wall-clock budgets** 240s + 330s post-submit grace; STUCK=4; LLM retries=3; submit cap=3 | `loop.py:111`–`152`, `:6408` |
| H10 | Static **success/expired text-pattern lists** per adapter and globally | `executor.py:165`, `generic.py:62`, every adapter |
| H11 | Static **apply/submit selector lists** duplicated in hints.py and each adapter | `hints.py`, all adapters |
| H12 | Hard-coded **cookie/consent/search/phone-widget filter selectors** | `loop.py:800`–`843`, `:1482`, `:5461` |
| H13 | Captcha provider default = anticaptcha; bot-walled host skip list | `executor.py:44`, `:1428` |

### 8.2 Workflow assumptions (the flow is assumed to be shaped a certain way)

| # | Assumption | Location |
|---|---|---|
| W1 | Forms are filled **strictly top-to-bottom, one field per turn** | `loop.py:270` |
| W2 | The flow is **forward-only** — `navigate_url` to a visited URL is rejected | `loop.py:5383`, `:5390` |
| W3 | **Fill fully, then submit once** — "READY FOR SUBMIT means stop and submit" | `loop.py:274` |
| W4 | After submit, the **only** remaining work is an email OTP screen | `loop.py:5573`, `:5717` |
| W5 | "No OTP screen after 2 quiet turns" ⇒ **submitted successfully** | `loop.py:5868` |
| W6 | AI and scripted pipeline are **mutually exclusive** on a touched DOM (else "conflict") | `loop.py:1146` |
| W7 | A fired submit without confirmation ⇒ **treat as SUBMITTED** (retry would double-apply) | `executor.py:1105` |
| W8 | Multi-step = "click Next/Continue up to N times" | `generic.py:196`, prompt rule 6 |
| W9 | OTP widget is a 4–10-box "split" input; that shape alone triggers OTP fetch | `loop.py:5997` |
| W10 | Aggregators resolve exactly one inner ATS; inner platform's hints apply | `executor.py:975` |
| W11 | Demographic questions must be answered even when not `required` (operator policy) | `loop.py:6510` |

### 8.3 Points where browser state is ignored / inferred instead of observed

| # | Gap | Location |
|---|---|---|
| S1 | **Submit success is inferred from DOM text / OTP absence**, never from the submit HTTP response | `loop.py:5853`+; every `verify_success` |
| S2 | **Click success = "a click happened"**, not "the expected state change occurred" | `generic.py:161`, adapter submits |
| S3 | **Navigation liveness via httpx pre-flight** (no cookies/JS) with a redirect heuristic — hence the large skip list of false-positive hosts | `executor.py:44`, `:93` |
| S4 | **Stuck detection = DOM hash unchanged** — a page doing async work with no DOM delta reads as stuck; a cosmetically-changing page reads as progress | `loop.py:1419`, `:6222` |
| S5 | **Field "filled" = `.value` non-empty**; react-select/controlled inputs need the end-of-fill re-scan hack, file inputs are exempted entirely | `executor.py:1340`, `loop.py:6555` |
| S6 | **Form readiness = regex on a self-generated text line**, not a real validity check | `loop.py:1451` |
| S7 | **Error classification = substring match on the exception string** (`"BLOCKED" in msg`, `"JOB_EXPIRED" in msg`) — brittle string protocol between layers | `executor.py:1682`–`1799` |
| S8 | **Deterministic reflexes race the AI** on the same widgets (country/demographic auto-fill was removed for exactly this reason — comment at `loop.py:6094`) | `loop.py:6107`–`6165` |
| S9 | **Screenshot skipped on unchanged DOM** — the AI can be blind on the turn a purely-visual change (spinner, toast, disabled button) matters | `loop.py:6177` |
| S10 | **Captcha/turnstile settled every turn deterministically** regardless of whether one is present | `loop.py:6059` |

---

## 9. Components requiring redesign

Ranked by leverage.

1. **`agent/loop.py` (8,132 lines) — the God-object.** One file holds the loop,
   prompt assembly, ~10 deterministic reflexes, OTP, turnstile, memory prefill,
   demographic autofill, and post-submit verification. It must be decomposed
   into: a thin **loop driver**, a **perception provider**, an **action
   executor**, a **policy/answer resolver**, and **phase handlers**
   (verification, captcha). No single change is safe here today.

2. **The triplicated decision layer.** Field policy lives in (a) the English
   system prompt, (b) `_deterministic_prefill`/memory, and (c) `llm_filler`.
   These must collapse into **one answer-resolution service** with a clear
   precedence, so the AI is asked only about genuinely novel questions and never
   races code on the same widget (S8).

3. **`ApplicationExecutor.execute` (1,839-line method).** Pre-flight, browser
   lifecycle, dual-brain orchestration, captcha, and status persistence are one
   try/finally. Extract a **pre-flight gate chain**, a **run strategy**
   (AI-first / scripted), and a **result/status mapper**. Replace the
   string-in-exception protocol (S7) with typed outcomes.

4. **Perception unification.** Merge `_DOM_SNAPSHOT_JS`, `detect_form`,
   `_dom_hash`, and `PageAgent` capture into **one perception module** that emits
   a single structured `PageObservation` (fields, errors, form-status, captcha,
   network/HTTP signal, screenshot) consumed by both brains. Eliminates 4-way
   drift and lets state be *observed* (S1–S6, S9).

5. **Adapter contract.** 20 adapters duplicate selectors, login, captcha,
   verify, and helper utilities. Redesign into: a **declarative platform
   profile** (selectors/quirks/success — much of `hints.py` already is this) +
   shared **capability mixins** (LoginCapable, CaptchaCapable, PassthroughCapable)
   so a new ATS is data + a mixin, not 500 lines. `verify_success` must move to
   the shared observed-state check.

6. **Outcome/verification model.** Replace "absence of OTP ⇒ success" and
   text-pattern verify with an **evidence-based verdict**: submit-response
   status + confirmation signal + rejection-banner scan + (bounded) visual
   confirmation, returning a typed confidence, not a boolean.

7. **`captcha/service.py` (1,560 lines)** and **`verification/code_fetcher.py`
   (912 lines)** — large but self-contained; lower priority. Redesign only their
   *interfaces* so the loop calls them through the phase-handler abstraction.

8. **Registry routing.** The slug-map + `if/elif` URL substring chain
   (`registry.py`) should become a data-driven matcher (host patterns on the
   platform profile).

---

## 10. Refactoring roadmap (later phases — not yet implemented)

> Sequenced so each phase is shippable, reversible, and keeps the pipeline green.
> Guardrails to preserve throughout: **never double-submit**, **never block on
> the LLM**, **early-ack task semantics**, and the **no-close-mid-run** rule.

### Phase 0 — Safety net (prerequisite)
- Capture a **golden-transcript harness**: for a fixed set of URLs per archetype
  (Greenhouse, Lever, Ashby, Workday, Dice, Talent, RR-passthrough, generic),
  record perception snapshots + action sequences under `DRY_RUN_NO_SUBMIT`.
- Add characterization tests asserting current behavior before touching it.
- Instrument the four perception mechanisms to log a comparable observation so
  drift is measurable.

### Phase 1 — Unify perception (`PageObservation`)
- Introduce one `perception/` module returning a typed `PageObservation`
  (fields, required-status, validation errors, form-status, captcha kind,
  screenshot handle, **navigation/submit HTTP status**, active frame).
- Back both the AI loop and `detect_form` with it (adapter shim first, no
  behavior change).
- Add **network observation**: subscribe to the submit request/response so
  submit-accepted becomes *observed* (addresses S1, and shrinks the httpx
  pre-flight skip list S3).

### Phase 2 — Single answer-resolution service
- Extract all field-answer logic (prompt policy H1–H6, memory, deterministic
  prefill, llm_filler) into `answers/resolve_field(observation, field, profile)`
  with explicit precedence: **memory → policy → M3 pre-answers → LLM**.
- Make policy **data-driven** (per-candidate config object), removing hard-coded
  demographics/US-centric assumptions from the prompt (H1–H3, H6, W11).
- Delete the racing deterministic autofill; the resolver owns each widget once (S8).

### Phase 3 — Decompose the loop
- Split `loop.py` into: `LoopDriver` (perceive→decide→act, budgets, stuck),
  `ActionExecutor` (`_execute_action`), `PhaseHandlers` (verification, OTP,
  turnstile, captcha), `PromptBuilder`. Each independently testable.
- Replace the stuck heuristic with **observation-delta + network-idle**, not
  DOM-hash alone (S4, S9).

### Phase 4 — Typed outcomes & executor slimming
- Replace string-in-exception classification with a `RunOutcome` enum + payload
  (S7). Executor maps outcome → status transition; no substring matching.
- Extract the pre-flight gate chain (idempotency, breaker, robots, creds, rate)
  into composable gates.
- Evidence-based submit verdict (Section 9.6) replaces "absence of OTP ⇒ success"
  (S5, W5, W7 become explicit, logged confidence levels).

### Phase 5 — Adapter model
- Convert `hints.py` + per-adapter static lists into one **platform profile**
  data structure (selectors, quirks, success patterns, host patterns, caps).
- Redesign adapters as **profile + capability mixins** (Login, Captcha,
  Passthrough, ModalEasyApply). Collapse the 6 near-identical login flows and the
  duplicated `_fill_first`/`_click_first`/`_wait_visible` helpers.
- Data-drive `registry.py` from profile host patterns.

### Phase 6 — Adaptive policy (stretch)
- With perception observed and answers centralized, make workflow assumptions
  (W1 top-to-bottom, W2 forward-only, W3 fill-then-submit, W8 Next/Continue)
  **strategies selected from observation**, not global constants — enabling
  non-linear forms, save-and-resume wizards, and conditional branches the current
  linear model can't express.

### Sequencing note
Phases 1→2→4 deliver the biggest correctness wins (observed state, one answer
source, typed outcomes) with contained blast radius. Phase 3 (loop
decomposition) is the riskiest and should follow the Phase-0 safety net. Phase 5
is mostly mechanical once Phase 1 lands. Phase 6 is optional and depends on 1–4.

---

## Appendix — canonical entry points for future work

- Task → `backend/app/tasks/browser_automation.py` (`task:execute_application`)
- Orchestrator → `services/executor.py:458` `ApplicationExecutor.execute`
- AI loop → `agent/loop.py:5361` `AgentLoop.run`; action exec `:1841`; prompt `:3126`
- Perception JS → `agent/loop.py:714` `_DOM_SNAPSHOT_JS`
- Deterministic forms → `forms/detector.py` / `forms/filler.py` / `forms/llm_filler.py`
- Routing → `adapters/registry.py:50`
- Static knowledge → `adapters/hints.py`
- Learning → `agent/learned_fixes.py`, `agent/portal_memory.py`, `forms/memory.py`

> **Reminder:** `module4/` at the repo root is a re-export shim — always edit
> `backend/app/browser_automation/`.
