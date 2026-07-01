# Browser Automation Module — Engineering Handoff

**Scope of this document**: the `backend/app/browser_automation/` module — the AI-driven system that
auto-fills and submits real job applications on Greenhouse, Ashby, and (in progress) Lever. This
covers one continuous engineering session: architecture review, then platform-by-platform
hardening to production-grade, validated with **real submissions**, not dry runs.

**Author's stance throughout**: every "it works" claim in this doc is backed by an actual
`--submit` run against a live job posting, not a code read-through. Where something is *not* yet
validated, it's called out explicitly below — don't take it on faith.

---

## 1. The goal

Make the AI form-filling pipeline (`AgentLoop` in `agent/loop.py`) reliably submit real
applications on each supported ATS platform, one platform at a time, without regressing the
platforms already proven. The working method has been: pick a platform, run a real job URL with
`--submit`, read the failure, find the root cause by inspecting the *live* DOM (not guessing),
fix it narrowly, re-run, repeat until a real submission succeeds — then move to the next platform.

**Hard constraint carried across the whole session**: once a platform is validated working, its
own adapter file (`adapters/greenhouse.py`, `adapters/ashby.py`) does not get touched again.
Shared infrastructure (`agent/loop.py`, `forms/`, `captcha/`, `services/executor.py`) can still
change, but only additively / behind platform checks, so a fix for platform B can never silently
break platform A. This is why `agent/loop.py` has accumulated most of the diffs — it's the shared
brain all adapters go through.

---

## 2. How to test (the one command that matters)

```bash
cd backend
python ../test_m4_ai_first_real.py --submit <job_url>
```

- `--submit` = **real submission** to a live job posting, under candidate **Sabih Haider**'s
  identity (`sabih0364@gmail.com`, Gmail connected for OTP verification). Omit `--submit` for a
  dry run that stops right before the final click.
- Platform is auto-detected from the URL's host — same script for every ATS.
- Live logs stream to the terminal; that's the primary debugging signal. Key lines to grep for:
  `RESULT:`, `PRE-SUBMIT GATE`, `SUBMITTED`, `STUCK`, `spam`, `WARNING`.
- Before spending a real submission on a hypothesis, verify it against the live DOM directly with
  a small throwaway Playwright script (pattern used throughout this session — see any of the
  `diag_*.py` investigations in the conversation history). It's much cheaper than a full pipeline
  run and doesn't risk a duplicate/flagged application on a real employer's system.

**Known external dependency**: the whole pipeline needs Supabase (Postgres) reachable — candidate
lookup happens before the browser ever opens. If a run fails immediately with a SQLAlchemy
`TimeoutError`, that's Supabase being down, not a code bug. Confirm with:

```bash
python -c "
import asyncio
from app.database import AsyncSessionLocal
from sqlalchemy import text
async def m():
    async with AsyncSessionLocal() as s:
        print(await (await s.execute(text('SELECT 1'))).scalar())
asyncio.run(m())
"
```

---

## 3. Platform status

| Platform   | Status | Real submissions | Notes |
|---|---|---|---|
| **Greenhouse** | ✅ Production-ready | 3/3 successful (Vercel, Reddit ×2) | No known open issues |
| **Ashby** | ✅ Working, minor caveat | 2/3 successful (Suno, Nooks); 1 correctly rejected as spam (Farsyt) | Anti-bot spam-flag is real and external; see §5 |
| **Lever** | 🟡 In progress, not yet validated | 0 confirmed successful | Root cause found and fixed; **needs a fresh live validation run** — the last attempt was interrupted by a Supabase outage before the fix could be observed end-to-end |

---

## 4. Greenhouse — what was fixed

All changes live in `agent/loop.py` (shared, platform-agnostic) — `adapters/greenhouse.py` was
never touched.

1. **Scroll-to-bottom bug**: the AI's `scroll` action moved a fixed 600px regardless of page
   length. On long Greenhouse forms (full job description + EEO/demographic block can run several
   thousand px), 5 fixed-size scrolls (the STUCK-guard cap) weren't enough to reach the bottom, so
   the AI got aborted before ever seeing the Submit button. Fixed to compute the real remaining
   distance to `document.body.scrollHeight` and travel most of it in one motion.

2. **Combobox reasoning gap (the "answers a state for a Yes/No dropdown" bug)**: when the AI
   proposed an invalid dropdown value, the runner discovered the *real* options while trying to
   click it, but never told the AI what they were — so it kept repeating the same wrong guess
   until STUCK. Fixed by surfacing the real options in the failed action's `reason`, which the
   history formatter now shows on the next turn. Also added an explicit prompt rule (12dd) for
   "do you live in `<specific list>`" style questions: cross-reference the candidate's actual
   location against the list rather than guessing.

3. **Resume-grounded answers were dead code**: `agent/loop.py` already had a fully-built prompt
   block ("RESUME CONTENT — source of truth...") that would inject the candidate's actual resume
   text for education/employer/skills questions — but nothing ever populated
   `profile["_resume_text"]`. Wired `resume_enricher.py` to populate it. Also found the resume
   text budget was accidentally capped at 1800 chars for Gemini (the actual primary provider),
   the same tight limit meant only for Groq's 30k TPM ceiling — raised Gemini's budget to 4500.
   Added a parallel prompt rule (12f) for open-ended essay questions: answer with something
   concrete from the resume + the job posting, not generic filler.

4. **Screenshot upload RLS failure**: `services/screenshot.py` was authenticating with
   `SUPABASE_ANON_KEY` for a server-side-only upload, which the bucket's RLS policy correctly
   rejected. Switched to `SUPABASE_SERVICE_ROLE_KEY` (the correct credential for a trusted
   backend write) — did **not** loosen the RLS policy itself, which would have been a real
   security regression.

---

## 5. Ashby — what was fixed

Also entirely in `agent/loop.py`, plus `adapters/ashby.py` (isolated to Ashby's own code, per the
constraint above) and a small addition to `adapters/hints.py`'s Ashby section.

1. **URL construction bug**: appending `/application` to a URL that already has a query string
   (e.g. Ashby's own `www.ashbyhq.com/careers?ashby_jid=...`) landed the suffix *inside* the query
   value, corrupting the job ID and producing a "Job not found" page. Fixed with proper
   URL-aware construction, scoped to only rewrite on the canonical `jobs.ashbyhq.com` host.

2. **Content-hydration race**: some Ashby-hosted pages fetch job details asynchronously after the
   shell renders; a fixed short wait sometimes observed the page before real content loaded.
   Added a poll that waits for real job content (an "Apply" mention + non-trivial text length)
   before proceeding, capped at 15s, exits early once satisfied.

3. **Checkbox-group detection (the "AI never fills the radio buttons" bug reported directly by
   the user)**: Ashby's "select all that apply" questions render as N separate
   `<input type="checkbox">`, each with the *option text* as its own unique `name` — no shared
   group name to dedupe by like radios. The required-field gate never counted them because it was
   only checking `el.required`/`aria-required` on the individual input, which Ashby never sets.
   Fixed with fieldset-based grouping + "at least one checked" semantics.

4. **The real required-marker was never being checked, anywhere**: Ashby marks a question
   required with a `_required_...`-named CSS class on the **question label**, which is a *sibling*
   of the input, not an ancestor — so `el.closest('[class*="required"]')` (walking the input's own
   ancestor chain) could never find it, for *any* field type. The one container that reliably
   wraps every Ashby question — native input, combobox, standalone checkbox, or a fieldset-grouped
   set — is `[data-field-path]`. Fixed by searching *within* that container instead of walking up
   from the input. This single fix, plus the ones below, is what actually made the gate correct.

5. **Combobox fields with no `id` were silently dropped**: Ashby's "Location" autocomplete field
   has no `id` and no `name` at all — the combobox-detection code did
   `el.id ? '#'+el.id : null` and bailed on `null`, so the field never even appeared to the AI.
   Fixed with a `[data-field-path]`-scoped selector fallback
   (`[data-field-path="X"] [role="combobox"]`).

6. **Yes/No toggle widget — `.checked` is not reliable**: Ashby's Yes/No questions render as a
   *hidden* `<input type="checkbox">` paired with two visible `<button>Yes</button>`/`<button>No
   </button>` elements. Confirmed live: clicking "Yes" sets `checked=true`, but clicking "No"
   **never sets it at all** — `checked=false` is then ambiguous between "unanswered" and "answered
   No". This caused the AI to loop forever re-clicking "No" because nothing it read ever
   registered progress. Fixed by reading the `active`-named CSS class Ashby applies to
   whichever button was actually clicked, instead of the checkbox's `.checked` property. Also
   surfaced the real, clickable Yes/No buttons to the AI (with proper question-text prefixing and
   a `[data-field-path]`-scoped selector) since they were previously invisible to it.

7. **Post-submit false positive ("submitted" when it wasn't)**: AgentLoop's own success detection
   only checked "is there an OTP screen?" — if not, after 2 confirmation turns it declared
   `SUBMITTED` unconditionally, **without ever reading the page content**. Ashby (and per an
   existing code comment, Workday/Lever too) can render a rejection banner
   ("flagged as possible spam", "already applied") as a normal 200 OK page. Fixed by checking page
   content for known rejection patterns before declaring success — routes through the existing
   `SPAM_FLAGGED`/`ALREADY_APPLIED` terminal-failure handling instead of lying about success.
   **This is now validated working** — a later Ashby run correctly reported `ABORTED` /
   `SPAM_FLAGGED` instead of a false `SUBMITTED`.

8. **Timing / anti-bot pacing**: the whole pre-fill phase (resume upload, name, email, LinkedIn)
   was committing 4 fields within ~200ms of each other — a strong bot signal. Added human-like
   pacing (250–700ms randomized) between every pre-fill commit, not just before the final submit.
   The existing `ASHBY_PRE_SUBMIT_DWELL_MS` dwell (4s) only fired on the rarely-used deterministic
   fallback path — wired the same dwell into AgentLoop's own submit path (the actual common case),
   and jittered it (±30%) instead of a suspiciously-exact flat constant.

**Known open item**: Ashby's own anti-bot can still flag a submission as spam even with all of the
above — this is Ashby's server-side risk scoring, not something fully controllable from our side.
The system now *detects and reports it honestly* rather than lying about success, which is the
correct, achievable bar here — not "never gets flagged."

---

## 6. Lever — in progress

**Status: root cause identified and a fix implemented, but not yet confirmed by a live successful
run.** The last validation attempt landed mid-investigation and was interrupted by a Supabase
outage. This section is the most important one for whoever picks this up next.

### 6a. False "file exceeds maximum upload size of 100MB" error (partially understood)

The candidate's resume is 4.4KB — nowhere near the limit. Traced this precisely by fetching
Lever's own `js/parseResume.js` source: Lever uploads the resume async to `/parseResume` for
server-side parsing (auto-fills name/email/phone/location from it), and the oversize banner shows
either for a genuinely large file, **or when the server responds `400 PayloadTooLargeError`** to
that POST. Their own code also `req.abort()`s any in-flight parse request the instant a new file
gets set on the same input.

Fix applied:
- After the *original* resume upload, wait for the async parse request to genuinely settle
  (network-idle, capped at 6s) before letting anything else touch the page.
- If the oversize banner is later detected as a blocking validation error, re-upload the resume
  **once only** (not on every retry — repeated re-uploads were racing/aborting each other per
  Lever's own logic, likely making it worse) and wait properly (network-idle, capped at 8s) for
  that retry to settle.

This part was validated to *fire correctly* (the log showed the recovery triggering and waiting),
but the error kept recurring across 3 attempts in the run where this was tested — **before** the
captcha discovery below. It's possible this error is itself a downstream symptom of the captcha
issue (Lever's client-side validation may be tangled up with the same submit-click handler that
triggers hCaptcha) rather than a fully independent bug. Needs re-observation now that the captcha
fix is also in place.

### 6b. The real blocker: invisible hCaptcha on submit (found via direct user testing)

The user manually tested the same Lever job and observed a captcha appearing on Submit — this
led directly to the real root cause. Confirmed by reading Lever's page source directly:

- Lever integrates **invisible hCaptcha** (sitekey confirmed live:
  `e33f87f8-88ec-4e1a-9a13-df9bbb1d8120`) that starts executing (`hcaptcha.execute(captchaId)`)
  the instant the visible `#btn-submit` button is clicked.
- The button the AI clicks is **not** the real submitting element. Lever renders a second,
  genuinely hidden `<button id="hcaptchaSubmitBtn" type="submit" class="hidden">` that only gets
  auto-clicked by hCaptcha's own success callback (`onSuccess`) once a valid token exists.
- Confirmed live: the hidden `#hcaptchaResponseInput` field never gets a value for our automated
  session — it does not resolve invisibly the way it would for a normal user, even after 8+
  seconds of waiting.
- `onSuccess` is scoped inside a closure (`$(function(){...})`), not reachable as
  `window.onSuccess` — so even a correctly solved and injected token would **not** automatically
  trigger the real submission the way it does for a genuine hCaptcha widget interaction.

Fix implemented in `agent/loop.py` (scoped to `"lever" in platform`, no changes to
`adapters/lever.py` or any other platform):

1. After the AI clicks Lever's submit button, wait briefly (2s) for Lever's own invisible
   resolution to have a chance.
2. If `#hcaptchaResponseInput` is still empty, call the existing `CaptchaService.solve(page,
   "hcaptcha")` — reuses the already-configured CapSolver integration and sitekey
   auto-extraction, no new captcha-solving infrastructure needed.
3. Inject the solved token into `#hcaptchaResponseInput` directly (with a `change` event
   dispatch).
4. Since Lever's own auto-click-through can't be reached, directly click the hidden
   `#hcaptchaSubmitBtn` ourselves (`force=True` to bypass the visibility check — it's
   *deliberately* hidden via `class="hidden"`, not actionable through normal means).

**This has not yet been observed working end-to-end.** Next step is simply: re-run the same job
URL with `--submit` and confirm (a) the hCaptcha solve fires and succeeds, (b) the hidden button
click actually completes the real submission, (c) whether the file-oversize error still recurs
now that the captcha race is handled.

### 6c. Possible second layer: Cloudflare Turnstile

The last (interrupted) run logged `"Cloudflare Turnstile widget detected — settling (managed-token
wait → CapSolver fallback)"` before being killed — this is *existing*, pre-session code (not
something added this session), suggesting Lever may also sit behind a page-level Cloudflare
Turnstile check, separate from the form-level hCaptcha. Whether this resolves cleanly on its own
or needs further attention is **unverified** — watch for it in the next run's logs.

### 6d. Test URLs on file for Lever

- `https://jobs.lever.co/gohighlevel/1adc3097-fe4c-4be2-b893-e6bf2f78d523` (primary test target,
  all investigation above is against this one)
- `https://jobs.lever.co/gohighlevel/bf5d3445-59fd-44d8-b129-5801be177c52` (given but not yet
  tested — use only after the first URL gets a clean success, to check the fix generalizes, same
  pattern used for Ashby)

---

## 7. Files touched this session

```
backend/app/browser_automation/adapters/ashby.py       — Ashby-only: URL fix, content-hydration wait,
                                                            submit-disabled check, ALREADY_APPLIED detection
backend/app/browser_automation/adapters/hints.py        — Ashby section only: quirk documentation
backend/app/browser_automation/agent/loop.py            — shared: all scroll/combobox/resume-grounding/
                                                            Ashby-field-detection/Lever-captcha fixes
backend/app/browser_automation/captcha/service.py       — image-captcha key-selection bug fix
backend/app/browser_automation/services/executor.py     — wired resume_enricher into the pipeline
backend/app/browser_automation/services/resume_enricher.py — extended to populate _resume_text
backend/app/browser_automation/services/screenshot.py   — RLS fix (service-role key)
backend/app/tasks/browser_automation.py                 — ALREADY_APPLIED terminal-failure routing
backend/.env                                             — CAPTCHA_PROVIDER: ocilar → capsolver
```

`adapters/greenhouse.py` and `adapters/lever.py`: **not touched**. All Lever-specific logic lives
in `agent/loop.py` behind `"lever" in platform` checks — if Lever work continues, consider whether
some of it belongs in `adapters/lever.py` instead for cleanliness, but there was no functional
need to move it yet.

---

## 8. Operating principles that got this far (worth keeping)

- **Never trust a diagnosis without live DOM evidence.** Every fix in this doc came from actually
  inspecting the real page (`page.evaluate(...)`, response bodies, computed styles), not from
  reading adapter code and guessing. Several first guesses were wrong and got corrected by direct
  inspection (e.g. the Ashby "already applied" pattern collision check, the Lever oversize error's
  real trigger).
- **Verify a fix against the live DOM before spending a real submission on it.** Cheaper, faster,
  and doesn't risk a duplicate or anti-bot-flagged application on a real employer's system.
- **Scope fixes narrowly.** Platform-specific behavior goes behind a platform check in shared
  code, or in that platform's own adapter file — never a blanket change that could silently affect
  an already-validated platform.
- **Report failures honestly, including your own tool's false positives.** The Ashby
  false-`SUBMITTED` bug is the clearest example: the system was lying about success, and the fix
  was to make it *fail correctly* and *say so*, not to chase a 100% success rate that doesn't
  exist against real anti-bot systems.
- **When a user reports something you didn't see in the logs, take it as ground truth and go
  find it — don't defend the existing diagnosis.** The Lever captcha discovery only happened
  because the user tested manually and pushed back on the file-size explanation. That was the
  right call — the file-size fix was necessary but incomplete.
