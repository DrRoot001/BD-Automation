# Browser Automation Module — Production-Readiness Pass

**Module:** `backend/app/browser_automation`
**Date:** 2026-07-03
**Basis:** verified every finding in [BROWSER_AUTOMATION_AUDIT.md](BROWSER_AUTOMATION_AUDIT.md) against the *current* working tree (the audit dated 2026-07-02 was already stale on several items), then fixed every genuinely-open, code-fixable gap and added the missing test coverage.

---

## TL;DR

- **3 of the audit's headline P0s were already fixed** in the working tree (FlareSolverr dead-code, synthetic mouse motion, keystroke timing). No action needed — see the "Already resolved" table.
- **12 genuine code-fixable gaps were fixed** this pass (1 data-integrity bug, several resilience/robustness improvements, and housekeeping). See "Fixed this pass".
- **The remaining blockers are infrastructure/spend, not code** — chiefly Lever's hCaptcha (needs a residential proxy or a paid hCaptcha solver key), MFA-protected accounts, and Talent.com's Gmail-only OTP. These cannot be closed by code alone; the code paths are complete and waiting on the resource. See "Still blocked (infra/spend)".
- **19 unit/fixture tests added** (were 1) covering identity determinism, form detection, and every new hardening helper. All green.

---

## Fixed this pass (code-complete)

| # | Area | Change | Files |
|---|------|--------|-------|
| 1 | **Executor (data integrity)** | The final `SUBMITTED` status PATCH return value was ignored — a failed DB write was logged then silently reported as success. Now captured: on failure we log CRITICAL and surface `ApplicationResult.db_persisted=False`. We deliberately do **not** flip to FAILED (the application already went to the employer; a retry would double-submit) — the flag lets a caller reconcile the DB without re-applying. | `services/executor.py`, `services/models.py` |
| 2 | **Agent loop** | DOM snapshot field cap 50 → 80 (long EEO/demographic forms were truncated → premature submit). The completeness gate already counts the *untruncated* required set (the real backstop) and now surfaces up to 25 unfilled fields. | `agent/loop.py` |
| 3 | **Agent loop** | `fill_field` selector-fallback chain: on a stale CSS selector it now retries via `get_by_label`/`get_by_placeholder` (plain-string, injection-safe) before forcing an LLM re-plan; the demographic-swap defence re-checks the resolved element so a wrong match still aborts safely. | `agent/loop.py` |
| 4 | **Agent loop (compliance)** | Submit-time LinkedIn-URL policy enforcement: a final React-aware scan rewrites any non-policy LinkedIn value right before submit, on the correct frame context — defense-in-depth beyond the existing pre-fill and memory overrides. | `agent/loop.py` |
| 5 | **Executor/Browser** | Redis session TTL is now refreshed on **restore** (not only save) and made configurable (`SESSION_TTL_DAYS`), so an actively-used session never expires mid-batch even when runs fail before `save_session`. | `browser/context_manager.py` |
| 6 | **Forms** | Multi-select checkbox groups (checkboxes sharing a `name`) now consolidate into one `multi_select` field with options; the filler matches provided value(s) to option boxes. The critical lone consent/terms checkbox path is provably unchanged (proven by test). | `forms/detector.py`, `forms/filler.py`, `forms/models.py` |
| 7 | **Forms** | Real multi-step count from "Step N of M" captions / progress rails, replacing the hardcoded `steps=2` placeholder. | `forms/detector.py` |
| 8 | **Verification** | Gmail OTP polling timeout is now `VERIFY_CODE_TIMEOUT_S` (default 90s) — some ATSes take 120s+. | `verification/code_fetcher.py`, `agent/loop.py` |
| 9 | **Forms** | Field-memory staleness: recalled answers older than `FIELD_MEMORY_TTL_DAYS` (default 180) are ignored; legacy undated records are grandfathered so nothing is wiped on upgrade. | `forms/memory.py` |
| 10 | **Adapters** | Built In new-tab handoff timeout 15s → configurable `BUILTIN_NEWTAB_TIMEOUT_MS` (default 25s) — slow employer ATSes were missing the 15s window and falling back to generic. | `adapters/builtin.py` |
| 11 | **Adapters** | Stale-session recovery: shared `session_utils.invalidate_session_file()`. LinkedIn now clears the dead session file + raises a clear `LOGIN_REQUIRED`; Dice/Glassdoor clear the file on a failed login so a corrupt blob can't wedge every future run. New hints for Workable/SmartRecruiters/Rippling/PinpointHQ. ZipRecruiter success-pattern coverage broadened. | `adapters/linkedin.py`, `adapters/dice.py`, `adapters/glassdoor.py`, `adapters/ziprecruiter.py`, `adapters/hints.py`, `adapters/session_utils.py` (new) |
| 12 | **Captcha/Housekeeping** | Removed dead `forms/ai_resolver.py`. `ai_solver` hCaptcha path no longer silently runs reCAPTCHA-only selectors — it now does an honest hCaptcha checkbox pass (using the previously-dead `_HCAPTCHA_ANCHOR`) and fails fast to AgentV otherwise. CapSolver "We don't support this service" now logs an explicit, actionable line. Stale NopeCHA comments corrected. | `captcha/ai_solver.py`, `captcha/service.py`, `agent/loop.py`, `forms/ai_resolver.py` (deleted) |

## Already resolved before this pass (audit was stale)

| Audit item | Reality in current code |
|------------|-------------------------|
| P0-2 "no synthetic mouse movement" | `_bezier_mouse_move` (Bezier curves, jittered steps) already applied on clicks (`agent/loop.py`). |
| P0-2 "no keystroke timing" | Per-character typing delays via `press_sequentially`, env-tunable (`HUMAN_TYPING`, `HUMAN_TYPE_MIN/MAX_MS`). |
| P0-3 "FlareSolverr fallback is dead code / AttributeError" | `_solve_turnstile_flaresolverr()` is fully implemented and `browser/flaresolverr.py` exists. |
| P2-26 "JSON truncation recovery misses mid-object" | `_extract_json_object()` finds the last balanced `{...}`, escape-aware — handles truncation. |
| P1-6 "AgentLoop terminal statuses no fallback" | Intentional and correct: terminal outcomes are propagated as exceptions by the executor; the mutual-exclusion design prevents scripted-fallback DOM conflicts / double-submits. |

## Still blocked (infra / spend — no code fix possible)

| Blocker | Why it's not code | What unblocks it |
|---------|-------------------|------------------|
| **Lever hCaptcha (the #1 blocker)** | The only integrated solver that runs is `hcaptcha-challenger` (AgentV, ~30–50%). CapSolver returns "We don't support this service" for hCaptcha; NopeCHA needs a key. The code paths (AgentV, NopeCHA provider, reload-reappearance handling) are complete. | A **residential `PROXY_URL`** (Lever's invisible hCaptcha then passes with no challenge) **or** a real `NOPECHA_API_KEY`. |
| **PROXY_URL not deployed** | Plumbing in `context_manager.py` is complete and correct; it's untested only because no proxy is provisioned. | Provision a residential/rotating proxy and set `PROXY_URL`. |
| **Glassdoor/Dice MFA accounts** | MFA needs out-of-band secrets (SMS/email/TOTP) the automation can't derive. Both adapters detect the wall and raise a clear BLOCKED. | Use accounts with MFA disabled, or seed a pre-authenticated session, or provide a TOTP seed for a future integration. |
| **Talent.com OTP (Gmail-only)** | No SMS fallback exists; `code_fetcher` is Gmail-only. | Candidate Gmail connected, or a future Twilio/SMS integration (needs a Twilio account). |

Notes intentionally left as documented limitations (higher risk to change blind, low incremental value): mid-flow HTTP-401 re-auth for account-walled adapters (the adapters already re-login on login-page detection at navigate time, and now clear stale files on failure), and full DOM-snapshot pagination beyond 80 fields (the completeness gate already prevents premature submit past the cap).

## New environment variables (all optional, defaults shown)

```
FIELD_MEMORY_TTL_DAYS=180       # 0 disables field-memory expiry
VERIFY_CODE_TIMEOUT_S=90        # Gmail OTP polling budget (seconds)
SESSION_TTL_DAYS=7              # Redis session TTL; refreshed on restore+save
BUILTIN_NEWTAB_TIMEOUT_MS=25000 # Built In → employer-ATS handoff wait
```

Also documented in [backend/.env.example](backend/.env.example).

## Test coverage added

Was: 1 test file (`test_frame_swap.py`). Now: **19 tests, all passing.**

- `tests/test_stealth_config.py` (10) — identity determinism, timezone/locale coherence, canvas-seed derivation, init-script assembly, WebGL/font opt-in gating.
- `tests/test_detector_forms.py` (2, headless Chromium) — checkbox-group consolidation **and** consent-checkbox-stays-boolean (no regression), multi-step count from a caption.
- `tests/test_production_hardening.py` (6) — field-memory TTL, OTP timeout override, session TTL override, session-file invalidation.

```bash
pytest backend/app/browser_automation/tests/ -q
```
