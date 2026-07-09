# BD Automator: Handoff Document v4
## Focus: Browser Automation & AI Pipeline Optimization

**Date:** July 2026
**Author:** Senior AI Automations Engineer

This document serves as a comprehensive handoff for the next AI agent (Claude) taking over development. It details the exact state of the BD Automator pipeline, focusing heavily on Module 4 (Browser Automation) and the recent architectural shifts in Module 3 (AI Orchestration).

---

### 1. Browser Automation (Module 4) State & Updates

#### Patchright Integration
We have successfully integrated **Patchright** (an undetected, open-source fork of Microsoft Playwright) to handle platforms with aggressive anti-bot mitigation. 
- **Implementation:** Patchright is dynamically provisioned via the `_PATCHRIGHT_PLATFORMS` set located in `backend/app/browser_automation/browser/context_manager.py`. 
- **Current Target:** It is currently enabled specifically for `himalayas.app` to natively bypass Cloudflare Turnstile and Managed Challenges without relying entirely on third-party solvers.

#### CAPTCHA & Cloudflare IP Reputation Findings
- **The Issue:** While we built fallback logic to inject Anti-Captcha tokens and submit challenge forms (in `captcha/service.py`), Cloudflare's Managed Challenge (the interactive "Verify you are human" checkbox) heavily weights IP reputation.
- **The Finding:** Datacenter/VPS IPs are consistently flagged and soft-locked by Cloudflare regardless of human-like cursor movements or token injection. 
- **The Rule:** For platforms behind strict Cloudflare gates (Himalayas, Built In), a **Residential Proxy** (`PROXY_URL` in `.env`) is strictly required.

#### Platform-Specific Status
- **Himalayas:** Patchright is active. Requires residential IP to pass the final Cloudflare gate.
- **BuiltIn:** We executed a database fix to clear 23 built-in jobs that were incorrectly marked `is_duplicate=True` by the Node.js scraper. Note: `matching.py` explicitly skips BuiltIn jobs if `PROXY_URL` is not set.
- **Stable Platforms:** `dice.com`, `talent.com`, `remoterocketship.com`, `indeed.com`, and `adzuna.com` are fully operational and passing through the executor loops properly.

---

### 2. Orchestration & Resume Tailoring (Module 3) Pipeline Shift

#### The Logical Flaw
Previously, the system was prematurely dropping valid applications. The orchestrator evaluated the ATS score of the candidate's **base resume** against the `.env` `APPLY_SCORE_THRESHOLD` (e.g., 40). If it failed, the application was dropped to `ANALYZED` status **before** the AI had a chance to tailor the resume.

#### The Architecture Fix
We rewrote the logic in `backend/module3/orchestrator.py` (both `orchestrate_application_package` and `prepare_package_for_live_application`) to prioritize aggressive tailoring:
1. **Base Evaluation:** `score_job_fit` runs to identify missing keywords.
2. **Mandatory Tailoring:** The `tailor_resume` function runs *unconditionally*, using the Gemini LLM and Fabricator prompt to aggressively weave missing keywords into the candidate's experience and summary.
3. **Post-Tailoring Gate:** The gate check was moved to the very end of the pipeline. The system now evaluates `tailored_resume.ats_score_after` against the `APPLY_SCORE_THRESHOLD`. 
4. **Transition:** If the new, tailored score passes the gate, the application transitions to `QUEUED` and is dispatched to the Celery worker for Browser Automation execution.

*Note: We also scrubbed the orchestrator of hardcoded "75" logging messages. It now dynamically prints the exact threshold loaded from `.env`.*

---

### 3. Recent Updates Merged from Main Branch

To ensure we are building on the most stable base, we have successfully synced and incorporated the latest features from the `main` branch. These features run perfectly alongside our orchestrator pipeline shifts:

1. **Robust CAPTCHA Enhancements (`captcha/service.py`):**
   - Implemented a smart auth/balance gate to probe provider balances (Anti-Captcha/CapSolver) *before* committing to retry loops.
   - Now immediately skips paid CAPTCHA retries if the provider is dead or out of balance, saving time and preventing infinite hang loops.
   - Improved error reporting: Generates strict terminal `BLOCKED` mappings rather than generic failures for unsupported CAPTCHA types or managed-mode site keys that cannot be solved without residential proxies.
2. **Browser Automation Hardening (`agent/loop.py` & `executor.py`):**
   - Added stronger URL sanitization and state recovery mechanisms to prevent executor stalls on broken or malformed job links.
   - Updated the `learned_fixes` JSON definitions (e.g., Workday, RemoteRocketship) and field memory patterns to increase form-fill accuracy.
3. **Frontend & Job Discovery:**
   - Added a **Scrape Time Filter** and **Platform Selector** to the candidate Auto-Apply UI, giving operators granular control over which jobs are fed into the Module 3 tailoring engine.
   - Added an explicit error state in the frontend if a candidate is missing mandatory third-party accounts (e.g., Dice).
   - Added a "Disconnect Gmail" feature for easier candidate credential management.

---

### 4. Immediate Aim & Next Steps for Claude

**Our Core Aim:** Flawless, zero-touch end-to-end execution. When a user hits "Start Auto-Apply" on the Next.js frontend, the backend must dynamically source jobs, aggressively tailor the resume to cross the ATS threshold, and execute the browser submission without stalling.

**Your Objectives (Claude):**
1. **Monitor Patchright Performance:** Ensure `himalayas.app` submissions flow smoothly when a residential proxy is attached. If Cloudflare introduces new heuristics, investigate stealth plugin updates or behavioral fingerprinting adjustments.
2. **Proxy Integration:** Ensure proxy rotation and assignment in `context_manager.py` are robustly attaching to the Patchright context. 
3. **Module 3 Economics:** Because we are now tailoring *every* resume before gating it, LLM token consumption will rise. Monitor this cost vs. the increase in viable applications, and consider fast-failing applications whose base score is abysmally low (e.g., < 10) to save API costs.
