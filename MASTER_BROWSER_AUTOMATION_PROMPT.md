# 🧠 MASTER PROMPT — Browser Automation Module
## Deep Analysis + Fix Blueprint + Platform Autonomy Roadmap
**File:** `backend/app/browser_automation/`  
**Created:** 2026-07-02  
**Scope:** All 14 platforms targeted, full browser identity upgrade, captcha handling, autonomous operation

---

## TABLE OF CONTENTS

1. [Deep Module Analysis](#1-deep-module-analysis)
2. [Bugs & Issues Found](#2-bugs--issues-found)
3. [Browser Identity — Current State & What to Fix](#3-browser-identity--current-state--what-to-fix)
4. [Making the Browser Fully Autonomous](#4-making-the-browser-fully-autonomous)
5. [Captcha Handling — Platform by Platform](#5-captcha-handling--platform-by-platform)
6. [Per-Portal Adapter Audit & Fix Plan](#6-per-portal-adapter-audit--fix-plan)
7. [Missing Adapters (New Platforms)](#7-missing-adapters-new-platforms)
8. [Implementation Checklist](#8-implementation-checklist)
9. [Engineering Prompts for Each Task](#9-engineering-prompts-for-each-task)

---

## 1. Deep Module Analysis

### 1.1 Architecture Overview

```
backend/app/browser_automation/
├── browser/
│   ├── context_manager.py     ← Playwright context factory (proxy, sessions)
│   └── stealth_config.py      ← Browser identity generation (per-candidate, seeded)
├── adapters/
│   ├── registry.py            ← Platform → Adapter routing
│   ├── hints.py               ← Per-platform LLM hint sheets (42KB)
│   ├── base.py                ← BasePlatformAdapter interface
│   ├── greenhouse.py          ← ✅ Most complete adapter (iframe, react-select, EEO)
│   ├── lever.py               ← ✅ Simple, clean, working
│   ├── ashby.py               ← ✅ Rejection detection, already-applied check
│   ├── dice.py                ← ✅ Full auth, Easy Apply wizard
│   ├── talent.py              ← ✅ OTP gate, Turnstile captcha aware
│   ├── remoterocketship.py    ← ✅ Passthrough resolver (delegates to inner ATS)
│   ├── indeed.py              ← ⚠️ External redirect only, no native fill
│   ├── workday.py             ← ⚠️ Requires account auth
│   ├── icims.py               ← ⚠️ Multi-step, account-walled
│   ├── linkedin.py            ← ⚠️ Login-walled, Easy Apply modal only
│   └── generic.py             ← 🆕 Fallback for unknown platforms
├── agent/
│   ├── loop.py                ← 6183-line autonomous vision-driven agent (CORE)
│   ├── page_agent.py          ← Page-state classifier (screenshot + DOM)
│   ├── failure_diagnoser.py   ← Post-failure LLM analysis
│   └── learned_fixes.py       ← Persistent per-platform selector memory
├── captcha/
│   ├── service.py             ← 627-line multi-provider captcha solver
│   ├── ai_solver.py           ← Claude/vision-first solver (free)
│   ├── audio_solver.py        ← Whisper-based audio challenge solver
│   └── models.py              ← CaptchaSolution dataclass
├── forms/
│   ├── detector.py            ← DOM field detection + classification
│   ├── filler.py              ← Deterministic field filling
│   ├── llm_filler.py          ← LLM-driven filler (Gemini-first)
│   ├── memory.py              ← Per-platform fill pattern memory
│   ├── ai_resolver.py         ← Ambiguous field value resolution
│   └── uploader.py            ← File input handler
├── services/
│   ├── executor.py            ← 1230-line main orchestrator (runs everything)
│   ├── resume_enricher.py     ← PDF → profile enrichment
│   ├── platform_review.py     ← Platform kill-switch (flag bad platforms)
│   ├── state_machine.py       ← Application status transitions
│   └── screenshot.py          ← Evidence capture
├── llm/
│   ├── claude_client.py       ← LLM client (Claude → OpenRouter → Gemini fallback)
│   └── telemetry.py           ← LLM usage tracking
├── verification/
│   └── code_fetcher.py        ← Gmail OTP auto-fetch
└── frame_utils.py             ← Cross-frame context helpers
```

### 1.2 Execution Flow (Full Pipeline)

```
Job URL arrives via Celery task
  ↓
ApplicationExecutor.execute()
  ├── Pre-flight checks:
  │   ├── robots.txt check (DISABLE_ROBOTS_CHECK=true recommended for job portals)
  │   ├── is_job_url_active() — lightweight HTTP check
  │   ├── platform credentials check (Dice, Workday, iCIMS)
  │   └── rate limiter (per candidate × platform, hourly)
  ├── BrowserContextManager.get_context()
  │   ├── Browser identity generation (seeded by candidate_id)
  │   ├── Real Chrome (channel="chrome") or bundled Chromium
  │   ├── Proxy injection (PROXY_URL env)
  │   └── Session state restoration (Redis cookies OR file storage_state)
  ├── Browser identity init script injection (navigator patches, WebGL, canvas, audio)
  ├── playwright-stealth v2 applied to page
  ├── adapter.navigate_to_application(page, job_url)
  │   └── Each platform: goto → detect iframe → click Apply → wait for form
  ├── PageAgent.classify_page() — vision check: FORM | LISTING | BLOCKED
  ├── AgentLoop.run() — AUTONOMOUS AGENT TAKES OVER
  │   ├── MAX 60 steps, 240s wall-clock budget
  │   ├── Each step: screenshot + DOM snapshot → LLM → ONE action
  │   ├── Actions: verify_page, fill_field, upload_file, click, scroll, next_step,
  │   │            navigate_url, wait, solve_captcha, abort, done
  │   ├── Batch fill support (multiple fill_field in one LLM call)
  │   ├── STUCK detection (4 consecutive no-DOM-change steps → abort)
  │   └── Post-submit: Gmail OTP polling (verification/code_fetcher.py)
  └── Fallback: deterministic fill_application() if AgentLoop fails
      ├── detect_form() → fill_form() / fill_form_with_llm()
      ├── CaptchaService.solve() (multi-provider, AI-first)
      └── adapter.submit() + adapter.verify_success()
```

---

## 2. Bugs & Issues Found

### 🔴 BUG-01: `color_scheme` and `device_scale_factor` Are Randomized (Not Seeded)

**File:** `browser/stealth_config.py` lines 78, 99  
**Problem:** `color_scheme` and `device_scale_factor` use `random.choice()` — not seeded by `candidate_id`. This means consecutive runs for the same candidate produce a different browser identity on these axes, breaking per-candidate consistency.

```python
# CURRENT (broken):
color_scheme = random.choice(["light", "dark"])
device_scale_factor = round(random.choice([1.0, 1.25, 1.5]), 2)

# FIX: Use _seeded_choice()
color_scheme = _seeded_choice(candidate_id, ["light", "dark"], 9)
device_scale_factor = float(_seeded_choice(candidate_id, ["1.0", "1.25", "1.5"], 10))
```

---

### 🔴 BUG-02: WebGL Vendor/Renderer Are Randomized (Not Seeded)

**File:** `browser/stealth_config.py` lines 83–89  
**Same problem as BUG-01.** The WebGL vendor/renderer combo is one of the strongest browser identity signals; using random makes the identity inconsistent across runs.

```python
# FIX: Use _seeded_choice() with offset 11 and 12
webgl_vendor = _seeded_choice(candidate_id, [
    "Intel Inc.", "NVIDIA Corporation", "ATI Technologies Inc."
], 11)
webgl_renderer = _seeded_choice(candidate_id, [
    "Intel Iris OpenGL Engine",
    "NVIDIA GeForce RTX 3070 OpenGL Engine",
    "ANGLE (Intel, Intel Iris Xe Graphics Direct3D11 vs_5_0)"
], 12)
```

---

### 🔴 BUG-03: `userAgentData.getHighEntropyValues()` Returns Hardcoded Chrome 136

**File:** `browser/stealth_config.py` lines 183–190  
**Problem:** The high-entropy client hints always return `platformVersion: "136.0.0"` regardless of which user agent string was selected. If the UA says `Chrome/134`, but `getHighEntropyValues()` returns `136`, inconsistency is introduced.

```javascript
// CURRENT (broken):
getHighEntropyValues: async () => ({
    platformVersion: '136.0.0',  // ← HARDCODED
    uaFullVersion: '136.0.0.0'   // ← HARDCODED
})

// FIX: Derive version from actual user_agent string
// In build_stealth_init_script(), extract the Chrome version from cfg.user_agent
// and pass it as cfg.chrome_version, then use cfg.chrome_version dynamically
```

---

### 🔴 BUG-04: `navigator.brands` Always Show Chrome 136

**File:** `browser/stealth_config.py` lines 176–180  
**Same problem.** The `navigator.userAgentData.brands` always claim `version: '136'` even when the UA string claims an older version. This cross-signal inconsistency causes form submission failures on some ATS platforms.

---

### 🔴 BUG-05: Canvas Noise Too Minimal

**File:** `browser/stealth_config.py` lines 216–230  
**Problem:** The canvas noise only modifies `data[0]` of a 1×1 pixel. Canvas identity checks typically test larger areas. The 1-pixel approach produces an unusually flat canvas signature.

```javascript
// FIX: Apply subtle noise across a wider area, using seeded pseudo-random
// Use a hash of candidate_id as seed for deterministic but consistent noise
```

---

### 🟡 BUG-06: `DISABLE_ROBOTS_CHECK` Is Not Set by Default

**File:** `services/executor.py` line 410  
**Problem:** robots.txt on most job portals restricts automated access via User-agent headers. The pre-flight robots check silently blocks most portals unless `DISABLE_ROBOTS_CHECK=true` is set.  
**Fix:** Default `DISABLE_ROBOTS_CHECK=true` in `.env.example` or add all major job portal hostnames to the bypass allowlist.

---

### 🟡 BUG-07: AgentLoop Aborts on `MAX_STEPS` for Multi-Step Forms

**File:** `agent/loop.py`  
**Problem:** When the agent hits 60 steps without submitting, it returns `status="MAX_STEPS"`. The executor marks the application FAILED. For complex multi-step forms (iCIMS 2-step, Dice 3-step wizard), 60 steps is insufficient.  
**Fix:** Increase `MAX_STEPS` to 90 for platforms that are known multi-step (Dice, iCIMS, ZipRecruiter), or add a platform-specific `max_steps` override.

---

### 🟡 BUG-08: Cloudflare Turnstile on Talent.com Not Fully Handled

**File:** `adapters/talent.py` line 484 (hints.py)  
**Problem:** The Talent.com adapter acknowledges a Cloudflare Turnstile widget on the "Send application" button but has no actual solve path — it only waits. Turnstile uses browser identity + behavioral signals. The reliable solve path is a properly configured browser session OR a third-party Turnstile API (CapSolver supports Turnstile).  
**Fix:** Integrate CapSolver's `TurnstileTask` type for Talent.com specifically.

---

### 🟡 BUG-09: Session Persistence Only Saves Cookies, Not `localStorage`/`sessionStorage`

**File:** `browser/context_manager.py` lines 212–216  
**Problem:** `save_session()` only saves cookies via `context.cookies()`. Many ATS platforms (Dice, Ashby, ZipRecruiter) store auth tokens in `localStorage`. Restoring only cookies means repeated full logins.  
**Fix:** Use Playwright's `context.storage_state()` which captures cookies + localStorage + sessionStorage together.

---

### 🟡 BUG-10: `_PREFLIGHT_SKIP_HOSTS` Missing New Portals

**File:** `services/executor.py` lines 36–47  
**Problem:** Several targeted portals (`builtin.com`, `glassdoor.com`, `ziprecruiter.com`, `himalayas.app`, `adzuna.com`, `remoteok.com`, `thehiring.cafe`) are not in the preflight skip list, meaning an httpx GET may falsely detect them as expired before the real browser even loads.

---

### 🔴 BUG-11: No Adapters for 7 of the 14 Target Platforms

The registry currently has adapters for: Greenhouse, Lever, Ashby, Workday, LinkedIn, RemoteRocketship, iCIMS, Indeed, Dice, Talent, Generic.

**Missing adapters for:**
- `builtin.com` (BuiltIn)
- `glassdoor.com` (Glassdoor)
- `ziprecruiter.com` (ZipRecruiter Easy Apply)
- `remoteok.com` (RemoteOK)
- `himalayas.app` (Himalayas)
- `adzuna.com` (Adzuna)
- `thehiring.cafe` (The Hiring Cafe)

These all fall through to `GenericFormAdapter`, which has no navigation logic, no cookie handling, and no submit verification.

---

## 3. Browser Identity — Current State & What to Fix

### Current State (What Works)

| Signal | Implementation | Quality |
|--------|----------------|---------|
| User-Agent | Seeded from candidate_id, Chrome 133-136 | ✅ Good |
| Viewport | Seeded, 4 common sizes | ✅ Good |
| Timezone | Seeded, US-only | ✅ Good |
| Locale | Seeded, en-US/en-GB | ✅ Good |
| Navigator.webdriver | Patched to `false` | ✅ Good |
| Navigator.plugins | Faked with Chrome PDF Plugin | ⚠️ Minimal |
| Navigator.permissions | Faked to `prompt` | ✅ Good |
| WebGL vendor/renderer | Random (not seeded) | 🔴 Bug |
| Canvas noise | Minimal 1-pixel | 🔴 Weak |
| Audio noise | Oscillator frequency clamping | ⚠️ Minimal |
| Screen dimensions | Matches viewport | ✅ Good |
| Hardware concurrency | Seeded 4-16 | ✅ Good |
| Device memory | Seeded 4-8 | ✅ Good |
| Color scheme | Random (not seeded) | 🔴 Bug |
| userAgentData | Hardcoded Chrome 136 version | 🔴 Bug |
| TLS fingerprint | Real Chrome binary | ✅ Excellent |
| HTTP/2 ALPN | Real Chrome binary | ✅ Excellent |
| Cookie jar | Restored from Redis | ✅ Good |
| localStorage | NOT saved/restored | 🔴 Missing |
| Behavioral timing | human_delay() random | ✅ Good |
| Mouse movement | No simulation | ⚠️ Missing |
| Scroll behavior | No natural scroll | ⚠️ Missing |

### What Must Be Added

#### A. Full Identity Coherence (Fix existing)

1. Seed ALL random values from `candidate_id`
2. Derive Chrome version dynamically from UA string → use in `userAgentData`
3. Make canvas noise seeded and multi-pixel
4. Add font detection evasion (the browser's available fonts are a strong signal)
5. Add `navigator.connection` spoofing (downlink, rtt, effectiveType)
6. Add `Intl.DateTimeFormat` locale consistency
7. Add `getBattery()` mock (many automation environments don't have battery API)

#### B. Behavioral Identity (New)

```python
# Add to stealth_config.py build script:
# - Natural mouse movement (Bézier curves between actions)
# - Scroll momentum simulation
# - Random micro-pauses between keystrokes
# - Tab focus/blur events before filling
```

#### C. Persistent Identity Across Sessions

```python
# Fix context_manager.py save_session():
# CURRENT (loses localStorage):
cookies = await context.cookies()
await redis.set(session_key, json.dumps(cookies))

# FIX (full storage state):
storage = await context.storage_state()  # includes cookies + localStorage
await redis.set(session_key, json.dumps(storage), ex=604800)

# FIX restore_session():
if session_data:
    # Pass as storage_state to new_context():
    context_kwargs["storage_state"] = json.loads(session_data)
```

---

## 4. Making the Browser Fully Autonomous

The AgentLoop is already 80% autonomous. The remaining 20% are hardcoded assumptions and gaps in the agent's decision-making. Here is what needs to change to make it **fully autonomous**:

### 4.1 Upgrade the AgentLoop Decision Policy

Add these capabilities to `agent/loop.py`:

#### A. Self-Healing Navigation
```
If the agent detects it's on the wrong page (not the job form):
  1. Try click_apply on any visible Apply button
  2. Try navigate_url to the /apply variant of the current URL
  3. If still wrong, try navigate_url to the original job_url
  4. Only abort after 3 recovery attempts
```

#### B. Dynamic Platform Detection Mid-Run
```
If AgentLoop detects it's now on an Ashby/Greenhouse/Lever URL (cross-ATS redirect):
  - Reload the appropriate hints sheet for the newly detected platform
  - Continue with the correct platform context
```

#### C. Multi-Window / New-Tab Handling
```
If a click opens a new tab (target="_blank"):
  - Switch to the new page context
  - Continue AgentLoop on the new page
  - Close it when done and return to original context
```

#### D. Overlay Dismissal as a Reflex
```
Before every LLM call, auto-dismiss:
  - Cookie consent banners (OneTrust, Osano, Cookiebot)
  - "Do you want to leave?" beforeunload dialogs
  - Chat widget popups (Intercom, Drift, HubSpot)
  - GDPR consent walls
  Without requiring an LLM call for these.
```

#### E. Auto-Retry on Transient Failures
```
If a fill_field action fails:
  1. Wait 500ms
  2. Re-read the DOM to confirm the selector exists
  3. Try an alternative selector from the DOM snapshot
  4. Only mark FAILED after 3 attempts on different selectors
```

### 4.2 Autonomous Success / Failure Classification

The agent currently relies on text pattern matching for success detection. Upgrade to:

1. **URL-pattern based success** (done for Dice: `/wizard/success`)
2. **Visual success detection**: Take screenshot after submit → LLM classifies "success", "error", "verification gate", or "still on form"
3. **HTTP response code check**: After submit click, monitor network for `2xx` responses to the submit endpoint

### 4.3 Full Autonomy Env Config

```bash
# .env settings for maximum autonomy:
USE_AGENT_LOOP=true              # ✅ Already default
USE_PAGE_AGENT=true              # ✅ Already default
USE_LLM_FILLER=true              # ✅ Already default
DISABLE_ROBOTS_CHECK=true        # 🔴 Must set for job portals
AGENT_LOOP_MAX_STEPS=90          # ⬆️ Increase from 60
AGENT_LOOP_WALL_TIMEOUT_S=360    # ⬆️ Increase from 240
PLAYWRIGHT_HEADLESS=false        # Recommended for reliable rendering
DRY_RUN_NO_SUBMIT=false          # Must be false for real submissions
```

---

## 5. Captcha Handling — Platform by Platform

### 5.1 Captcha Types Encountered

| Platform | Captcha Type | Current Handler | Action Needed |
|----------|-------------|-----------------|---------------|
| Talent.com | reCAPTCHA v2 + Turnstile | AI + Whisper + CaptchaService | Add Turnstile (CapSolver) |
| Dice | reCAPTCHA v2 (login) | AI + Whisper | ✅ OK |
| Glassdoor | Cloudflare Turnstile | None | Add Turnstile (CapSolver) |
| ZipRecruiter | hCaptcha | None | Add hCaptcha (CapSolver) |
| BuiltIn | reCAPTCHA v3 | Handled passively | ✅ OK |
| Greenhouse | None / reCAPTCHA invisible | Auto-handled | ✅ OK |
| Lever | None | N/A | ✅ OK |
| Ashby | Behavioral check | Pre-submit dwell | Extend dwell if rejected |
| RemoteRocketship | Page-level check | Real Chrome session | ✅ OK |
| Himalayas | None | N/A | N/A |
| RemoteOK | None | N/A | N/A |
| Adzuna | None | N/A | N/A |
| The Hiring Cafe | None | N/A | N/A |

### 5.2 Cloudflare Turnstile Integration (Talent.com Fix)

CapSolver supports Cloudflare Turnstile via `TurnstileTask`. Add to `captcha/service.py`:

```python
async def solve_turnstile(self, page: Page, sitekey: str, action: str = "") -> CaptchaSolution:
    """Solve Cloudflare Turnstile using CapSolver."""
    if self.provider != "capsolver":
        logger.warning("[CAPTCHA] Turnstile requires capsolver provider")
        return CaptchaSolution(captcha_type="turnstile", success=False)
    
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://api.capsolver.com/createTask", json={
            "clientKey": self.api_key,
            "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": page.url,
                "websiteKey": sitekey,
                "action": action or "managed",
            }
        })
        task_id = resp.json()["taskId"]
        token = await self._capsolver_poll(client, task_id)
    
    # Inject token into the Turnstile response field
    await page.evaluate(f"""
        const el = document.querySelector('[name="cf-turnstile-response"]');
        if (el) el.value = '{token}';
    """)
    return CaptchaSolution(captcha_type="turnstile", token=token, success=True)
```

Add Turnstile detection to `captcha/service.py` `solve()` entry point:
```python
if captcha_type == "turnstile":
    sitekey = await self._extract_turnstile_sitekey(page)
    return await self.solve_turnstile(page, sitekey)
```

### 5.3 Captcha Detection in AgentLoop

Add Turnstile and challenge page detection to the AgentLoop DOM snapshot:

```javascript
// Add to _DOM_SNAPSHOT_JS in agent/loop.py:
// Detect Turnstile widget
const turnstile = document.querySelector('.cf-turnstile, [data-sitekey]');
if (turnstile) {
    out.push({type: "turnstile_captcha", sitekey: turnstile.getAttribute('data-sitekey')});
}
// Detect challenge page
if (document.title.includes("Just a moment") || document.querySelector('#challenge-running')) {
    out.push({type: "challenge_page"});
}
```

---

## 6. Per-Portal Adapter Audit & Fix Plan

### 6.1 Greenhouse ✅ (Status: Mostly Working)

**Issues:**
- EEO demographic block on Vercel/Greenhouse has 6 fields; agent sometimes misses the 6th
- Some company domains rate-limit rapid form submissions

**Fixes:**
1. Add a pre-submit demographic validation step that counts EEO fields and verifies all 6 are filled
2. Add `PROXY_URL` residential proxy for Greenhouse hosts that rate-limit datacenter IPs

**Hints to Add:**
```python
"greenhouse": {
    "quirks": [
        # ADD: "On job-boards.greenhouse.io, the page can require a 'I agree to the terms' checkbox before the Resume upload appears. Look for it and tick it first.",
        # ADD: "EEO block has exactly 6 fields. Count them before submitting."
    ]
}
```

---

### 6.2 Dice ✅ (Status: Working — Account Required)

**Issues:**
- `DICE_EMAIL` / `DICE_PASSWORD` must be set — needs a clear error message if missing
- The 3-step wizard can take >60 AgentLoop steps on slow connections

**Fixes:**
1. Increase `MAX_STEPS` for dice to 90
2. Store `storage_state` after login to avoid re-login on subsequent runs

---

### 6.3 Remote Rocketship ✅ (Status: Working)

**Issues:**
- Some RR listings link to platforms not in `_ATS_HOSTS` (e.g., Workable, Rippling, Lever on custom domains)
- Scraping the Apply URL from RR HTML can miss dynamically rendered links

**Fixes:**
1. Expand `_ATS_HOSTS` to include Workable, Rippling, SmartRecruiters, Pinpoint
2. Add a JavaScript evaluation step that captures the resulting navigation from clicking Apply, rather than scraping static `href` values

---

### 6.4 Lever ✅ (Status: Simple, Working)

**Issues:**
- Some custom domain Lever boards use `<company>.lever.co` or embedded iframes

**Fixes:**
1. Add Lever iframe support (detect `iframe[src*="lever.co"]`)
2. Expand URL rewriting to handle `boards.lever.co` and custom domain Lever embeds

---

### 6.5 Ashby ✅ (Status: Good — Submission Rejection is Main Issue)

**Issues:**
- Submission rejection (behavioral check) occurs when the form is filled too quickly
- The `ASHBY_PRE_SUBMIT_DWELL_MS=4000` dwell helps but isn't always enough

**Fixes:**
1. After every file upload, add a 2-3 second dwell (Ashby tracks upload-to-submit timing)
2. If `SPAM_FLAGGED` is returned, apply exponential backoff before retry (5 minutes per ATS domain)
3. Increase dwell to 6000ms and add random mouse movement simulation before submit

---

### 6.6 Talent.com ⚠️ (Status: Captcha is the Main Blocker)

**Issues:**
- Cloudflare Turnstile on "Send application" button blocks unauthenticated sessions
- First-time runs always require solving the reCAPTCHA v2 interstitial
- OTP fetching from Gmail must complete within 5 minutes or token expires

**Fixes:**
1. **Implement Turnstile solver** using CapSolver (see Section 5.2)
2. After first successful application, save full `storage_state` — trusted sessions skip Turnstile on subsequent runs
3. Set Gmail polling interval to 15 seconds with 10 retries for OTP fetch

**Key ENV:**
```bash
TALENT_CAPTCHA_ATTEMPTS=5     # More attempts for first-time sessions
CAPSOLVER_API_KEY=<key>       # Required for Turnstile
```

---

## 7. Missing Adapters (New Platforms)

### 7.1 BuiltIn (builtin.com)

**Apply Flow:** Job listing → "Easy Apply" button → multi-step form (profile, screening, resume upload) → submit  
**Auth Required:** No (anonymous apply with email verification)  
**CAPTCHA:** reCAPTCHA v3 (behavioral, no checkbox — handled passively)  
**Key Behaviors:**
- Form is hosted on `builtin.com/job/<id>/apply`
- Fields: First name, Last name, Email, Phone, LinkedIn (→ "N/A"), Resume upload, Cover letter, Screening questions
- Multi-step: Contact Info → Resume → Screening → Review → Submit

**Adapter Code Template:**
```python
class BuiltInAdapter(BasePlatformAdapter):
    platform_name = "builtin"
    container_selector = "main"
    
    async def navigate_to_application(self, page, job_url):
        # Direct /apply URL or click Easy Apply button
        target = job_url.rstrip("/") + "/apply" if "/apply" not in job_url else job_url
        await page.goto(target, wait_until="domcontentloaded", timeout=25_000)
        # Dismiss overlays
        # Wait for form fields
```

**Hints:**
```python
"builtin": {
    "apply_selectors": ["a:has-text('Easy Apply')", "button:has-text('Apply Now')"],
    "submit_selectors": ["button[type='submit']:has-text('Submit')", "button:has-text('Apply')"],
    "success_patterns": ["application submitted", "thank you for applying"],
    "quirks": [
        "Form is at /job/<id>/apply on builtin.com",
        "LinkedIn field should always be filled with 'N/A'",
        "reCAPTCHA v3 is behavioral — the browser session handles it passively, no manual action needed",
    ]
}
```

---

### 7.2 The Hiring Cafe (thehiring.cafe)

**Apply Flow:** Job listing → "Apply" button → Ashby/Greenhouse/Lever embedded or redirect  
**Key Behaviors:** Acts like an aggregator (similar to RemoteRocketship). Listings link to an underlying ATS.

**Adapter Code:** Extend `RemoteRocketshipAdapter` — add `thehiring.cafe` to `_ATS_HOSTS` detection and registry mapping.

```python
# No special adapter needed — use RemoteRocketship passthrough pattern
```

**Registry addition:**
```python
ADAPTER_REGISTRY["thehiring.cafe"] = RemoteRocketshipAdapter
ADAPTER_REGISTRY["hiringcafe"] = RemoteRocketshipAdapter
```

---

### 7.3 Glassdoor (glassdoor.com)

**Apply Flow:** Job listing → "Easy Apply" / "Apply Now" → Multi-step modal OR redirect to employer ATS  
**Auth Required:** Yes (Glassdoor account + login)  
**CAPTCHA:** Cloudflare Turnstile (on login and sometimes apply)  
**Key Behaviors:**
- Glassdoor Easy Apply is similar to LinkedIn Easy Apply — modal-based
- Many listings redirect to external ATS (Indeed, Greenhouse, Workday)
- Requires `GLASSDOOR_EMAIL` / `GLASSDOOR_PASSWORD`

**Strategy:**
1. Attempt login with stored credentials
2. If listing is "Easy Apply" → fill the Glassdoor modal
3. If listing links to external ATS → resolve and delegate

**Adapter Template:**
```python
class GlassdoorAdapter(BasePlatformAdapter):
    platform_name = "glassdoor"
    container_selector = ".modal-content, [data-test='JobApplicationModal']"
    
    async def navigate_to_application(self, page, job_url):
        await self._ensure_logged_in(page)
        await page.goto(job_url, wait_until="domcontentloaded", timeout=25_000)
        await self._click_easy_apply(page)
```

**Hints:**
```python
"glassdoor": {
    "apply_selectors": [
        "button[data-test='applyButtonGDP']",
        "button:has-text('Easy Apply')",
        "button:has-text('Apply Now')",
    ],
    "quirks": [
        "Account required: set GLASSDOOR_EMAIL and GLASSDOOR_PASSWORD",
        "Turnstile challenge may appear on login — requires CAPSOLVER_API_KEY",
        "Some listings redirect to external ATS — treat as passthrough",
        "Easy Apply opens a modal within the page",
    ]
}
```

---

### 7.4 ZipRecruiter (ziprecruiter.com — Easy Apply Only)

**Apply Flow:** Job detail → "Apply" button → 1-click Easy Apply OR multi-step  
**Auth Required:** Yes — ZipRecruiter account required for Easy Apply  
**CAPTCHA:** hCaptcha (login + sometimes apply)  
**Special:** Phone verification with US number required for account creation  
**Key Behaviors:**
- ZipRecruiter's Easy Apply submits with one click using the pre-built profile
- No manual form fill if profile is complete
- Rate limit: aggressive (max ~5 applications/hour per account)

**Strategy:**
1. Login with `ZIPRECRUITER_EMAIL` / `ZIPRECRUITER_PASSWORD`
2. Navigate to job and click "Apply" → 1-click or confirm-and-apply
3. Handle any screening questions in the modal
4. US phone verification required at account setup time — not automatable; must be pre-configured

**Adapter Template:**
```python
class ZipRecruiterAdapter(BasePlatformAdapter):
    platform_name = "ziprecruiter"
    container_selector = "form[data-testid='apply-form'], .apply-modal"
    
    async def navigate_to_application(self, page, job_url):
        await self._ensure_logged_in(page)
        await page.goto(job_url, wait_until="domcontentloaded", timeout=25_000)
        # Click the Easy Apply button
        # Handle 1-click or multi-step modal
```

**Hints:**
```python
"ziprecruiter": {
    "apply_selectors": [
        "button[data-testid='joblist-apply-button']",
        "button:has-text('Apply')",
        "a:has-text('Apply Now')",
    ],
    "submit_selectors": [
        "button:has-text('Apply Now')",
        "button:has-text('Submit Application')",
        "button[type='submit']",
    ],
    "success_patterns": [
        "applied successfully",
        "application submitted",
        "you've applied",
    ],
    "quirks": [
        "Account required: ZIPRECRUITER_EMAIL + ZIPRECRUITER_PASSWORD",
        "US phone number required at account setup (one-time, pre-configured by operator)",
        "Only Easy Apply postings — skip listings with 'Apply on company site'",
        "hCaptcha may appear at login — use CaptchaService with capsolver provider",
        "Rate limit: ~5 applications/hour per account — add per-account rate limiter",
    ]
}
```

---

### 7.5 RemoteOK (remoteok.com)

**Apply Flow:** Job listing → "Apply" button → external ATS (Greenhouse, Lever, Ashby) or email  
**Auth Required:** No  
**Key Behaviors:** Pure aggregator like RemoteRocketship — always redirects to external ATS

**Adapter:** Use RemoteRocketship pattern exactly — extract the Apply link href and delegate to inner ATS adapter.

```python
# Just add to registry:
ADAPTER_REGISTRY["remoteok"] = RemoteRocketshipAdapter
ADAPTER_REGISTRY["remoteok.com"] = RemoteRocketshipAdapter
```

---

### 7.6 Himalayas (himalayas.app)

**Apply Flow:** Job listing → "Apply" button → external ATS link OR Himalayas-native form  
**Auth Required:** No  
**CAPTCHA:** None observed  
**Key Behaviors:**
- Some listings link directly to Greenhouse/Lever/Ashby — pure aggregator
- Some listings have a Himalayas-native "Quick Apply" that collects name/email/resume and forwards

**Adapter:**
```python
class HimalayasAdapter(BasePlatformAdapter):
    platform_name = "himalayas"
    
    async def navigate_to_application(self, page, job_url):
        await page.goto(job_url, wait_until="domcontentloaded", timeout=25_000)
        # Check for native Quick Apply vs external link
        quick_apply = await page.locator("button:has-text('Quick Apply')").count()
        if quick_apply:
            await page.locator("button:has-text('Quick Apply')").first.click()
            # Handle native form
        else:
            # Find and follow external ATS link
            hrefs = await page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a=>a.href)")
            for href in hrefs:
                ats = _detect_ats_from_url(href)  # Reuse RR detection
                if ats:
                    await page.goto(href)
                    # Delegate to ATS adapter
                    break
```

---

### 7.7 Adzuna (adzuna.com)

**Apply Flow:** Job aggregator → "Apply Now" → external employer ATS always  
**Auth Required:** No  
**CAPTCHA:** None  
**Key Behaviors:** Pure link aggregator — always redirects to external ATS

**Adapter:** Use RemoteRocketship pattern.

```python
ADAPTER_REGISTRY["adzuna"] = RemoteRocketshipAdapter
```

---

## 8. Implementation Checklist

### Phase 1: Fix Existing Bugs

- [ ] **FIX BUG-01/02:** Seed `color_scheme`, `device_scale_factor`, `webgl_vendor`, `webgl_renderer` from `candidate_id`
- [ ] **FIX BUG-03/04:** Derive Chrome version dynamically from UA string; use it in `userAgentData.brands` and `getHighEntropyValues()`
- [ ] **FIX BUG-05:** Improve canvas noise to cover wider pixel area with seeded noise
- [ ] **FIX BUG-06:** Add all target portals to `DISABLE_ROBOTS_CHECK` default or expand bypass list
- [ ] **FIX BUG-07:** Set `MAX_STEPS=90` for multi-step platforms (Dice, iCIMS, ZipRecruiter)
- [ ] **FIX BUG-09:** Replace cookie-only session with full `context.storage_state()` save/restore
- [ ] **FIX BUG-10:** Add new portal hostnames to `_PREFLIGHT_SKIP_HOSTS`

### Phase 2: Browser Identity Upgrade

- [ ] Add `navigator.connection` spoofing (NetworkInformation API)
- [ ] Add `navigator.getBattery()` mock
- [ ] Add keyboard timing variance (5-50ms per keystroke vs current instant fill)
- [ ] Add mouse movement simulation (Bézier path between element center and click point)
- [ ] Add font enumeration countermeasure
- [ ] Increase `Intl.DateTimeFormat` timezone consistency with spoofed timezone

### Phase 3: Captcha Handling

- [ ] Implement Cloudflare Turnstile solver (CapSolver `AntiTurnstileTaskProxyLess`)
- [ ] Add Turnstile detection to AgentLoop DOM snapshot
- [ ] Add `captcha_type="turnstile"` to `AgentAction` and `solve_captcha` action handler
- [ ] Test Talent.com with `CAPSOLVER_API_KEY` configured
- [ ] Add challenge page detection + CapSolver integration

### Phase 4: New Adapters

- [ ] **BuiltIn adapter** (`adapters/builtin.py`) + hints + registry entry
- [ ] **Glassdoor adapter** (`adapters/glassdoor.py`) + auth + hints + registry entry
- [ ] **ZipRecruiter adapter** (`adapters/ziprecruiter.py`) + auth + hints + registry entry + US phone note
- [ ] **RemoteOK** — add to registry as `RemoteRocketshipAdapter` alias
- [ ] **Himalayas adapter** (`adapters/himalayas.py`) — native quick-apply + ATS passthrough
- [ ] **Adzuna** — add to registry as `RemoteRocketshipAdapter` alias
- [ ] **The Hiring Cafe** — add to registry as `RemoteRocketshipAdapter` alias

### Phase 5: Autonomy Upgrades

- [ ] Multi-tab/popup handling in AgentLoop
- [ ] Overlay auto-dismissal reflex (pre-LLM, deterministic)
- [ ] Dynamic platform re-detection mid-run
- [ ] Visual success classification (LLM screenshot analysis post-submit)
- [ ] Exponential backoff per ATS domain after `SPAM_FLAGGED`
- [ ] Increase `AGENT_LOOP_WALL_TIMEOUT_S` to 360 for multi-step flows

---

## 9. Engineering Prompts for Each Task

These are ready-to-paste prompts for implementing each section. Use them with Claude, Gemini, or any code AI.

---

### PROMPT: Fix Browser Identity in `browser/stealth_config.py`

```
You are improving the browser identity system in backend/app/browser_automation/browser/stealth_config.py.

CURRENT BUGS TO FIX:
1. `color_scheme` and `device_scale_factor` use Python's `random.choice()` — they must use `_seeded_choice()` with unique offsets (9 and 10 respectively) so the identity is deterministic per candidate_id.

2. `webgl_vendor` and `webgl_renderer` also use `random.choice()` — fix using `_seeded_choice()` with offsets 11 and 12.

3. The `build_stealth_init_script()` function hardcodes Chrome version `136` in `userAgentData.brands` and `getHighEntropyValues()`. Fix by:
   - In Python: extract the Chrome major version from `config.user_agent` using regex `r"Chrome/(\d+)"`
   - Pass it as `cfg.chrome_version` in the JSON payload
   - In the JS script: use `cfg.chrome_version` instead of hardcoded `136`

4. Canvas noise only modifies 1 pixel. Improve to apply subtle seeded noise to a 4x4 region using a deterministic pattern derived from candidate_id hash.

CONSTRAINTS:
- Do NOT break the `StealthConfig` pydantic model interface — other files import it
- Do NOT change the function signatures of `get_stealth_config()` or `build_stealth_init_script()`
- The JS patch must be syntactically valid and not conflict with Playwright's init script injection
- Test that the seeded functions always return the same value for the same candidate_id input
```

---

### PROMPT: Implement Cloudflare Turnstile Solver

```
You are adding Cloudflare Turnstile captcha solving support to backend/app/browser_automation/captcha/service.py.

REQUIREMENTS:
1. Add a new method `solve_turnstile(self, page, sitekey, action="")` to `CaptchaService`
   - Only works when provider is "capsolver"
   - Uses CapSolver's `AntiTurnstileTaskProxyLess` task type
   - Injects the token into `[name="cf-turnstile-response"]` input
   - Returns a `CaptchaSolution` with captcha_type="turnstile"

2. Add a private method `_extract_turnstile_sitekey(self, page)` that:
   - Looks for `.cf-turnstile[data-sitekey]` attribute
   - Looks for `script[src*="turnstile"]` and extracts sitekey from it
   - Returns None if not found

3. In the `solve()` method entry point, add:
   if captcha_type == "turnstile":
       sitekey = await self._extract_turnstile_sitekey(page)
       if not sitekey:
           logger.warning("[CAPTCHA] Turnstile sitekey not found")
           return CaptchaSolution(captcha_type="turnstile", success=False)
       return await self.solve_turnstile(page, sitekey)

4. In the `_PROVIDER_ENV` dict, add "capsolver": "CAPSOLVER_API_KEY" if not already present

CONSTRAINTS:
- The CapSolver API URL is https://api.capsolver.com/createTask
- Poll interval: 3 seconds, max 20 polls
- Tokens expire in ~120 seconds — inject immediately after solving
- Do NOT break existing 2captcha, anticaptcha, or AI solver paths
```

---

### PROMPT: Implement ZipRecruiter Easy Apply Adapter

```
You are creating a new adapter backend/app/browser_automation/adapters/ziprecruiter.py.

REQUIREMENTS:
1. Subclass `BasePlatformAdapter` with platform_name = "ziprecruiter"
2. In `navigate_to_application()`:
   - Call `_ensure_logged_in(page)` first (ZIPRECRUITER_EMAIL + ZIPRECRUITER_PASSWORD)
   - Navigate to the job URL
   - Dismiss overlays (OneTrust cookie banner)
   - Find and click the "Apply" / "Easy Apply" button
   - Confirm the application modal or next page loaded
3. Login flow:
   - Navigate to https://www.ziprecruiter.com/login
   - Fill email, then password, then click Sign In
   - Handle hCaptcha if it appears (use CaptchaService)
   - Wait for redirect off /login page
4. `fill_application()`: delegate to `detect_form()` + `fill_form()` on the modal
5. `submit()`: click the "Apply Now" or "Submit Application" button in the modal
6. `verify_success()`: check for text patterns "applied successfully" / "application submitted"
7. `detect_application_type()`: return "EASY_APPLY"

Add hints to adapters/hints.py under "ziprecruiter" key.
Add "ziprecruiter" to adapters/registry.py ADAPTER_REGISTRY.

IMPORTANT NOTE — US Phone:
ZipRecruiter requires a US phone number at account registration. The operator must:
1. Create a ZipRecruiter account manually with a US number
2. Set ZIPRECRUITER_EMAIL and ZIPRECRUITER_PASSWORD in .env
3. After the first successful login, storage_state is saved and future runs skip login

Add this note as a comment in the module docstring.
```

---

### PROMPT: Implement BuiltIn Adapter

```
You are creating a new adapter backend/app/browser_automation/adapters/builtin.py.

REQUIREMENTS:
1. Subclass `BasePlatformAdapter` with platform_name = "builtin"
2. In `navigate_to_application()`:
   - Navigate to job URL
   - If URL doesn't end in /apply, try appending /apply and navigating there
   - If that 404s, fall back to the job listing and click the "Easy Apply" button
   - Wait for form inputs to appear
   - Dismiss cookie banners
3. `fill_application()`: detect_form() + fill_form() + handle file uploads
   - LinkedIn fields → always fill with "N/A"
4. `submit()`: look for "button[type='submit']", "button:has-text('Apply')"
5. `verify_success()`: check for "thank you for applying", "application submitted", "application received"

Add hints to adapters/hints.py under "builtin" key:
- apply_selectors: Easy Apply button
- submit_selectors: Submit/Apply buttons
- success_patterns
- quirks about reCAPTCHA v3 (behavioral, handled by browser session passively)

Add to registry: "builtin" → BuiltInAdapter

NOTE: reCAPTCHA v3 on BuiltIn is behavioral (no checkbox). The browser session handles it passively.
Do NOT attempt to manually solve it.
```

---

### PROMPT: Implement Glassdoor Adapter

```
You are creating a new adapter backend/app/browser_automation/adapters/glassdoor.py.

REQUIREMENTS:
1. Subclass `BasePlatformAdapter` with platform_name = "glassdoor"
2. The adapter MUST handle two branches:
   a. Glassdoor Easy Apply (modal within Glassdoor)
   b. External redirect to employer ATS (passthrough like RemoteRocketship)
3. Authentication:
   - Navigate to https://www.glassdoor.com/profile/login_input.htm
   - Fill email + password (GLASSDOOR_EMAIL + GLASSDOOR_PASSWORD env vars)
   - Handle Turnstile on login (use CaptchaService with provider=capsolver)
   - Wait for redirect off login page
   - Save storage_state after successful login
4. Branch detection after loading job URL:
   - If page has button[data-test='applyButtonGDP'] or "Easy Apply" button → Glassdoor native flow
   - If "Apply on company site" / external link → extract href and delegate to ATS adapter
     (reuse RemoteRocketship _detect_ats_from_url logic)
5. For native Easy Apply flow:
   - Click the Easy Apply button → modal opens
   - fill_form() within modal container
   - submit via modal submit button
   - verify success by text pattern
6. For external flow:
   - Navigate to the external ATS URL
   - get_adapter(ats_key).navigate_to_application(page, external_url)
   - Set self._inner = inner adapter (same pattern as RemoteRocketshipAdapter)

Add Glassdoor hints to hints.py.
Add "glassdoor" to registry.
```

---

### PROMPT: Upgrade Session Persistence to Full `storage_state`

```
You are upgrading session persistence in backend/app/browser_automation/browser/context_manager.py.

CURRENT PROBLEM: save_session() saves only cookies (context.cookies()). Many ATS platforms store
auth tokens in localStorage and sessionStorage. Restoring only cookies causes repeated full logins.

REQUIRED CHANGES:
1. In `save_session()`:
   OLD: cookies = await context.cookies()
        await redis.set(session_key, json.dumps(cookies), ex=604800)
   NEW: state = await context.storage_state()
        await redis.set(session_key, json.dumps(state), ex=604800)

2. In `get_context()` when restoring from Redis:
   OLD: cookies = json.loads(session_data)
        await context.add_cookies(cookies)
   NEW: The storage_state dict has "cookies", "origins" keys (Playwright format)
        Pass as context_kwargs["storage_state"] = json.loads(session_data) BEFORE new_context()
        Remove the post-creation add_cookies() call since storage_state includes cookies

3. Also fix the persistent context path (use_extension=True branch) to use storage_state the same way.

CONSTRAINTS:
- context.storage_state() returns {"cookies": [...], "origins": [{"origin": "...", "localStorage": [...]}]}
- This is directly usable as the storage_state parameter to new_context()
- The Redis TTL (7 days) remains unchanged
- Add a try/except around storage_state() in case older context objects don't support it — fall back to cookies only in that case
- Maintain backward compatibility: if the Redis data looks like a list (old cookie format), still handle it as cookies
```

---

*End of Master Prompt Document*  
*This document is the single source of truth for all browser automation module improvements.*  
*Reference it when issuing any implementation task to an AI assistant.*
