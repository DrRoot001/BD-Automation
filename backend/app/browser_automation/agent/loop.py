"""Vision-driven agent loop for autonomous form filling.

The loop replaces the fixed script with a continuous perception → decision →
action cycle. On every step the agent receives a screenshot + scoped DOM and
returns ONE structured action. Playwright executes it, then the loop repeats.

Supported actions
-----------------
  verify_page   — first step; confirm this is a job application form
  fill_field    — type / select / check a value into a field
  upload_file   — attach resume or cover letter
  click         — click any element (buttons, checkboxes, Apply links)
  scroll        — scroll the viewport to reveal off-screen content
  next_step     — click "Next" / "Continue" on multi-step forms
  wait          — short pause (e.g. after a dialog opens)
  abort         — give up with a reason (wrong page, bot wall, etc.)
  done          — application submitted; include confirmation text

Design constraints
------------------
- Max MAX_STEPS iterations to prevent runaway loops.
- STUCK_THRESHOLD consecutive steps with no DOM change → abort.
- Every action is logged with its step index for debugging.
- LLM errors are non-fatal for up to LLM_RETRY_LIMIT retries, then abort.
- File uploads always use local paths resolved by the executor.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from playwright.async_api import Frame, Page

from ..adapters.hints import format_hints_for_prompt, get_platform_hints
from ..llm import LLMUnavailable, get_llm

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

MAX_STEPS = 60          # hard cap on loop iterations
# Wall-clock ceiling. Even if MAX_STEPS isn't reached, the loop ABORTS after
# this many seconds so a confused AI on an unsupported site doesn't burn a
# tester's patience. Env-overridable: AGENT_LOOP_WALL_TIMEOUT_S.
_DEFAULT_WALL_TIMEOUT_S = float(__import__("os").getenv("AGENT_LOOP_WALL_TIMEOUT_S", "240"))
STUCK_THRESHOLD = 4     # consecutive no-DOM-change steps before abort
LLM_RETRY_LIMIT = 3     # consecutive LLM failures before abort
STEP_TIMEOUT_S = 45.0   # per-step LLM call timeout (was 30s — bumped after observing
                        # Anthropic vision calls occasionally taking 30-40s during
                        # peak hours, causing unnecessary fallback to OpenRouter/Gemini)
DOM_HASH_SELECTOR = "body"  # element used to detect DOM changes between steps


# ─────────────────────────────────────────────────────────────────────────────
# Action schema
# ─────────────────────────────────────────────────────────────────────────────

ActionKind = Literal[
    "verify_page",
    "fill_field",
    "upload_file",
    "click",
    "click_apply",
    "scroll",
    "next_step",
    "navigate_url",
    "wait",
    "abort",
    "done",
]


@dataclass
class AgentAction:
    kind: ActionKind
    # fill_field / upload_file
    selector: Optional[str] = None
    value: Optional[str] = None          # text value or "resume" / "cover_letter"
    field_label: Optional[str] = None    # human label for logging
    # scroll
    direction: Optional[Literal["down", "up"]] = None
    # abort / done
    reason: Optional[str] = None
    confirmation: Optional[str] = None
    # click
    click_text: Optional[str] = None     # :has-text fallback if selector fails
    # navigate_url — for AI to self-recover when on wrong page
    url: Optional[str] = None
    # internal — set by the executor after the action runs
    step: int = 0
    ok: bool = True                      # False if Playwright couldn't execute
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LoopResult:
    success: bool
    status: Literal[
        "SUBMITTED", "FORM_COMPLETED", "ABORTED", "MAX_STEPS",
        "STUCK", "LLM_UNAVAILABLE", "WRONG_PAGE", "ERROR"
    ]
    confirmation: Optional[str] = None
    error: Optional[str] = None
    steps_taken: int = 0
    actions: List[AgentAction] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt templates
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = "(populated at runtime by AgentLoop._build_system_prompt)"

_SYSTEM_PROMPT_BASE = """You are {agent_name}.

══════════════════════════════════════════════════════════════════════════
ROLES — KNOW WHO DECIDES AND WHO EXECUTES
══════════════════════════════════════════════════════════════════════════
You are the MASTER. You are the AI. Every decision on this run — what page
this is, what field to fill, what value to type, when to scroll, when to
click Apply, when a popup needs dismissing, when to submit — is YOUR call.
You analyze, you plan, you research, you decide.

Playwright is the SLAVE. It has no judgment. It only executes the ONE action
you return per turn. It does not pick selectors. It does not interpret the
page. It does not skip steps. It does whatever you tell it, exactly as you
tell it.

If something unexpected appears — a popup, a modal, a cookie banner, a
redirect to a careers landing, a missing form, a different layout — that
is YOUR call to navigate. Playwright will not "figure it out". You will.

══════════════════════════════════════════════════════════════════════════
WHO YOU REPRESENT (their data is what you fill — never invent)
══════════════════════════════════════════════════════════════════════════
{candidate_card}

══════════════════════════════════════════════════════════════════════════
THE JOB YOU ARE APPLYING TO
══════════════════════════════════════════════════════════════════════════
{job_card}

══════════════════════════════════════════════════════════════════════════
HOW TO THINK ON EVERY TURN — ANALYZE → PLAN → ACT
══════════════════════════════════════════════════════════════════════════
Every turn you receive a fresh screenshot + DOM snapshot. Walk through this
mental sequence before responding:

  1. ANALYZE — What is on screen RIGHT NOW?
     • Is this an application form? A job listing with an Apply button? A
       cookie banner overlay? A careers search page? A success confirmation?
     • Look at the "FORM STATUS:" line at the top of the DOM snapshot —
       how many required fields are filled vs empty?
     • Read "STILL EMPTY:" — those are your remaining work items.
     • Look at "VALIDATION ERRORS visible on page:" — the form already
       rejected something; address those first.

  2. PLAN — Given what you see, what is the ONE next action that makes the
     most progress toward "form fully filled and submitted"?
     • If no form fields visible AND an Apply link is visible → click_apply.
     • If form fields visible but FORM STATUS shows NOT READY → fill the
       NEXT unfilled required field from STILL EMPTY. **STRICT TOP-TO-BOTTOM
       ORDER** — pick the first label in STILL EMPTY and fill THAT one, not
       a later one. Sequential filling lets the form's conditional fields
       render properly and gives you a clean checklist to work through.
     • If FORM STATUS shows READY FOR SUBMIT → **STOP filling. ANALYZE the
       entire form one last time** (every required field has a value, files
       are uploaded, policy answers are correct — Country=United States,
       Disability/Veteran/Armed-Forces/Transgender=No) — and then click the
       submit button. This is the END of the flow. No more fills, no more
       scrolls. Submit.
     • **BEFORE EVERY SUBMIT, MENTALLY CHECK ALL SIX DEMOGRAPHIC QUESTIONS:**
         1. Gender identity → declared (Male/Man, Female/Woman — one only)
         2. Racial / ethnic background → South Asian (or Asian)
         3. Sexual orientation → Heterosexual
         4. Transgender → No
         5. **Disability → No** (often the LAST demographic field, easy to
            miss because it sits below veteran or vice-versa)
         6. Veteran / active member of US Armed Forces → No
       The Vercel/Greenhouse demographic block has SIX questions in this
       block. If you can recall filling only 5, the 6th is unfilled — scroll
       down and find it. Submitting with a missing demographic answer causes
       a silent server rejection, costing 3 submit attempts before the
       runner aborts. NEVER submit without verifying all six.
     • If a modal/popup blocks the form → click its close button first.
     • If history shows actions ❌FAILED on the same selector — DO NOT
       retry it; the element doesn't exist. Choose a different target.
     • If the SAME field appears in STILL EMPTY two turns in a row after
       you already filled it — the commit didn't take. Re-fill it ONCE
       with the same value, then move on; do NOT loop on it.

  3. ACT — Return exactly ONE JSON action. No prose. No markdown fences.
     No multi-step plans in one response. ONE action.

You will see the result of that action on the next turn and re-plan from
there. This is your decision loop. Own it.

Ignore noise on the page (newsletter signups, footer forms, search bars,
unrelated banners). Focus only on the application.

You respond with EXACTLY ONE action as a JSON object. No prose, no markdown.
"""

_ACTION_SCHEMA_BLOCK = """
Action schema (return ONE per turn):
  verify_page    — first step on a fillable form
  click_apply    — click any visible Apply/Apply Now/Apply for this Job link to reveal the form
  fill_field     — {selector, value, field_label}
  upload_file    — {selector, value:"resume"|"cover_letter", field_label}
  click          — {selector, click_text?}   (generic click, NOT for Apply — use click_apply)
  scroll         — {direction:"down"|"up"}
  next_step      — no params (clicks Next/Continue)
  navigate_url   — {url}  (use ONLY when current page is clearly the wrong page and you
                          can identify a better URL — last resort before abort)
  wait           — no params (short pause; use after a click that triggers async load)
  abort          — {reason}  (ONLY after you've tried click_apply + scroll + wait)
  done           — {confirmation}

DECISION POLICY — read in order:
1. First turn → if page shows form fields, return verify_page. If it shows an Apply link
   instead, return click_apply. If it's blank/loading, return wait. NEVER abort on turn 1.
   The DOM snapshot type=apply_link indicates an Apply/Apply-Now anchor link is visible
   — clicking it is REQUIRED before any form can appear. Whenever you see a type=apply_link
   entry in the DOM and you do NOT see fillable form fields (first_name, last_name, etc.),
   your next action MUST be click_apply (or click on that specific selector). Do not scroll
   away from an unclicked Apply link.
2. Before EVER aborting because "no form visible":
     (a) scroll down once — many SPAs render the form below the fold
     (b) try click_apply — even subtle "Apply" links count
     (c) wait one turn — SPAs often hydrate after DOMContentLoaded
   Only abort if all three failed across consecutive turns.
3. Career-search bars are NOT a reason to abort. They sit on top of real application
   forms on company-mirrored ATS pages all the time. Look BELOW the search bar.
4. fill_field: selector must point at the real <input>/<select>/<textarea>. For
   react-select dropdowns use the combobox input id (e.g. #react-select-X-input).
5. upload_file value is the LITERAL string "resume" or "cover_letter" — never a path.
5a. **ALWAYS UPLOAD BOTH RESUME AND COVER LETTER** if the form has a "Cover
    Letter" file input. Greenhouse forms typically have TWO file inputs side
    by side: one labeled "Resume/CV" and one labeled "Cover Letter". After
    you've issued upload_file for the resume, scroll to verify and ALSO issue
    upload_file with value="cover_letter" for the cover-letter field. Both
    files are pre-fetched and ready — failing to upload the cover letter when
    a field exists is a real omission, not optional.
6. After all visible fields filled → look for Next/Continue → next_step.
7. After last page → click Submit → done with confirmation.
8. CAPTCHA you can't solve → abort reason="captcha_wall".
9. Login wall / bot detection → abort reason="blocked".
10. Same field unfilled twice → skip and continue.
11. Prefer #id selectors > [data-*] > short class chain. Never invent selectors not in the DOM.
12. STAY IN CHARACTER as this candidate's agent. Every value you type represents them
    personally — never invent a different name, email, or location.
12a. **COUNTRY-SPECIFIC WORK AUTHORIZATION** — read the candidate identity card.
    The candidate LIVES IN THE UNITED STATES and is FULLY AUTHORIZED to work
    in the United States. Apply these rules:
      * "Are you authorized to work in the United States / US / USA?" → **YES**
        (they live in the US — pick "Yes" / "Yes, I am authorized" / the
        affirmative option). NEVER answer "No" or "I am not authorized" to a
        US work-authorization question — that is a critical error.
      * "Will you require sponsorship to work in the United States?" → **No**
        (they are already authorized; no sponsorship needed).
      * For OTHER countries (UK / Canada / EU / Australia / India / etc.):
        "Are you authorized to work in <that country>?" → use the LONG-FORM
        answer **"I am not authorized to work in <that country>"** (substitute
        the actual country name from the question — "I am not authorized to
        work in UK", "I am not authorized to work in Canada", etc.). If the
        field is a dropdown with shorter options, fall back through this
        priority list to find the closest match:
            1. "I am not authorized to work in <country>"
            2. "I am not authorized to work in the country"
            3. "I am not authorized to work in the country and need visa support"
            4. "No, I am not authorized"
            5. "No"
        For "Will you require sponsorship to work in <that country>?" → "Yes".
    Treat each country question independently; do NOT generalize. The DEFAULT
    job country here is the United States — when a work-auth question does not
    name a specific foreign country, assume it means the US and answer YES.
12b. **DROPDOWN STRICTNESS** — for any field whose snapshot shows `options=[...]`,
    the value you choose MUST be one of those exact strings. Never invent a value
    when the option list is provided — pick the closest matching option from the
    list. The runner will further snap your answer to the closest real option.
12b-demo. **DEMOGRAPHIC QUESTIONS** — use the candidate's declared answers from
    the identity card above. Map each form question to the corresponding
    candidate value:
      * "What is your gender identity?" → use the inferred gender from the
        candidate's first name (Male if name is masculine like Harmain, John,
        Ahmed, Sabih; Female if feminine like Sarah, Aisha, Jane).
      * "Race / ethnic background?" → SOUTH ASIAN preferred if the option
        list contains "South Asian" / "South Asian / Indian / Pakistani" /
        "South Asian American". If no South Asian option exists, fall back
        to the closest "Asian" option (e.g. "Asian", "Asian American",
        "Asian / Pacific Islander", "Asian (Not Hispanic or Latino)").
      * "Sexual orientation?" → Heterosexual (or the option closest to
        "Heterosexual" / "Straight").
      * "Are you a veteran?" / "Are you an active member of the US Armed Forces?"
        / "Active military?" / "Have you served in the military?" / any veteran
        OR armed-forces OR military-service question → **ALWAYS "No"**.
        Pick the option closest to: "No" / "I am not a veteran" /
        "Not a veteran" / "Not a protected veteran" / "I am not an active member
        of the US Armed Forces". This is a HARD RULE — never pick "Yes",
        never pick "Prefer not to say", never skip the field.
      * "Do you have a disability?" / "Disability status?" / any disability or
        chronic-condition question → **ALWAYS "No"**. Pick "No" / "I do not
        have a disability" / "No, I don't have a disability". Never pick "Yes"
        or "Prefer not to say".
      * "Do you identify as transgender?" → **ALWAYS "No"**.
      * "LinkedIn URL" / "LinkedIn Profile" / "LinkedIn" / any field asking
        for a LinkedIn URL → **ALWAYS "N/A"** (literally the string "N/A").
        Do NOT submit the candidate's real LinkedIn URL from the identity
        card — operator policy. This rule does NOT apply to a separate
        "Website" / "Portfolio" / "GitHub" field, only LinkedIn.
      * "Country" / "Country of residence" / "Where are you currently based?"
        / "Are you currently based in any of these countries?" / "Which country
        do you live in?" / any location-or-residence question whose options are
        a list of COUNTRY NAMES → **ALWAYS "United States"**. Pick the option
        whose text is "United States" (or "United States of America" / "USA" —
        whichever exact spelling the option list uses). The candidate lives in
        San Francisco, USA.
        **CRITICAL:** "Are you currently based in any of these countries?" is
        NOT a yes/no question — its options are country names, and you must
        pick "United States", NOT "No". Answering "No" to a country-list
        dropdown matches no option and silently fails to commit, which BLOCKS
        the entire submit. If a field's options contain country names and the
        candidate is US-based, the answer is "United States".
        Never pick another country, never pick "Prefer not to say". This rule
        does NOT apply to "Country of citizenship" or "Country code" (phone)
        fields — those follow the candidate's actual data.
    These are the candidate's declared answers — NOT defaults. Do NOT pick
    "Prefer not to say" for these six unless the option list literally does
    not contain a closer match.
    **CRITICAL FIXED ORDERING:** the demographic block on Greenhouse / Vercel /
    similar forms typically has SIX questions in this order — Gender, Race,
    Sexual Orientation, Transgender, Disability, Veteran/Armed-Forces. If
    you've filled the first four (Gender, Race, Orientation, Transgender),
    the LAST TWO (Disability + Veteran) are still waiting at the bottom.
    DO NOT scroll past the demographic block thinking you're done — find the
    last two selectors (often [id="4015781..."] for disability and
    [id="4015780..."] for veteran on Vercel-style forms) and fill BOTH with
    "No" / "I am not a veteran" BEFORE you ever consider the form done.
12c. **DROPDOWNS GET fill_field, NEVER click.** Any field with type `combobox`
    or `select` in the DOM snapshot is a DROPDOWN — its value lives behind a
    menu that opens when you interact with it. Use:
        kind="fill_field", selector=<the combobox/select id>, value=<the option text>
    The runner will OPEN the dropdown, READ the actual options that appear,
    SNAP your value to the closest real option, and CLICK it. You do NOT need
    to click first and then fill — that just opens-then-closes the menu and
    burns turns. ONE fill_field per dropdown does the whole open→read→pick→close
    sequence atomically.
12d. **READ THE QUESTION BEFORE PICKING A VALUE.** Look at the field label,
    not just the field type. "Are you authorized to work in the UK?" needs
    your UK answer (No — see identity card), NOT your home-country answer.
    "What is your gender identity?" needs the "Prefer not to say" equivalent
    from the option list. "Country of residence?" needs the candidate's
    actual country. Map each question's INTENT to the candidate's data,
    then pick the matching option string.
13a. ACTIONS MARKED ❌FAILED IN HISTORY DID NOT EXECUTE. If a selector keeps failing, it
    doesn't exist on the page — STOP retrying it. Either choose a different selector from
    the DOM snapshot, scroll to refresh the view, or move to a different action.
13c. ONLY SUBMIT WHEN THE FORM IS GENUINELY DONE. Greenhouse / Lever / Ashby forms have
    optional but visible sections at the very bottom — "U.S. Standard Demographic
    Questions" / "Voluntary Self-Identification" / "Disability Status". These look
    optional but the SUBMIT button stays disabled or returns errors if you skip them.
    Before clicking Submit, scroll all the way to the bottom (use scroll direction="down"
    until the DOM snapshot stops showing new fields), and fill every visible select /
    radio / checkbox in the demographic block — "Decline to self-identify" or "Prefer
    not to say" is the SAFE answer to every demographic question. The system blocks the
    first submit click and re-runs the loop with a full-page DOM view so you can verify
    completeness.
13ab. **READ THE "FORM STATUS:" LINE AT THE TOP OF EVERY DOM SNAPSHOT BEFORE
    EVERY ACTION.** It looks like "FORM STATUS: 12/17 required filled (70%) — NOT
    READY (5 required field(s) still empty)". This is your authoritative checklist:
      * If status says NOT READY, the next "STILL EMPTY:" list shows exactly
        which fields to fill. Pick one and fill it. DO NOT click submit yet.
        DO NOT scroll endlessly — if a field isn't visible, scroll ONCE then
        fill the one from STILL EMPTY by selector even without seeing it.
      * If status says **READY FOR SUBMIT, your next action is REQUIRED to be a
        Submit click.** Not scroll. Not wait. Not done. CLICK the Submit button:
            {"kind":"click","selector":"button[type='submit']","click_text":"Submit application"}
        The form is complete. Scrolling more wastes turns. SUBMIT IMMEDIATELY.
        If you cannot find the submit button selector, try click_text="Submit",
        then click_text="Apply", then click_text="Submit application".
    The runner will BLOCK any submit click while NOT READY and waste a turn.
    Don't waste turns. Plan from the checklist.
13abz. **NEVER SCROLL MORE THAN TWICE IN A ROW.** If two scrolls didn't reveal
    the next field, the field's selector is already in the DOM snapshot or the
    STILL EMPTY list — use fill_field with that selector directly. Scrolling
    is for revealing visual context, not for "looking around". After 5
    consecutive scrolls the runner aborts the session as STUCK.
13b. POST-SUBMIT VALIDATION RECOVERY: if you click Submit and the page does NOT navigate
    away, the form is rejecting it because of hidden required fields BELOW the fold.
    Do NOT click Submit again immediately. Instead:
      (a) Look at the DOM snapshot — if "VALIDATION ERRORS visible on page" appears at
          the top, read each error and fill the missing field it names.
      (b) If no errors visible yet, return scroll direction="down" to bring them into
          view. The runner auto-scrolls when you repeat-click Submit, so the NEXT turn's
          DOM snapshot will surface any errors that were below the fold.
      (c) Only after every visible error is addressed should you click Submit again.
    Repeat-clicking Submit without scrolling is the #1 cause of stuck loops.

EXAMPLES of valid one-action responses:
  {"kind":"verify_page","selector":"#first_name"}
  {"kind":"fill_field","selector":"#email","value":"jane@example.com","field_label":"Email"}
  {"kind":"upload_file","selector":"input[type='file']#resume","value":"resume","field_label":"Resume"}
  {"kind":"click_apply","click_text":"Apply for this Job"}
  {"kind":"scroll","direction":"down"}
  {"kind":"navigate_url","url":"https://job-boards.greenhouse.io/foo/jobs/123"}
  {"kind":"wait"}
  {"kind":"done","confirmation":"Your application has been received"}
Respond with EXACTLY ONE such object. No prose, no markdown fences, no list.
"""



_USER_TURN_TEMPLATE = """STEP {step}/{max_steps} on {platform}

FILLED (do NOT refill):
{filled_summary}

RECENT ({n_actions}):
{history}

DOM:
{dom_snapshot}

Return ONE action as a JSON object (no fences, no prose):"""


# ─────────────────────────────────────────────────────────────────────────────
# DOM snapshot helper — extracts only interactive elements to keep prompt small
# ─────────────────────────────────────────────────────────────────────────────

_DOM_SNAPSHOT_JS = """() => {
    // Bumped from 25 to 50. Long Greenhouse forms (Vercel, Stripe, Coinbase)
    // often have 30+ interactive elements counting demographic questions at
    // the bottom. With cap=25 those were getting silently truncated — the AI
    // never saw the demographic fields and clicked Submit prematurely.
    const MAX_FIELDS = 50;
    const MAX_ERRORS = 10;

    // Pass 1 — collect validation-error messages so the AI can see what the
    // form rejected after a submit click. These often live BELOW the fold and
    // are the #1 reason a fill-and-submit run gets stuck looping on Submit.
    const errorOut = [];
    const errSelectors = [
        '.field-error', '.error-message', '.error', '[role="alert"]',
        '[aria-invalid="true"]', '[class*="invalid"]', '[class*="errorMessage"]',
        '.help-block.error', '.has-error', '.form-error', '.input-error',
    ];
    const seenErrTexts = new Set();
    errSelectors.forEach(sel => {
        document.querySelectorAll(sel).forEach(el => {
            if (errorOut.length >= MAX_ERRORS) return;
            if (inConsentBanner(el)) return;
            const txt = (el.textContent || el.getAttribute('aria-label') || '').trim();
            if (!txt) return;
            // Filter out aria-invalid='true' on visible empty inputs without a message
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return;
            if (txt.length < 3 || txt.length > 200) return;
            if (seenErrTexts.has(txt)) return;
            seenErrTexts.add(txt);
            // Try to associate the error with the nearest form field
            let assocLabel = '';
            const formGroup = el.closest('.form-group, .field, .input-group, [class*="field"]');
            if (formGroup) {
                const lbl = formGroup.querySelector('label, legend');
                if (lbl) assocLabel = (lbl.textContent || '').trim().slice(0, 60);
            }
            errorOut.push({ message: txt.slice(0, 160), field: assocLabel });
        });
    });

    const out = [];
    const seen = new Set();
    const skip_types = new Set(['hidden', 'submit', 'button', 'image', 'reset', 'search']);

    // Skip phone-widget containers
    function inPhoneWidget(el) {
        return !!el.closest('.iti, .iti__country-list, .iti--container');
    }

    // Skip careers-site search bars. On mirror careers SPAs (Elastic, etc.)
    // the page header has search inputs that aren't application fields. The
    // AI was filling email into them and getting stuck.
    function isSearchInput(el) {
        const name = (el.name || '').toLowerCase();
        const id = (el.id || '').toLowerCase();
        const ph = (el.placeholder || '').toLowerCase();
        const al = (el.getAttribute('aria-label') || '').toLowerCase();
        if (name === 'search' || name === 'q' || name === 'query') return true;
        if (id === 'search' || id.startsWith('search-')) return true;
        if (/\bsearch\b/i.test(ph) && !/\b(email|name|phone)\b/i.test(ph)) return true;
        if (/\bsearch\b/i.test(al)) return true;
        if (id === 'outlined-basic' && !el.getAttribute('aria-label')) return true;
        if (el.closest('nav, header, [role="search"], [class*="search-bar"], [class*="searchbox"]')) return true;
        return false;
    }

    // Skip cookie-consent / privacy-banner / vendor-list controls. These overlays
    // commonly sit on top of the actual application form (OneTrust, Cookiebot,
    // Osano, TrustArc, etc.) and if we don't filter them the AI thinks every
    // "accept vendor X" checkbox is a job-application field. The id/class
    // patterns below cover the major CMP vendors used by Greenhouse/Lever/Ashby
    // host sites in 2024-2026.
    function inConsentBanner(el) {
        return !!el.closest(
            '#onetrust-banner-sdk, #onetrust-pc-sdk, #ot-pc-content, '
          + '#onetrust-consent-sdk, .onetrust-pc-dark-filter, '
          + '[id*="cookie"], [class*="cookie-banner"], [class*="cookie-consent"], '
          + '[id*="consent-banner"], [id*="consent-modal"], '
          + '[class*="consent-banner"], [class*="consent-modal"], '
          + '#CybotCookiebotDialog, '   // Cookiebot
          + '[id^="cookie"], [class^="cookie"], '
          + '.cmp-banner, .cmp-modal, [class*="cmp-"], '
          + '#truste-consent-track, [id*="truste"], '   // TrustArc
          + '#osano-cm-window, [class*="osano-"], '     // Osano
          + '[aria-label*="cookie" i], [aria-label*="consent" i], '
          + '[role="dialog"][aria-label*="privacy" i]'
        );
    }

    // Resolve a human label from nearby DOM
    function labelFor(el) {
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim().slice(0, 80);
        const id = el.id;
        if (id) {
            const lbl = document.querySelector('label[for="' + id + '"]');
            if (lbl) return lbl.textContent.trim().slice(0, 80);
        }
        let p = el.parentElement;
        for (let i = 0; i < 5 && p; i++) {
            const lbl = p.querySelector(':scope > label, :scope > legend, :scope > span.label');
            if (lbl && !lbl.contains(el)) return lbl.textContent.trim().slice(0, 80);
            p = p.parentElement;
        }
        return el.placeholder || el.name || el.id || '?';
    }

    // Native inputs / selects / textareas
    document.querySelectorAll('input, select, textarea').forEach(el => {
        if (out.length >= MAX_FIELDS) return;
        const type = (el.type || 'text').toLowerCase();
        if (skip_types.has(type)) return;
        if (inPhoneWidget(el)) return;
        if (inConsentBanner(el)) return;
        if (isSearchInput(el)) return;
        const style = window.getComputedStyle(el);
        if (style.display === 'none') return;
        if (style.visibility === 'hidden' && type !== 'radio' && type !== 'checkbox') return;
        // Build selector
        const sel = el.id ? '#' + el.id : (el.name ? el.tagName.toLowerCase() + '[name="' + el.name + '"]' : el.tagName.toLowerCase());
        if (seen.has(sel)) return;
        seen.add(sel);
        const entry = { sel, type, label: labelFor(el), required: el.required || el.getAttribute('aria-required') === 'true' };
        if (el.tagName === 'SELECT') {
            const allOpts = Array.from(el.options).filter(o => o.value).map(o => o.text.trim());
            let opts = allOpts.slice(0, 15);
            // Country dropdowns hold ~200 options; the first 15 alphabetically
            // (Afghanistan, Albania, ...) never include "United States", so the
            // AI couldn't see it was a valid choice and fell back to a wrong
            // answer. If a "United States" option exists but isn't in the
            // preview, force it in so the AI knows to pick it.
            const usOpt = allOpts.find(o => /^(united states( of america)?|usa)$/i.test(o));
            if (usOpt && !opts.includes(usOpt)) {
                opts = [usOpt, ...opts.slice(0, 14)];
            }
            if (allOpts.length > opts.length) {
                entry.options = opts;
                entry.options_note = `(${allOpts.length} total options — list truncated; pick the exact matching one)`;
            } else if (opts.length) {
                entry.options = opts;
            }
        }
        out.push(entry);
    });

    // Custom dropdowns (react-select combobox inputs)
    document.querySelectorAll('[role="combobox"]').forEach(el => {
        if (out.length >= MAX_FIELDS) return;
        if (inPhoneWidget(el)) return;
        if (inConsentBanner(el)) return;
        const sel = el.id ? '#' + el.id : null;
        if (!sel || seen.has(sel)) return;
        seen.add(sel);
        out.push({ sel, type: 'combobox', label: labelFor(el), required: false });
    });

    // Visible buttons (Next / Submit / Apply)
    document.querySelectorAll('button, input[type="submit"], input[type="button"]').forEach(el => {
        if (out.length >= MAX_FIELDS) return;
        if (inConsentBanner(el)) return;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return;
        const text = el.textContent.trim() || el.value || '';
        if (!text) return;
        const sel = el.id ? '#' + el.id : 'button:has-text("' + text.slice(0, 40) + '")';
        if (seen.has(sel)) return;
        seen.add(sel);
        out.push({ sel, type: 'button', label: text.slice(0, 80) });
    });

    // Anchor links that ACT as application controls — Greenhouse's Apply button
    // is `<a id="apply_button">Apply for this Job</a>` (not a <button>!). Without
    // surfacing these the AI can never "see" the Apply link in its DOM view and
    // ends up wandering the page wondering why no form appears.
    document.querySelectorAll('a').forEach(el => {
        if (out.length >= MAX_FIELDS) return;
        if (inConsentBanner(el)) return;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return;
        const text = (el.textContent || '').trim();
        if (!text) return;
        // Only include anchors whose text strongly suggests an application
        // action — avoid flooding the snapshot with every nav/footer link.
        if (!/\b(apply|submit your|view application|start application)\b/i.test(text)) return;
        const sel = el.id ? '#' + el.id : 'a:has-text("' + text.slice(0, 40) + '")';
        if (seen.has(sel)) return;
        seen.add(sel);
        out.push({ sel, type: 'apply_link', label: text.slice(0, 80) });
    });

    // ── Form Status checklist ────────────────────────────────────────────
    // Explicit summary of required-field completion. The AI uses this to
    // know whether the form is ready for submit. Without this signal the AI
    // tends to repeat-click submit before the demographic section is done.
    const formStatus = { totalRequired: 0, filled: 0, unfilled: [] };
    document.querySelectorAll(
        'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]):not([type=reset]), '
      + 'select, textarea, [role="combobox"]'
    ).forEach(el => {
        if (inConsentBanner(el)) return;
        if (inPhoneWidget(el)) return;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return;
        // Resolve the field label early so we can detect demographic questions.
        let fsLabel = el.getAttribute('aria-label') || '';
        if (!fsLabel && el.id) {
            // Try direct association by id, plus the react-select pattern
            // where the visible <input> id is `<base>--input` but the
            // associated label points at `<base>`.
            const lbl = document.querySelector('label[for="' + el.id + '"]');
            if (lbl) fsLabel = (lbl.textContent || '').trim();
            if (!fsLabel && el.id.endsWith('--input')) {
                const base = el.id.slice(0, -7);
                const lbl2 = document.querySelector('label[for="' + base + '"]');
                if (lbl2) fsLabel = (lbl2.textContent || '').trim();
            }
        }
        if (!fsLabel) {
            const fset = el.closest('fieldset');
            if (fset) { const lg = fset.querySelector('legend'); if (lg) fsLabel = (lg.textContent || '').trim(); }
        }
        // Walk up parent containers looking for a question label. Greenhouse
        // wraps each application question in a div whose own <label> sits
        // BEFORE the input, not associated via `for=`. Without this the
        // disability/veteran/transgender question text is invisible to us,
        // they don't match the demographic regex, FORM STATUS misses them,
        // and the AI submits before answering them.
        if (!fsLabel) {
            let p = el.parentElement;
            for (let i = 0; i < 6 && p && !fsLabel; i++) {
                const lbl = p.querySelector(':scope > label, :scope > .application-question__label, :scope > .question-label, :scope > .field-label, :scope > .form-question, :scope > div > label');
                if (lbl) fsLabel = (lbl.textContent || '').trim();
                p = p.parentElement;
            }
        }
        if (!fsLabel) fsLabel = el.name || el.id || '';
        // Demographic / voluntary-self-identification questions are NOT marked
        // `required` in the DOM on Greenhouse/Vercel forms, but the operator
        // policy is to ALWAYS answer them (gender/race/orientation = declared,
        // disability/veteran/transgender = No). Treat them as must-fill so the
        // AI doesn't submit before completing the demographic block.
        const DEMOGRAPHIC_RE = /\\b(gender identity|gender|racial|race|ethnic|sexual orientation|transgender|disabilit|veteran|armed forces|hispanic|latino)\\b/i;
        // Fallback: if label resolution missed the question text (Greenhouse's
        // disability dropdown structure can defeat the label walk), scan the
        // text content of the nearest "question container" for the same
        // keywords. This catches disability/veteran fields even when their
        // <label> isn't directly accessible from the input.
        let isDemographic = DEMOGRAPHIC_RE.test(fsLabel);
        if (!isDemographic) {
            const container = el.closest(
                '.application-question, .form-question, .question, '
              + '[class*="question"], [class*="Question"], fieldset, '
              + '[data-question-id], .field, [class*="field-"]'
            );
            if (container) {
                const txt = (container.textContent || '').slice(0, 400);
                if (DEMOGRAPHIC_RE.test(txt)) isDemographic = true;
            }
        }
        const isRequired = el.required
            || el.getAttribute('aria-required') === 'true'
            || (el.closest('[class*="required"]') !== null)
            || isDemographic;
        if (!isRequired) return;
        // Mirror the pre-submit gate's phantom-field filter — these
        // computations MUST agree, otherwise the AI sees "NOT READY"
        // (FORM STATUS) but the gate would allow submit (mismatch =
        // scroll-loop-until-STUCK). Skip:
        //  • file inputs — .value is unreliable after Playwright upload
        //  • fields with no id, no name, AND no label — unaddressable
        //    phantoms (honeypots, react-select internal proxies, etc.)
        if (el.type === 'file') return;
        const hasIdent_fs = !!(el.id || el.name);
        const hasLabel_fs = !!(
            el.getAttribute('aria-label')
            || (el.id && document.querySelector('label[for="' + el.id + '"]'))
            || el.closest('fieldset')?.querySelector('legend')
        );
        if (!hasIdent_fs && !hasLabel_fs) return;
        formStatus.totalRequired += 1;
        let val = '';
        if (el.tagName === 'SELECT') {
            val = el.value && el.options[el.selectedIndex]
                  ? el.options[el.selectedIndex].text : '';
        } else if (el.type === 'checkbox' || el.type === 'radio') {
            if (el.name) {
                const grp = document.querySelectorAll(`[name="${el.name}"]:checked`);
                val = grp.length ? 'checked' : '';
            } else { val = el.checked ? 'checked' : ''; }
        } else {
            val = (el.value || '').trim();
        }
        if (!val && el.getAttribute('role') === 'combobox') {
            const ctrl = el.closest('.select__control, .react-select__control');
            if (ctrl) {
                // Single-select shows .select__single-value; "mark all that
                // apply" multi-selects (the Vercel/Greenhouse demographic
                // fields) show .select__multi-value chips instead. Read both,
                // otherwise a filled demographic dropdown looks empty and the
                // AI loops forever waiting for it.
                const sv = ctrl.querySelector('.select__single-value, .react-select__single-value');
                if (sv) val = sv.textContent.trim();
                if (!val) {
                    const mv = ctrl.querySelector('.select__multi-value, .react-select__multi-value, .select__multi-value__label');
                    if (mv) val = mv.textContent.trim();
                }
            }
        }
        if (val) {
            formStatus.filled += 1;
        } else {
            if (formStatus.unfilled.length < 15) {
                let label = el.getAttribute('aria-label') || '';
                if (!label && el.id) {
                    const lbl = document.querySelector(`label[for="${el.id}"]`);
                    if (lbl) label = (lbl.textContent || '').trim();
                }
                if (!label) label = el.name || el.id || '?';
                formStatus.unfilled.push(label.slice(0, 80));
            }
        }
    });

    return { fields: out, errors: errorOut, formStatus: formStatus };
}"""


async def _dom_snapshot(page: Page, frame: Optional[Frame] = None, is_iframe_mode: bool = False) -> str:
    # If we are in iframe mode but frame is None (e.g. detached mid-loop), we MUST NOT fall back to page,
    # otherwise we capture the top-level site's DOM instead of the form.
    if is_iframe_mode and not frame:
        return "(iframe detached or reloading)"
    ctx = frame or page
    try:
        snap = await ctx.evaluate(_DOM_SNAPSHOT_JS)
        # Back-compat: older shape returned a flat list; new shape is {fields, errors, formStatus}
        if isinstance(snap, list):
            fields, errors, form_status = snap, [], None
        else:
            fields = (snap or {}).get("fields") or []
            errors = (snap or {}).get("errors") or []
            form_status = (snap or {}).get("formStatus") or None
        lines = []
        # Form status block — the explicit "ReAct planning" signal the AI uses
        # to decide whether the form is ready for submit. Renders as a tiny
        # checklist with unfilled labels so the AI knows exactly what's left.
        if form_status and form_status.get("totalRequired", 0) > 0:
            total = form_status["totalRequired"]
            filled = form_status.get("filled", 0)
            unfilled = form_status.get("unfilled", []) or []
            pct = int(100 * filled / total) if total else 0
            verdict = "READY FOR SUBMIT" if filled == total else f"NOT READY ({total - filled} required field(s) still empty)"
            lines.append(
                f"FORM STATUS: {filled}/{total} required filled ({pct}%) — {verdict}"
            )
            if unfilled:
                lines.append("STILL EMPTY (fill these BEFORE clicking submit):")
                for lbl in unfilled[:10]:
                    lines.append(f"  ○ {lbl}")
            lines.append("")
        # Validation errors next — these tell the AI what just got rejected
        if errors:
            lines.append("VALIDATION ERRORS visible on page (form rejected last submit):")
            for e in errors:
                fld = f" [{e.get('field','')}]" if e.get("field") else ""
                lines.append(f"  ⚠ {e.get('message','?')}{fld}")
            lines.append("")
        for f in fields:
            req = " [required]" if f.get("required") else ""
            opts = f" options={f['options']}" if f.get("options") else ""
            note = f" {f['options_note']}" if f.get("options_note") else ""
            lines.append(f"  {f['sel']} | {f['type']} | {f.get('label','?')}{req}{opts}{note}")
        return "\n".join(lines) or "(no interactive elements found)"
    except Exception as exc:
        logger.warning(f"[AgentLoop] dom_snapshot failed: {exc}")
        return "(dom snapshot unavailable)"


async def _dom_hash(ctx) -> str:
    """SHA1 of the body inner HTML and input values — used to detect DOM changes between steps."""
    try:
        html = await ctx.evaluate("""() => {
            let html = document.body.innerHTML;
            let vals = Array.from(document.querySelectorAll('input, select, textarea')).map(e => e.value).join('|');
            return html + vals;
        }""")
        return hashlib.sha1(html.encode()).hexdigest()[:16]
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Action parser
# ─────────────────────────────────────────────────────────────────────────────

_VALID_KINDS: set[str] = {
    "verify_page", "fill_field", "upload_file", "click", "click_apply",
    "scroll", "next_step", "navigate_url", "wait", "abort", "done",
}


def _parse_action(raw: Dict[str, Any], step: int) -> Optional[AgentAction]:
    kind = str(raw.get("kind") or "").strip()
    if kind not in _VALID_KINDS:
        logger.warning(f"[AgentLoop] step={step} unknown action kind={kind!r}")
        return None
    return AgentAction(
        kind=kind,  # type: ignore[arg-type]
        selector=raw.get("selector") or None,
        value=raw.get("value") or None,
        field_label=raw.get("field_label") or None,
        direction=raw.get("direction") or None,
        reason=raw.get("reason") or None,
        confirmation=raw.get("confirmation") or None,
        click_text=raw.get("click_text") or None,
        url=raw.get("url") or None,
        step=step,
        raw=raw,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Action executor
# ─────────────────────────────────────────────────────────────────────────────

def _pick_closest_option(proposed: str, options: List[str]) -> str:
    """Map an LLM-proposed value to the closest item in the actual options list.

    Strategy (cheap, no extra LLM call):
      1. Exact case-insensitive match wins
      2. "Decline / Prefer not / Rather not" intent → first matching option
      3. Substring match either direction
      4. First-word match (e.g. proposed "Yes" → "Yes — authorized")
      5. Fall back to the proposed value (caller's select_option will error
         out cleanly and we'll surface that as a fill failure)
    """
    if not options:
        return proposed
    p = (proposed or "").strip().lower()
    if not p:
        return proposed
    # 1. Exact
    for o in options:
        if o.strip().lower() == p:
            return o
    # NEGATIVE intent — check FIRST so "Disagree" doesn't fall into the
    # affirm branch via substring 'agree'.
    negate_patterns = ("decline", "prefer not", "rather not", "don't wish",
                       "do not wish", "wish not", "don't want", "do not want",
                       "disagree", "do not agree", "don't agree",
                       "do not acknowledge", "don't acknowledge",
                       "i don't", "i do not")
    is_negative = (p.strip() == "no"
                   or p.strip().startswith(("no ", "no,", "no."))
                   or any(n in p for n in negate_patterns))
    if is_negative:
        # Prefer EXPLICIT negative options over decline-style ones first —
        # "No, I do not have a disability" should win over "I do not wish to
        # answer" when the candidate's answer is genuinely "No". Only fall
        # back to decline-style if no explicit-No option exists.
        # Priority 1: options that LITERALLY START with "No"
        for o in options:
            ol = o.strip().lower()
            if ol.startswith(("no ", "no,", "no.")):
                return o
        # Priority 2: explicit negative phrasing (disagree, do not agree, etc.)
        for o in options:
            ol = o.lower()
            if any(n in ol for n in ("disagree", "do not agree", "don't agree",
                                       "do not acknowledge", "don't acknowledge")):
                return o
        # Priority 3: ONLY for "decline-intent" candidates (not bare "No"),
        # match decline-style options as the safe fallback.
        is_explicit_decline = any(d in p for d in
            ("decline", "prefer not", "rather not", "don't wish", "do not wish",
             "wish not", "i don't", "i do not"))
        if is_explicit_decline:
            for o in options:
                ol = o.lower()
                if any(d in ol for d in ("decline", "prefer not", "rather not",
                                           "don't wish", "do not wish")):
                    return o
        # Last resort: any option containing "do not" / "don't" / "no"
        for o in options:
            ol = o.lower()
            if any(n in ol for n in ("i do not", "i don't")):
                return o
    # AFFIRMATIVE-acknowledgment intent (only after negative ruled out)
    affirm_patterns = ("yes", "agree", "acknowledge", "accept", "confirm",
                       "i have read", "i understand", "i certify")
    if not is_negative and any(a in p for a in affirm_patterns):
        # Prefer options that contain affirmative keywords
        positives = [o for o in options
                     if any(kw in o.lower() for kw in
                            ("acknowledge", "agree", "accept", "confirm",
                             "i have read", "i understand", "i certify",
                             "consent"))
                     # Exclude negative phrasings even if they contain "agree"
                     and not any(neg in o.lower() for neg in
                                 ("disagree", "do not", "don't", "decline"))]
        if positives:
            return positives[0]
        # Fall back: any option starting with "yes" / "i " (not negative)
        for o in options:
            ol = o.lower().strip()
            if ol.startswith(("yes", "i ", "i,")):
                if any(neg in ol for neg in ("decline", "disagree", "do not", "don't")):
                    continue
                return o
    # 2d. Demographic-specific intent mapping. The AI may propose short forms
    #     ("Asian", "Heterosexual", "Male") while the option list uses
    #     long phrasings ("Asian (Not Hispanic or Latino)", "Heterosexual /
    #     Straight", "Male — Cisgender"). Word-boundary match on the
    #     proposed term wins.
    # Specific South-Asian intent → check FIRST so it doesn't get caught by
    # the broader "asian" → any-asian match below.
    if "south asian" in p:
        for o in options:
            if "south asian" in o.lower():
                return o
        # Fall back to plain Asian if no South Asian option exists
        for o in options:
            if "asian" in o.lower() and "not" not in o.lower():
                return o
    demographic_kw_map = {
        "asian": ("asian",),
        "white": ("white", "caucasian"),
        "black": ("black", "african american", "african-american"),
        "hispanic": ("hispanic", "latino", "latina", "latinx", "latine"),
        "native": ("native", "american indian", "alaska"),
        "pacific": ("pacific islander", "native hawaiian"),
        "two or more": ("two or more", "multi-racial", "multiracial", "mixed"),
        "heterosexual": ("heterosexual", "straight"),
        "male": ("male", "man"),
        "female": ("female", "woman"),
        "veteran": ("veteran",),
        "not a veteran": ("not a veteran", "i am not a veteran", "non-veteran"),
    }
    for proposed_kw, option_kws in demographic_kw_map.items():
        if proposed_kw in p:
            for o in options:
                ol = o.lower()
                if any(kw in ol for kw in option_kws):
                    # Avoid negation when the proposed wasn't negated
                    if "not" not in p and "non-" not in p and "do not" not in p:
                        if any(neg in ol for neg in (" not ", "non-", "do not")):
                            continue
                    return o
    # 3. Substring either direction
    for o in options:
        ol = o.strip().lower()
        if p in ol or ol in p:
            return o
    # 4. First-word match
    first_word = p.split()[0] if p.split() else p
    for o in options:
        if o.strip().lower().split()[:1] == [first_word]:
            return o
    # 5. Give up — caller's select_option will error out
    return proposed


def _sanitize_selector(sel: Optional[str]) -> Optional[str]:
    """Normalize selectors the LLM produces so Playwright can parse them.

    The specific failure we hit on Vercel: Greenhouse's demographic-field IDs
    are pure digits like ``4015790004``. The LLM generates ``#4015790004`` —
    which is INVALID CSS (selector tokens can't start with a digit) and
    Playwright throws ``SyntaxError: Failed to execute 'querySelectorAll'``.

    Convert any ``#<digit>...`` form to the equivalent ``[id="..."]`` form
    which IS valid CSS and matches the same element.
    """
    if not sel:
        return sel
    s = sel.strip()
    # #1234abc → [id="1234abc"]   (only when the first char after # is a digit)
    if len(s) >= 2 and s[0] == "#" and s[1].isdigit():
        # Stop at the first selector-combinator char so we don't swallow trailing
        # bits like ".foo" or " >.bar". Conservative: take while alphanumeric / _ / -.
        body = s[1:]
        cut = 0
        for ch in body:
            if ch.isalnum() or ch in ("_", "-"):
                cut += 1
            else:
                break
        ident, rest = body[:cut], body[cut:]
        return f'[id="{ident}"]' + rest
    return sel


async def _execute_action(
    action: AgentAction,
    page: Page,
    frame: Optional[Frame],
    resume_path: Optional[str],
    cover_letter_path: Optional[str],
) -> bool:
    """Execute one action. Returns True if Playwright succeeded."""
    ctx = frame or page
    kind = action.kind
    # Sanitize the LLM-provided selector — fixes the #<digit>... case that
    # Playwright rejects as invalid CSS. Idempotent for already-valid selectors.
    if action.selector:
        sanitized = _sanitize_selector(action.selector)
        if sanitized != action.selector:
            logger.debug(f"[AgentLoop] selector sanitized: {action.selector!r} → {sanitized!r}")
            action.selector = sanitized

    if kind == "verify_page":
        await asyncio.sleep(0.3)
        return True

    if kind == "wait":
        # Longer wait — used when AI suspects a SPA is still hydrating
        await asyncio.sleep(2.0)
        return True

    if kind == "navigate_url":
        target_url = action.url
        if not target_url:
            logger.warning("[AgentLoop] navigate_url: no url provided")
            return False
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(2.0)
            logger.info(f"[AgentLoop] navigated to {target_url!r}")
            return True
        except Exception as exc:
            logger.warning(f"[AgentLoop] navigate_url failed: {exc}")
            return False

    if kind == "click_apply":
        # AI-guided: try the AI's selector + a small set of universal Apply patterns.
        candidates: List[str] = []
        if action.selector:
            candidates.append(action.selector)
        if action.click_text:
            candidates.append(f"a:has-text('{action.click_text}')")
            candidates.append(f"button:has-text('{action.click_text}')")
        candidates += [
            "a:has-text('Apply for this Job')",
            "a:has-text('Apply for This Job')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply Now')",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "a[href*='#app']",
            "a[href*='/apply']",
            "[data-qa='btn-apply']",
            "#apply_button",
            "#nav_apply",
        ]
        for s in candidates:
            try:
                loc = ctx.locator(s).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=6000)
                    await asyncio.sleep(2.5)
                    logger.info(f"[AgentLoop] click_apply succeeded via {s!r}")
                    return True
            except Exception:
                continue
        logger.warning("[AgentLoop] click_apply: no Apply element matched any selector")
        return False

    if kind == "scroll":
        direction = action.direction or "down"
        delta = 600 if direction == "down" else -600
        await page.evaluate(f"window.scrollBy(0, {delta})")
        await asyncio.sleep(0.3)
        return True

    if kind == "next_step":
        # Try common next/continue selectors
        candidates = [
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button:has-text('Next Step')",
            "[data-qa='btn-next']",
            "button[type='button']:has-text('Next')",
        ]
        for sel in candidates:
            try:
                loc = ctx.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=6000)
                    await asyncio.sleep(1.0)
                    return True
            except Exception:
                continue
        logger.warning("[AgentLoop] next_step: no Next button found")
        return False

    if kind == "click":
        sel = action.selector
        text = action.click_text
        tried: List[str] = []
        if sel:
            tried.append(sel)
        if text:
            tried.append(f"button:has-text('{text}')")
            tried.append(f":has-text('{text}')")
        for s in tried:
            try:
                loc = ctx.locator(s).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=6000)
                    await asyncio.sleep(0.8)
                    return True
            except Exception:
                continue
        logger.warning(f"[AgentLoop] click: no element matched sel={sel!r} text={text!r}")
        return False

    if kind == "upload_file":
        file_key = (action.value or "").lower()
        path = resume_path if "cover" not in file_key else cover_letter_path
        if not path:
            logger.warning(f"[AgentLoop] upload_file: no path for key={file_key!r}")
            return False

        # Greenhouse-style upload widgets render an "Attach / Dropbox / Google
        # Drive / Enter manually" menu instead of a plain <input type=file>.
        # The AI often picks the visible button selector (e.g. #resume which
        # is actually a <div>), which fails. Build a CANDIDATE LIST starting
        # with whatever the AI proposed, then expand to common Greenhouse and
        # generic file-input patterns. Pick the first one that's actually a
        # file input or accepts set_input_files.
        candidates: List[str] = []
        if action.selector:
            # Try literal selector first
            candidates.append(action.selector)
            # If AI gave us a widget id like #resume, try the file input INSIDE it
            candidates.append(f"{action.selector} input[type='file']")

        # Field-label-aware lookups (resume vs cover_letter)
        kind_word = "cover" if "cover" in file_key else "resume"
        candidates += [
            # Greenhouse generic patterns
            f"input[type='file'][id*='{kind_word}']",
            f"input[type='file'][name*='{kind_word}']",
            f"input[type='file'][aria-label*='{kind_word}' i]",
            # Any visible/hidden file input near a matching label
            "input[type='file']",
        ]

        # Dedupe while keeping order
        tried_set = set()
        ordered: List[str] = []
        for c in candidates:
            if c and c not in tried_set:
                tried_set.add(c)
                ordered.append(c)

        for sel in ordered:
            try:
                loc = ctx.locator(sel).first
                if await loc.count() == 0:
                    continue
                # set_input_files works on hidden inputs too — no visibility check
                await loc.set_input_files(path, timeout=8000)
                await asyncio.sleep(0.8)
                logger.info(f"[AgentLoop] upload_file succeeded via {sel!r} path={path!r}")
                return True
            except Exception as exc:
                logger.debug(f"[AgentLoop] upload_file try {sel!r}: {exc}")
                continue
        logger.warning(f"[AgentLoop] upload_file: no working file input found "
                       f"for key={file_key!r} (tried {len(ordered)} selectors)")
        return False

    if kind == "fill_field":
        sel = action.selector
        value = action.value or ""
        if not sel:
            logger.warning("[AgentLoop] fill_field: no selector")
            return False

        # ── LinkedIn-URL hard override (operator policy) ────────────────────
        # The system prompt tells the AI to answer "N/A" on LinkedIn-URL
        # fields, but Opus has repeatedly emitted the real URL anyway (likely
        # pulling it from resume/cover-letter context that's also in its
        # prompt). Defense-in-depth: at the executor level, if the field
        # label or selector says "LinkedIn" AND the value looks like a URL
        # or a long string, rewrite it to "N/A" before touching the page.
        # Carve-outs: "Website", "Portfolio", "GitHub" — not affected.
        _lbl = (action.field_label or "").lower()
        _sel_l = (sel or "").lower()
        _is_linkedin_field = (
            ("linkedin" in _lbl or "linkedin" in _sel_l)
            and "website" not in _lbl
            and "portfolio" not in _lbl
            and "github" not in _lbl
        )
        if _is_linkedin_field:
            if value.strip().upper() != "N/A":
                logger.info(
                    f"[AgentLoop] LinkedIn policy override: AI proposed {value[:50]!r} "
                    f"for {action.field_label!r}, forcing 'N/A'"
                )
                value = "N/A"
                action.value = "N/A"
        try:
            loc = ctx.locator(sel).first
            if await loc.count() == 0:
                logger.warning(f"[AgentLoop] fill_field: selector not found: {sel!r}")
                return False
            field_type = await loc.evaluate("el => el.type || el.tagName.toLowerCase()")
            field_type = (field_type or "text").lower()

            if field_type == "file":
                # Redirect to upload logic
                path = resume_path
                if "cover" in (action.field_label or "").lower():
                    path = cover_letter_path
                if not path:
                    return False
                await loc.set_input_files(path, timeout=10000)
                return True

            if field_type in ("checkbox",):
                if value.lower() in ("yes", "true", "1", "on", "agree"):
                    await loc.check(force=True, timeout=5000)
                return True

            if field_type == "radio":
                await loc.check(force=True, timeout=5000)
                return True

            if field_type == "select-one" or field_type == "select":
                # Strict-options enforcement: read the actual option list off
                # the DOM and pick the closest match instead of blindly trying
                # the AI's value. Prevents invalid-value failures and mismatch
                # cases like AI says "Decline" but option is "Prefer not to say".
                try:
                    options = await loc.evaluate(
                        "el => Array.from(el.options).filter(o => o.value).map(o => o.text.trim())"
                    )
                except Exception:
                    options = []
                target = _pick_closest_option(value, options) if options else value
                if target != value:
                    logger.info(
                        f"[AgentLoop] dropdown enforcement: AI proposed {value!r}, "
                        f"snapping to closest option {target!r} (of {len(options)})"
                    )
                try:
                    await loc.select_option(label=target, timeout=5000)
                    return True
                except Exception:
                    try:
                        await loc.select_option(value=target, timeout=3000)
                        return True
                    except Exception:
                        logger.warning(
                            f"[AgentLoop] select_option failed for {target!r}; "
                            f"options were {options[:8]}"
                        )
                        return False

            # combobox (react-select, custom dropdowns) — OPEN → READ ACTUAL
            # OPTIONS → PICK BEST MATCH from those options → CLICK it.
            # Previously we just typed the AI's value and prayed the menu had
            # a matching option. Now we enumerate what's actually visible and
            # snap to the closest real option, eliminating mismatches like
            # AI says "Decline" but option list has "Prefer not to say".
            if field_type == "combobox" or "combobox" in (await loc.get_attribute("role") or ""):
                # 1. Open the dropdown
                try:
                    await loc.click(timeout=5000)
                except Exception:
                    pass
                await asyncio.sleep(0.4)
                # 2. Read the actual options now visible in the dropdown menu.
                #    We collect from common react-select / aria patterns.
                try:
                    available_options = await ctx.evaluate("""() => {
                        const out = [];
                        const seen = new Set();
                        document.querySelectorAll(
                            '.select__menu .select__option, '
                          + '.react-select__menu .react-select__option, '
                          + '[role="listbox"] [role="option"], '
                          + '.select__menu [role="option"]'
                        ).forEach(el => {
                            if (el.offsetParent === null) return;
                            const t = (el.textContent || '').trim();
                            if (!t || seen.has(t)) return;
                            seen.add(t);
                            out.push(t);
                        });
                        return out;
                    }""")
                except Exception:
                    available_options = []

                logger.info(
                    f"[AgentLoop] combobox opened with {len(available_options or [])} option(s); "
                    f"AI wanted {value!r}; first few options: {(available_options or [])[:6]}"
                )

                # 3. Pick best match from the actual options
                target_text = (
                    _pick_closest_option(value, available_options)
                    if available_options else value
                )
                if available_options and target_text != value:
                    logger.info(
                        f"[AgentLoop] combobox option-snap: AI {value!r} → "
                        f"snapped to actual menu option {target_text!r}"
                    )

                # 4. Click the chosen option. Try exact-text match first.
                escaped = target_text.replace("'", "\\'")
                option_sel = (
                    f".select__menu .select__option:text-is('{escaped}'), "
                    f".react-select__menu .react-select__option:text-is('{escaped}'), "
                    f"[role='listbox'] [role='option']:text-is('{escaped}')"
                )
                try:
                    opt = ctx.locator(option_sel).first
                    if await opt.count() > 0:
                        await opt.click(timeout=3000)
                        await asyncio.sleep(0.4)
                        logger.info(f"[AgentLoop] combobox clicked option {target_text!r} (exact)")
                        return True
                except Exception:
                    pass
                # Fallback: EXACT (case-insensitive, trimmed) match via JS.
                # We deliberately do NOT use Playwright :has-text() here — it's a
                # substring match, so targeting "Man" would also click "Woman"
                # (contains "man") and on multi-select demographic fields that
                # caused BOTH Man and Woman to get selected. Exact equality only.
                try:
                    clicked = await ctx.evaluate(
                        """(target) => {
                            const want = target.trim().toLowerCase();
                            const opts = document.querySelectorAll(
                                '.select__menu .select__option, '
                              + '.react-select__menu .react-select__option, '
                              + '[role="listbox"] [role="option"], '
                              + '.select__menu [role="option"]'
                            );
                            for (const el of opts) {
                                if ((el.textContent || '').trim().toLowerCase() === want) {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }""",
                        target_text,
                    )
                    if clicked:
                        await asyncio.sleep(0.4)
                        logger.info(f"[AgentLoop] combobox clicked option {target_text!r} (exact-js)")
                        return True
                except Exception:
                    pass
                # Last resort: type-then-Enter (legacy behavior)
                try:
                    await loc.fill("")
                    await loc.type(target_text, delay=40)
                    await asyncio.sleep(0.4)
                    await page.keyboard.press("Enter")
                    logger.info(f"[AgentLoop] combobox fallback type+Enter for {target_text!r}")
                    return True
                except Exception as exc:
                    logger.warning(f"[AgentLoop] combobox fallback failed: {exc}")
                    return False

            # Default: text / email / textarea / url / tel / number
            try:
                await loc.scroll_into_view_if_needed(timeout=3000)
                await loc.wait_for(state="visible", timeout=3000)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.05, 0.15))
            # Use native setter to handle React-controlled inputs
            committed = await loc.evaluate(
                """(el, v) => {
                    try {
                        const proto = el.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                        setter.call(el, v);
                        el.dispatchEvent(new Event('input',  {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        return el.value === v;
                    } catch(e) { return false; }
                }""",
                value,
            )
            if not committed:
                await loc.fill(value, timeout=5000)
            await asyncio.sleep(random.uniform(0.1, 0.3))
            return True

        except Exception as exc:
            logger.warning(f"[AgentLoop] fill_field sel={sel!r} value={value!r} error: {exc}")
            return False

    return False


# ─────────────────────────────────────────────────────────────────────────────
# History formatter
# ─────────────────────────────────────────────────────────────────────────────

def _format_history(actions: List[AgentAction], last_n: int = 12) -> str:
    if not actions:
        return "(none)"
    recent = actions[-last_n:]
    lines = []
    for a in recent:
        # FAIL markers tell the AI its previous action didn't actually execute.
        # Without this signal the model would keep proposing the same broken
        # selector and burn turns until the stuck detector fires.
        tag = " ❌FAILED" if a.ok is False else ""
        if a.kind == "fill_field":
            lines.append(f"  [{a.step}] fill_field  '{a.field_label or a.selector}' = '{(a.value or '')[:60]}'{tag}")
        elif a.kind == "upload_file":
            lines.append(f"  [{a.step}] upload_file '{a.field_label or a.selector}' ({a.value}){tag}")
        elif a.kind == "click":
            # If a submit-click was blocked by the pre-submit gate, the reason
            # contains the "FILL THESE NEXT" list — surface it so the AI knows
            # which specific fields to address before retrying submit.
            extra = ""
            if a.ok is False and a.reason and "BLOCKED" in a.reason:
                extra = f"  ⚠ {a.reason}"
            lines.append(f"  [{a.step}] click       sel={a.selector!r} text={a.click_text!r}{tag}{extra}")
        elif a.kind == "click_apply":
            lines.append(f"  [{a.step}] click_apply text={a.click_text!r}{tag}")
        elif a.kind == "scroll":
            lines.append(f"  [{a.step}] scroll      {a.direction}{tag}")
        elif a.kind == "next_step":
            lines.append(f"  [{a.step}] next_step{tag}")
        elif a.kind == "navigate_url":
            lines.append(f"  [{a.step}] navigate_url url={a.url!r}{tag}")
        elif a.kind == "verify_page":
            lines.append(f"  [{a.step}] verify_page ✓")
        else:
            lines.append(f"  [{a.step}] {a.kind}  {a.reason or a.confirmation or ''}")
    return "\n".join(lines)


def _compact(obj: Any, max_keys: int = 12) -> str:
    """JSON-encode an object, capping dict entries to keep prompt size down."""
    if isinstance(obj, dict):
        trimmed = dict(list(obj.items())[:max_keys])
        return json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _filled_summary(actions: List[AgentAction]) -> str:
    """One-line-per-field summary of what's already been filled this session.

    Tells the LLM not to refill fields it already handled, preventing the most
    common stuck pattern: agent sees empty field, fills it, DOM updates, sees it
    again on the same page with a slightly different label and fills it twice.
    """
    lines = []
    for a in actions:
        if a.kind == "fill_field" and a.field_label and a.value:
            lines.append(f"  {a.field_label}: {str(a.value)[:60]}")
        elif a.kind == "upload_file" and a.field_label:
            lines.append(f"  {a.field_label}: [file uploaded — {a.value}]")
    return "\n".join(lines) if lines else "(none yet)"


# ─────────────────────────────────────────────────────────────────────────────
# Main agent loop
# ─────────────────────────────────────────────────────────────────────────────

class AgentLoop:
    """Autonomous vision-driven form-filling agent.

    Usage::

        loop = AgentLoop(
            candidate_profile=profile_dict,
            job_context={"title": "SWE", "company": "Acme", "platform": "greenhouse"},
            resume_path="/tmp/resume.pdf",
            cover_letter_path=None,
        )
        result = await loop.run(page, frame=adapter._frame)
    """

    def __init__(
        self,
        candidate_profile: Dict[str, Any],
        job_context: Dict[str, Any],
        resume_path: Optional[str] = None,
        cover_letter_path: Optional[str] = None,
        max_steps: int = MAX_STEPS,
        platform_hints: Optional[Dict[str, Any]] = None,
        screening_answers: Optional[Dict[str, str]] = None,
        candidate_id: Optional[str] = None,
        stop_before_submit: bool = False,
    ):
        self.profile = candidate_profile
        self.job_ctx = job_context
        self.resume_path = resume_path
        self.cover_letter_path = cover_letter_path
        self.max_steps = max_steps
        # Pre-resolved screening answers from M3 (Q→A). The AI is instructed
        # to use these verbatim when a form field matches one of these labels —
        # this preserves M3's tailored answer rather than letting the AI re-invent.
        self.screening_answers: Dict[str, str] = dict(screening_answers or {})
        # Candidate ID enables per-candidate field_memory recall — by the 2nd
        # application this candidate submits, identity fields (name, email,
        # phone, LinkedIn, country, work-auth) are answered for FREE from disk
        # without any LLM call. See Lever 2 in the cost-optimization plan.
        self.candidate_id: Optional[str] = candidate_id
        # When True, the loop refuses to actually click Submit — useful for the
        # standalone M4 test so we don't litter target ATS systems with fake
        # applications during dev iteration. The pre-submit gate still runs;
        # then instead of clicking submit, the loop returns SUBMITTED with
        # confirmation="dry_run_stopped_before_submit".
        self.stop_before_submit: bool = stop_before_submit
        # Auto-populate hints from registry if not explicitly provided
        platform = str(job_context.get("platform") or "generic").lower()
        self._hints = platform_hints if platform_hints is not None else get_platform_hints(platform)
        self._platform = platform
        self._llm = get_llm()
        # Build the identity-anchored system prompt once. Persistent across turns.
        self._system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        """Bake the candidate identity + job context into the system prompt.

        This is a deliberate token optimization AND a behavioral one:
          * The LLM never re-receives the same profile JSON on every turn (a
            ~400-token saving per step across a 30-step run).
          * Anchoring the candidate's NAME at the top of the system message
            stops the model from forgetting whose application it's filling
            on long multi-page forms — it now treats itself as that person's
            agent, not a generic form-filler.
        """
        p = self.profile or {}
        name = p.get("name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip() or "the candidate"
        # Identity card: short, dense, immediately readable by the LLM
        card_lines = [
            f"  Name        : {name}",
            f"  Email       : {p.get('email','')}",
            f"  Phone       : {p.get('phone','')}",
            f"  Location    : {p.get('location','')}",
            # Operator policy: do NOT expose the candidate's real LinkedIn URL
            # on application forms. The literal answer for any LinkedIn-URL
            # field is "N/A".
            f"  LinkedIn URL: N/A  (policy — do NOT submit real LinkedIn URL on forms)",
            f"  Website     : {p.get('website','')}",
            f"  Current role: {p.get('current_title','')} at {p.get('current_company','')}",
            f"  Experience  : {p.get('experience_years','')} years",
            f"  Tech stack  : {p.get('tech_stack','')}",
            f"  Education   : {p.get('education','')}",
            # Country-aware work-auth: the work_authorization flag refers to
            # the candidate's home country (derived from `location`). For
            # ANY other country the answer is "No" and sponsorship is "Yes".
            f"  Work-auth (home, {p.get('location','?')}): {p.get('work_authorization','Yes')}",
            f"  Work-auth (UK / EU / Canada / Australia / other): No — sponsorship required",
            f"  Sponsorship needed in home country: {p.get('sponsorship','No')}",
            # Demographic answers — these are the candidate's actual declared
            # values, NOT defaults invented by the AI. Gender is inferred from
            # the candidate's first name (e.g. "Harmain" → Male, "Sabih" → Male,
            # "Sarah" → Female). The other 5 are fixed per operator config.
            f"  Gender                : (INFER from first name '{p.get('first_name') or (p.get('name','').split()[0] if p.get('name') else '')}' — male names → Male, female names → Female, ambiguous → Male)",
            f"  Race / ethnicity      : South Asian (if option exists), otherwise Asian",
            f"  Sexual orientation    : Heterosexual",
            f"  Veteran status        : I am not a veteran (No)",
            f"  Disability status     : No, I don't have a disability",
            f"  Transgender           : No",
        ]
        # Drop empty rows so the card stays dense
        candidate_card = "\n".join(l for l in card_lines if l.split(":", 1)[1].strip() not in ("", "at"))

        jc = self.job_ctx or {}
        job_desc_snip = (jc.get("job_description") or "").strip()
        if job_desc_snip:
            # Single line, capped — enough for the AI to answer "why this role?"
            # without bloating the cached system prompt.
            job_desc_snip = " ".join(job_desc_snip.split())[:800]
        job_card_lines = [
            f"  Title    : {jc.get('job_title','')}",
            f"  Company  : {jc.get('company','')}",
            f"  Platform : {jc.get('platform','')} ({jc.get('ats_type','')})",
            f"  URL      : {jc.get('job_url','')}",
        ]
        if job_desc_snip:
            job_card_lines.append(f"  Summary  : {job_desc_snip}")
        job_card = "\n".join(l for l in job_card_lines if l.split(":", 1)[1].strip())

        # Pre-resolved screening answers from M3 — append as a "use these
        # verbatim" block so the AI doesn't re-invent answers M3 already wrote.
        screening_block = ""
        if self.screening_answers:
            lines = ["PRE-RESOLVED ANSWERS (use these verbatim when a field's label matches the question):"]
            for q, a in list(self.screening_answers.items())[:20]:
                lines.append(f"  Q: {q[:120]}")
                lines.append(f"  A: {str(a)[:200]}")
            screening_block = "\n" + "\n".join(lines) + "\n"

        # Generic agent identity — same wording for every candidate. The
        # specific candidate they're representing is shown in the WHO YOU
        # REPRESENT section below, not embedded in the agent's role.
        agent_name = "Aria, an AI agent driving a real web browser"

        base = _SYSTEM_PROMPT_BASE.format(
            agent_name=agent_name,
            candidate_card=candidate_card,
            job_card=job_card,
        )
        # Resume content block — the candidate's actual resume text. When
        # present, this is the AUTHORITATIVE source: name, email, phone,
        # education, work history, skills come from HERE, not the identity
        # card. "AI is the master — analyze the resume FIRST, then fill."
        resume_text = (p.get("_resume_text") or "").strip()
        resume_block = ""
        if resume_text:
            sep = "-" * 60
            resume_block = (
                "\n\nRESUME CONTENT (source of truth — when a form field asks "
                "for name, email, phone, education, employer, title, or "
                "skills, use values from THIS resume verbatim. If the "
                "identity card above disagrees with the resume, the RESUME "
                "WINS):\n"
                + sep + "\n"
                + resume_text[:5500]
                + "\n" + sep + "\n"
            )
        # Append the action-schema/rules block (unchanged from the original
        # system prompt, just relocated so the identity card comes first).
        # Then the screening-answers block, if M3 provided any.
        return base + resume_block + screening_block + _ACTION_SCHEMA_BLOCK

    # ──────────────────────────────────────────────────────────────────────
    # Lever 2: memory pre-fill — recall + apply known answers before the LLM
    # ──────────────────────────────────────────────────────────────────────

    async def _try_memory_prefill(
        self,
        page: Page,
        frame: Optional[Any],
        is_iframe_mode: bool,
        actions: List[AgentAction],
    ) -> int:
        """Walk visible interactive elements, recall any memorized answers,
        and fill them in-place. Returns the count of fields filled this pass.

        Safety:
          - Only fills fields that currently have an empty value
          - Skips fields the AI already attempted this session (any label that
            appears in `actions`) so we don't fight the agent
          - Uses memory.recall() which already gates identity fields to the
            per-candidate namespace — never leaks cross-candidate
        """
        if not self.candidate_id:
            return 0
        try:
            from ..forms import memory as _field_memory
        except Exception:
            return 0

        ctx = frame or page
        already_attempted_labels = {
            (a.field_label or "").strip().lower()
            for a in actions
            if a.kind == "fill_field" and a.field_label
        }

        # Walk the same elements the DOM snapshot exposes — but in Python so
        # we have memory + selectors at hand.
        try:
            fields = await ctx.evaluate("""() => {
                const out = [];
                const skip = new Set(['hidden','submit','button','image','reset','file','search']);
                function inConsentBanner(el) {
                    return !!el.closest(
                        '#onetrust-banner-sdk, #onetrust-pc-sdk, #onetrust-consent-sdk, '
                      + '[id*="cookie"], [class*="cookie-banner"], [class*="cookie-consent"], '
                      + '#CybotCookiebotDialog, [class*="cmp-"], '
                      + '[id*="truste"], [class*="osano-"], '
                      + '[aria-label*="cookie" i], [aria-label*="consent" i]'
                    );
                }
                // Skip search bars — they're not application form fields.
                // Memory pre-fill was wrongly filling email into careers-site
                // search inputs (MUI's generic #outlined-basic on jobs.elastic.co
                // for example) because they match label 'Email' or similar.
                function isSearchInput(el) {
                    const name = (el.name || '').toLowerCase();
                    const id = (el.id || '').toLowerCase();
                    const ph = (el.placeholder || '').toLowerCase();
                    const al = (el.getAttribute('aria-label') || '').toLowerCase();
                    if (name === 'search' || name === 'q' || name === 'query') return true;
                    if (id === 'search' || id.startsWith('search-')) return true;
                    if (/\\bsearch\\b/i.test(ph) && !/\\b(email|name|phone)\\b/i.test(ph)) return true;
                    if (/\\bsearch\\b/i.test(al)) return true;
                    // Material UI generic outlined input that doesn't have a
                    // proper label — almost always a careers-site search box
                    if (id === 'outlined-basic' && !el.getAttribute('aria-label')) return true;
                    // Inside an obvious nav / header / search container
                    if (el.closest('nav, header, [role="search"], [class*="search-bar"], [class*="searchbox"]')) return true;
                    return false;
                }
                document.querySelectorAll('input, select, textarea').forEach(el => {
                    if (out.length >= 40) return;
                    const type = (el.type || 'text').toLowerCase();
                    if (skip.has(type)) return;
                    if (inConsentBanner(el)) return;
                    if (isSearchInput(el)) return;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return;
                    let label = el.getAttribute('aria-label') || '';
                    if (!label && el.id) {
                        const lbl = document.querySelector(`label[for="${el.id}"]`);
                        if (lbl) label = (lbl.textContent || '').trim();
                    }
                    if (!label) label = el.name || el.id || '';
                    if (!label) return;
                    const sel = el.id ? '#' + el.id : (el.name ? `[name="${el.name}"]` : '');
                    if (!sel) return;
                    const value = (el.value || '').trim();
                    out.push({ sel, type, label: label.slice(0, 100), value });
                });
                return out;
            }""")
        except Exception as exc:
            logger.debug(f"[AgentLoop] memory pre-fill scan failed: {exc}")
            return 0

        filled = 0
        for f in fields or []:
            if f.get("value"):
                continue  # already filled
            label = (f.get("label") or "").strip()
            if not label or label.lower() in already_attempted_labels:
                continue
            try:
                answer = _field_memory.recall(
                    label=label,
                    field_type=f.get("type", "text"),
                    options=None,
                    candidate_id=self.candidate_id,
                )
            except Exception as exc:
                logger.debug(f"[AgentLoop] memory recall failed for {label!r}: {exc}")
                continue
            if not answer:
                continue
            sel = f["sel"]
            try:
                loc = ctx.locator(sel).first
                if await loc.count() == 0:
                    continue
                # Use the React-aware native setter so controlled inputs commit
                committed = await loc.evaluate(
                    """(el, v) => {
                        try {
                            const proto = el.tagName === 'TEXTAREA'
                                ? window.HTMLTextAreaElement.prototype
                                : window.HTMLInputElement.prototype;
                            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                            setter.call(el, v);
                            el.dispatchEvent(new Event('input',  {bubbles:true}));
                            el.dispatchEvent(new Event('change', {bubbles:true}));
                            return el.value === v;
                        } catch(e) { return false; }
                    }""",
                    answer,
                )
                if not committed:
                    await loc.fill(answer, timeout=3000)
                # Synthesize an AgentAction record so the rest of the loop
                # (filled_summary, history, stuck detector) sees this fill.
                actions.append(AgentAction(
                    kind="fill_field",
                    selector=sel,
                    value=answer,
                    field_label=label,
                    step=-1,  # -1 = memory pre-fill (not an LLM turn)
                    ok=True,
                    raw={"source": "memory_prefill"},
                ))
                logger.info(f"[AgentLoop] memory pre-fill: '{label}' = '{str(answer)[:60]}'")
                filled += 1
            except Exception as exc:
                logger.debug(f"[AgentLoop] memory pre-fill apply failed for {label!r}: {exc}")
                continue

        return filled

    async def _try_demographic_autofill(
        self,
        page: Page,
        frame: Optional[Any],
        actions: List[AgentAction],
    ) -> int:
        """Force "No" on policy-fixed demographic questions BEFORE the LLM runs.

        Operator policy: disability, veteran/armed-forces, and transgender
        questions ALWAYS get "No" — regardless of candidate, platform, or form.
        Doing this here (not via the LLM) means:
          • The answer can't be skipped if the AI loops earlier in the form
          • Zero LLM cost for these fields
          • The fields surface as "filled" in the FORM STATUS checklist, which
            unblocks the pre-submit gate without the AI needing to remember
            the rule mid-session.

        Handles: native <select>, native radio groups, react-select comboboxes.
        Idempotent — skips fields that already have a value.

        **One-shot per session per selector**: after the first attempt on a
        given selector, defer to the AI's normal fill_field path. The hardcoded
        combobox commit logic can race with the form's controlled state on
        Greenhouse — retrying every turn caused a fight that left #country
        permanently empty in form state and blocked all submits.
        """
        ctx = frame or page
        # Selectors we've already attempted in this session — never retry,
        # even if the visual scan thinks the field is still empty.
        attempted_selectors = {
            (a.selector or "").strip()
            for a in actions
            if a.raw and a.raw.get("source") == "demographic_autofill"
        }
        try:
            targets = await ctx.evaluate("""() => {
                const QUESTION_RE = /\\b(disabilit|veteran|armed forces|transgender|military service)\\b/i;
                const NO_OPTION_RE = /^\\s*(no|no, i don'?t|i don'?t|i am not|not a veteran|not a protected veteran|not an active|no, i'?m not)\\b/i;
                // Work-auth in NON-US countries — policy: always "No".
                // Visa-sponsorship in NON-US countries — policy: always "Yes".
                // The label must mention a non-US country (UK/EU/etc.) AND a
                // work-auth or sponsorship intent. We do NOT touch US-targeted
                // versions of these questions — those stay for the AI/memory.
                const NON_US_COUNTRY_RE = /\\b(uk|united kingdom|britain|england|eu|european union|europe|ireland|canada|canadian|australia|australian|new zealand|germany|france|netherlands|spain|italy|japan|singapore|hong kong|india|brazil|mexico)\\b/i;
                const WORK_AUTH_RE = /\\b(authori[sz]ed to work|legally authori[sz]ed|right to work|work permit|work authori[sz]ation|eligib(le|ility) to work)\\b/i;
                const SPONSORSHIP_RE = /\\b(sponsorship|require sponsorship|need sponsorship|sponsor.{0,15}(visa|work)|visa sponsorship)\\b/i;
                const YES_OPTION_RE = /^\\s*(yes|i (do|will)|yes,)\\b/i;
                // Country-of-residence questions — operator policy: always "United States".
                // Match generic "Country" labels but exclude "country of citizenship",
                // "country code" (phone), and explicit non-US confirmations like "UK?".
                const COUNTRY_RE = /\\bcountry\\b/i;
                const COUNTRY_EXCLUDE_RE = /\\b(code|citizenship|nationality)\\b/i;
                const US_OPTION_RE = /^\\s*(united states( of america)?|usa|u\\.s\\.a?\\.?)\\s*$/i;

                function labelFor(el) {
                    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
                    if (el.id) {
                        const lbl = document.querySelector('label[for="' + el.id + '"]');
                        if (lbl) return (lbl.textContent || '').trim();
                    }
                    // Fieldset legend / nearest label
                    const fs = el.closest('fieldset');
                    if (fs) {
                        const lg = fs.querySelector('legend');
                        if (lg) return (lg.textContent || '').trim();
                    }
                    let p = el.parentElement;
                    for (let i = 0; i < 6 && p; i++) {
                        const lbl = p.querySelector(':scope > label, :scope > legend, :scope > span');
                        if (lbl && !lbl.contains(el)) return (lbl.textContent || '').trim();
                        p = p.parentElement;
                    }
                    return '';
                }

                const out = [];
                const seen = new Set();

                // Decide the policy answer for a field's label, or null to skip.
                // Returns {answer: 'No'|'Yes'|'United States', matcher: RegExp}.
                function policyFor(label) {
                    if (!label) return null;
                    if (QUESTION_RE.test(label)) {
                        return { answer: 'No', matcher: NO_OPTION_RE };
                    }
                    // Non-US country work-auth → No
                    if (NON_US_COUNTRY_RE.test(label) && WORK_AUTH_RE.test(label)) {
                        return { answer: 'No', matcher: NO_OPTION_RE };
                    }
                    // Non-US country sponsorship → Yes
                    if (NON_US_COUNTRY_RE.test(label) && SPONSORSHIP_RE.test(label)) {
                        return { answer: 'Yes', matcher: YES_OPTION_RE };
                    }
                    // Country-of-residence → United States
                    if (COUNTRY_RE.test(label) && !COUNTRY_EXCLUDE_RE.test(label)) {
                        return { answer: 'United States', matcher: US_OPTION_RE };
                    }
                    return null;
                }

                // Native <select> — handles demographic "No" questions AND
                // country-of-residence (always "United States").
                document.querySelectorAll('select').forEach(sel => {
                    const style = window.getComputedStyle(sel);
                    if (style.display === 'none' || style.visibility === 'hidden') return;
                    const label = labelFor(sel);
                    const policy = policyFor(label);
                    if (!policy) return;
                    if (sel.value && sel.value.trim()) {
                        // Already filled with a non-placeholder option — leave alone.
                        const cur = (sel.options[sel.selectedIndex]?.text || '').trim();
                        if (cur && !/^(select|choose|please|—|--)/i.test(cur)) {
                            // BUT: if it's a Yes/No question and the current answer
                            // contradicts policy (e.g. "Yes" on UK work-auth), force-overwrite.
                            const wantNo = policy.answer === 'No';
                            const wantYes = policy.answer === 'Yes';
                            const isYes = /^\\s*(yes|i (do|will)|yes,)\\b/i.test(cur);
                            const isNo = /^\\s*(no|i (don'?t|am not|do not))\\b/i.test(cur);
                            if (wantNo && isYes) {
                                // fall through to overwrite
                            } else if (wantYes && isNo) {
                                // fall through to overwrite
                            } else {
                                return;  // current value is acceptable
                            }
                        }
                    }
                    let pickText = null, pickValue = null;
                    for (const opt of sel.options) {
                        if (!opt.value) continue;
                        if (policy.matcher.test(opt.text || '')) {
                            pickText = opt.text.trim();
                            pickValue = opt.value;
                            break;
                        }
                    }
                    if (pickText === null) return;
                    const key = sel.id || sel.name;
                    if (!key || seen.has(key)) return;
                    seen.add(key);
                    out.push({
                        kind: 'select',
                        selector: sel.id ? '#' + sel.id : 'select[name="' + sel.name + '"]',
                        label: label.slice(0, 120),
                        value: pickText,
                        optionValue: pickValue,
                    });
                });

                // Native radio groups
                const radioGroups = {};
                document.querySelectorAll('input[type="radio"]').forEach(r => {
                    if (!r.name) return;
                    (radioGroups[r.name] = radioGroups[r.name] || []).push(r);
                });
                for (const [name, radios] of Object.entries(radioGroups)) {
                    // Group-level label = fieldset legend on any radio
                    const first = radios[0];
                    const fs = first.closest('fieldset');
                    let groupLabel = '';
                    if (fs) {
                        const lg = fs.querySelector('legend');
                        if (lg) groupLabel = (lg.textContent || '').trim();
                    }
                    if (!groupLabel) groupLabel = labelFor(first);
                    const policy = policyFor(groupLabel);
                    if (!policy) continue;
                    // For radio groups, also check whether the currently-checked
                    // option contradicts policy — if so, overwrite. Otherwise,
                    // leave correctly-answered groups alone.
                    const checkedRadio = radios.find(r => r.checked);
                    if (checkedRadio) {
                        const checkedLabel = labelFor(checkedRadio) || checkedRadio.value || '';
                        if (policy.matcher.test(checkedLabel)) continue;  // already correct
                    }
                    // Find the radio whose own label text matches the policy answer
                    let pick = null;
                    for (const r of radios) {
                        const ownLabel = labelFor(r) || r.value || '';
                        if (policy.matcher.test(ownLabel)) { pick = r; break; }
                    }
                    if (!pick) continue;
                    if (seen.has(name)) continue;
                    seen.add(name);
                    out.push({
                        kind: 'radio',
                        selector: pick.id ? '#' + pick.id
                            : 'input[type="radio"][name="' + name + '"][value="' + pick.value + '"]',
                        label: groupLabel.slice(0, 120),
                        value: labelFor(pick) || pick.value,
                    });
                }

                // React-select comboboxes — country-of-residence + non-US work-auth/sponsorship
                document.querySelectorAll('[role="combobox"], input[id$="--input"]').forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return;
                    const label = labelFor(el);
                    const policy = policyFor(label);
                    if (!policy) return;
                    // Inspect the current rendered single-value, if any.
                    const container = el.closest('[class*="select__control"], [class*="-control"]');
                    let currentText = '';
                    if (container) {
                        const sv = container.querySelector('[class*="singleValue"], [class*="-singleValue"]');
                        if (sv) currentText = sv.textContent.trim();
                    }
                    if (currentText) {
                        if (policy.matcher.test(currentText)) return;  // already correct
                        // else: contradicts policy → overwrite
                    }
                    const sel = el.id ? '#' + el.id : null;
                    if (!sel || seen.has(sel)) return;
                    seen.add(sel);
                    out.push({
                        kind: 'combobox',
                        selector: sel,
                        label: label.slice(0, 120),
                        value: policy.answer,
                    });
                });

                return out;
            }""")
        except Exception as exc:
            logger.debug(f"[AgentLoop] demographic auto-fill scan failed: {exc}")
            return 0

        filled = 0
        for t in targets or []:
            sel = t.get("selector")
            kind = t.get("kind")
            label = t.get("label", "")
            value = t.get("value", "No")
            if (sel or "").strip() in attempted_selectors:
                # Already tried this field once this session — skip to avoid
                # fighting the AI / form state every subsequent turn.
                continue
            try:
                if kind == "select":
                    loc = ctx.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    try:
                        await loc.select_option(value=t.get("optionValue"), timeout=3000)
                    except Exception:
                        await loc.select_option(label=value, timeout=3000)
                elif kind == "radio":
                    loc = ctx.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    try:
                        await loc.check(timeout=3000, force=True)
                    except Exception:
                        await loc.click(timeout=3000, force=True)
                elif kind == "combobox":
                    # Greenhouse's country picker is a react-select. The
                    # reliable commit pattern is: click the visible combobox,
                    # TYPE the value (not .fill — react-select needs real
                    # keystrokes to filter), wait for options to render,
                    # then click the matching option. .fill() doesn't fire
                    # the keystroke events react-select listens to, which is
                    # why the previous attempt left the form value empty
                    # while LOOKING filled.
                    loc = ctx.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    try:
                        await loc.scroll_into_view_if_needed(timeout=2000)
                        await loc.click(timeout=3000)
                        await page.wait_for_timeout(200)
                        await loc.type(value, delay=30, timeout=4000)
                        await page.wait_for_timeout(500)
                        option = ctx.locator(
                            "[role='option']", has_text=value
                        ).first
                        if await option.count() > 0:
                            await option.click(timeout=3000)
                        else:
                            await loc.press("Enter", timeout=2000)
                        await page.wait_for_timeout(200)
                    except Exception as exc:
                        logger.debug(
                            f"[AgentLoop] combobox policy auto-fill failed: {exc}"
                        )
                        continue
                else:
                    continue

                actions.append(AgentAction(
                    kind="fill_field",
                    selector=sel,
                    value=value,
                    field_label=label,
                    step=-1,
                    ok=True,
                    raw={"source": "demographic_autofill"},
                ))
                logger.info(
                    f"[AgentLoop] demographic auto-fill: '{label[:60]}' = '{value}'"
                )
                filled += 1
            except Exception as exc:
                logger.debug(
                    f"[AgentLoop] demographic auto-fill apply failed for {label!r}: {exc}"
                )
                continue

        return filled

    async def _handle_email_verification(
        self,
        page: Page,
        frame: Optional[Any],
        after_epoch: int,
        actions: List[AgentAction],
    ) -> bool:
        """Post-submit handler: if the ATS rendered a verification-code input,
        fetch the code from the candidate's Gmail inbox and fill it.

        Returns True when a code was successfully filled (the AI's next turn
        will see the post-fill page and submit again). Returns False on every
        no-op path so the loop continues as before.

        No-fatal-errors policy: anything that can fail (no Gmail token, OAuth
        failure, timeout) just logs and returns False — the existing abort
        path takes over from there.
        """
        # Give the page a moment to render the verification UI.
        try:
            await asyncio.sleep(2.0)
        except Exception:
            pass
        try:
            from ..verification import (
                detect_code_input, fetch_verification_code, fill_code,
            )
        except Exception as exc:
            logger.debug(f"[verify] module import failed: {exc}")
            return False

        # Live frame may have detached during navigation — re-resolve.
        ctx_frame = frame
        try:
            from ..frame_utils import get_live_frame
            ctx_frame = get_live_frame(page, frame) or frame
        except Exception:
            pass

        shape = await detect_code_input(page, ctx_frame)
        if not shape:
            return False
        logger.info(
            f"[verify] code-input detected (kind={shape.kind}, "
            f"digits={shape.digits}); fetching from Gmail…"
        )
        if not self.candidate_id:
            logger.warning("[verify] no candidate_id on AgentLoop — cannot fetch code")
            return False
        code = await fetch_verification_code(
            self.candidate_id, after_epoch=after_epoch, timeout_s=90.0
        )
        if not code:
            logger.warning(
                "[verify] no verification code fetched — Gmail not connected, "
                "no matching email arrived, or OAuth refused. Falling back."
            )
            return False
        ok = await fill_code(page, ctx_frame, shape, code)
        if not ok:
            logger.warning(f"[verify] failed to fill code {code!r} into shape={shape}")
            return False
        logger.info(
            f"[verify] filled verification code {code!r} into {shape.kind} input "
            f"({len(shape.selectors)} field(s)); next turn will submit"
        )
        # Synthesize an action record so the next-turn history shows the fill.
        actions.append(AgentAction(
            kind="fill_field",
            selector=shape.selectors[0],
            value="*" * len(code),  # never log the code itself
            field_label="Email verification code",
            step=-1,
            ok=True,
            raw={"source": "email_verification", "digits": len(code)},
        ))
        return True

    async def run(
        self,
        page: Page,
        frame: Optional[Any] = None,
    ) -> LoopResult:
        from ..frame_utils import get_live_frame

        actions: List[AgentAction] = []
        stuck_count = 0
        llm_error_count = 0
        prev_dom_hash = ""
        page_verified = False
        is_iframe_mode = frame is not None

        transition_state = {"is_transitioning": False}

        def on_frame_attached(f: Any):
            import time
            if f == page.main_frame or getattr(f, "parent_frame", None) == page.main_frame:
                logger.info(f"[FrameLifecycle {time.time():.3f}] attached: {f.name}")

        def on_frame_navigated(f: Any):
            import time
            if f == page.main_frame or getattr(f, "parent_frame", None) == page.main_frame:
                logger.info(f"[FrameLifecycle {time.time():.3f}] navigated: {f.name} (url: {f.url})")
                transition_state["is_transitioning"] = True

        def on_frame_detached(f: Any):
            import time
            if f == page.main_frame or getattr(f, "parent_frame", None) == page.main_frame:
                logger.info(f"[FrameLifecycle {time.time():.3f}] detached: {f.name}")
                transition_state["is_transitioning"] = True

        page.on("frameattached", on_frame_attached)
        page.on("framenavigated", on_frame_navigated)
        page.on("framedetached", on_frame_detached)

        # ── Pre-loop: dismiss cookie / consent banners ──────────────────────
        # Cookie banners (OneTrust, Cookiebot, Osano, TrustArc, etc.) overlay
        # the application form on most company-hosted careers pages. Their
        # checkboxes show up in the DOM and the AI mistakes them for form
        # fields (we just saw this on cockroachlabs.com where the AI tried
        # to fill #select-all-vendor-leg-handler as a job-application field).
        # We try common "Accept All" / "Reject All" buttons first; either is
        # fine — we just need the overlay gone.
        try:
            cookie_dismissed = await page.evaluate("""() => {
                const tryClick = (selectors) => {
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null) {
                            try { el.click(); return sel; } catch(e) {}
                        }
                    }
                    return null;
                };
                // 1. OneTrust patterns (most common)
                let clicked = tryClick([
                    '#onetrust-accept-btn-handler',
                    '#accept-recommended-btn-handler',
                    '#onetrust-reject-all-handler',
                    '.onetrust-close-btn-handler',
                ]);
                if (clicked) return clicked;
                // 2. Cookiebot patterns
                clicked = tryClick([
                    '#CybotCookiebotDialogBodyButtonAccept',
                    '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
                ]);
                if (clicked) return clicked;
                // 3. Osano patterns
                clicked = tryClick(['.osano-cm-accept-all', '.osano-cm-close']);
                if (clicked) return clicked;
                // 4. Generic text-based fallback — look for any visible button
                //    with "Accept" / "Agree" / "Got it" text in a banner area
                const banner = document.querySelector(
                    '[id*="cookie"], [class*="cookie"], [id*="consent"], '
                  + '[class*="consent"], [role="dialog"][aria-label*="privacy" i]'
                );
                if (banner) {
                    const btns = banner.querySelectorAll('button, a[role="button"]');
                    for (const btn of btns) {
                        const t = (btn.textContent || '').toLowerCase().trim();
                        if (/^(accept|allow|agree|got it|i agree|ok)/i.test(t)) {
                            try { btn.click(); return 'text:' + t.slice(0, 30); } catch(e) {}
                        }
                    }
                }
                // 5. Generic close-X on ANY non-application modal/dialog. Lots
                //    of careers sites pop a "Subscribe to our newsletter" or
                //    "Get job alerts" modal on top of the form. We close any
                //    fixed-position dialog whose visible text doesn't look
                //    like a job application.
                const modals = document.querySelectorAll(
                    '[role="dialog"], [role="alertdialog"], .modal, [class*="modal-overlay"]'
                );
                for (const m of modals) {
                    if (m.offsetParent === null) continue;
                    const txt = (m.textContent || '').toLowerCase();
                    // Skip the actual application form — it sometimes is in a dialog
                    if (/(first name|last name|email|resume|apply)/i.test(txt) && txt.length > 400) continue;
                    // Find a close button — × / ✕ / "Close" / aria-label
                    const closeBtn = m.querySelector(
                        '[aria-label*="close" i], [aria-label*="dismiss" i], '
                      + 'button.close, .close-button, [class*="close-btn"], '
                      + 'button[title*="close" i]'
                    ) || Array.from(m.querySelectorAll('button')).find(
                        b => /^[×✕✖xX]$|^close$|^dismiss$/i.test((b.textContent||'').trim())
                    );
                    if (closeBtn) {
                        try { closeBtn.click(); return 'modal-close'; } catch(e) {}
                    }
                }
                return null;
            }""")
            if cookie_dismissed:
                logger.info(f"[AgentLoop] dismissed cookie banner via {cookie_dismissed!r}")
                await asyncio.sleep(0.8)  # let the banner animate out
        except Exception as exc:
            logger.debug(f"[AgentLoop] cookie-banner dismiss pass failed (non-fatal): {exc}")

        # ── Pre-loop: give SPAs time to hydrate. Many modern careers sites
        # (Elastic, Stripe, Coinbase, etc.) render the form via JavaScript
        # AFTER DOMContentLoaded. If we screenshot immediately we get a
        # bare shell with a search bar — the AI then incorrectly classifies
        # it as a "careers landing" and aborts. Wait for either an Apply
        # link / form input to appear, or 6s, whichever is first.
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        try:
            # Wait for ANY of: a form field, an Apply link/button
            await page.wait_for_function(
                """() => {
                    const hasForm  = !!document.querySelector(
                        'input[type="text"], input[type="email"], input[type="file"], '
                      + 'select, textarea, [role="combobox"]'
                    );
                    const hasApply = !!Array.from(
                        document.querySelectorAll('a, button')
                    ).find(el => /apply/i.test(el.textContent || ''));
                    return hasForm || hasApply;
                }""",
                timeout=6000,
            )
            logger.info("[AgentLoop] SPA settled — form or Apply detected")
        except Exception:
            logger.info("[AgentLoop] SPA settle timed out; proceeding anyway")
        await asyncio.sleep(1.0)

        # Wall-clock budget — abort if the loop runs longer than this even when
        # MAX_STEPS hasn't been reached. Prevents the AI from grinding away on
        # an unsupported careers SPA forever.
        import time as _time
        _wall_start = _time.monotonic()

        try:
            for step in range(1, self.max_steps + 1):
                # Wall-clock check
                if _time.monotonic() - _wall_start > _DEFAULT_WALL_TIMEOUT_S:
                    logger.warning(
                        f"[AgentLoop] wall-clock timeout {_DEFAULT_WALL_TIMEOUT_S:.0f}s "
                        f"reached at step={step} — aborting"
                    )
                    return LoopResult(
                        success=False,
                        status="MAX_STEPS",
                        error=f"wall-clock timeout after {_DEFAULT_WALL_TIMEOUT_S:.0f}s",
                        steps_taken=step,
                        actions=actions,
                    )
                if transition_state["is_transitioning"]:
                    from ..frame_utils import wait_for_stability
                    await wait_for_stability(page, frame, is_iframe_mode)
                    transition_state["is_transitioning"] = False

                # ── Check frame attachment ──
                live_frame = None
                if is_iframe_mode:
                    live_frame = await get_live_frame(frame)
                    if live_frame and live_frame.is_detached():
                        logger.warning(f"[AgentLoop] step={step} Frame is detached!")
                        live_frame = None

                # ── Capture perception ──────────────────────────────────────────
                # Lever 1 (cost optimization): if the DOM hasn't changed since
                # the last call AND the previous action was DOM-mutating
                # (fill_field, click, upload — i.e. we expected change but got
                # none), skip the screenshot for THIS turn. The DOM snapshot
                # text alone is enough for the AI to decide its next move,
                # and we save ~1,500 vision tokens per skip. Vision is brought
                # back automatically on the next turn so the AI can re-evaluate
                # if it gets confused.
                try:
                    # Lever 2: before paying an LLM call, scan the current
                    # form for fields whose label has a memory hit and fill
                    # them in-place. By the candidate's 2nd application this
                    # eliminates 6-8 LLM turns (name/email/phone/LinkedIn/etc).
                    # NOTE: the JS-driven demographic / country auto-fill that
                    # used to live here has been removed. It was racing with
                    # the AI's own fill_field path on the same widgets every
                    # turn (most visibly: refiring on #country 13× in one
                    # session, leaving the react-select state desynced and
                    # the form value empty in DOM, which the server then
                    # rejected on submit).
                    # The policy answers (No for disability / veteran /
                    # armed-forces / transgender, United States for country)
                    # are still enforced via the system prompt rules in
                    # _SYSTEM_PROMPT_BASE, so the AI handles them through its
                    # normal sequential fill flow.

                    memory_filled = await self._try_memory_prefill(
                        page, live_frame, is_iframe_mode, actions
                    )
                    if memory_filled:
                        logger.info(
                            f"[AgentLoop] step={step} memory pre-fill applied to "
                            f"{memory_filled} field(s) — skipped {memory_filled} LLM call(s)"
                        )
                        # Memory only matches real form-field labels. If any matched,
                        # this page IS a job-application form — short-circuit the
                        # verify-page gate so the AI can dive straight into filling.
                        page_verified = True

                    dom = await _dom_snapshot(page, live_frame, is_iframe_mode)
                    current_hash = await _dom_hash(live_frame or page)
                    # Decide whether to send a screenshot or a text-only turn
                    dom_unchanged_after_mutation = (
                        prev_dom_hash
                        and current_hash == prev_dom_hash
                        and bool(actions)
                        and actions[-1].kind in (
                            "fill_field", "click", "upload_file", "next_step"
                        )
                    )
                    if dom_unchanged_after_mutation:
                        screenshot = None
                        logger.debug(
                            f"[AgentLoop] step={step} DOM stable + last action mutating "
                            "— sending text-only turn (no screenshot, saves ~1500 vision tokens)"
                        )
                    else:
                        # Lever 3: 960x540 @ q35 is sufficient resolution for
                        # the LLM to read form labels and identify widgets.
                        screenshot = await page.screenshot(
                            full_page=False,
                            type="jpeg",
                            quality=35,
                            clip={"x": 0, "y": 0, "width": 960, "height": 540},
                        )
                except Exception as exc:
                    logger.error(f"[AgentLoop] step={step} capture failed: {exc}")
                    return LoopResult(
                        success=False,
                        status="ERROR",
                        error=f"screenshot/dom capture failed: {exc}",
                        steps_taken=step,
                        actions=actions,
                    )

                # ── Stuck detector ──────────────────────────────────────────────
                # DOM unchanged is OK if the *last* action was one that doesn't
                # mutate DOM by design (scroll / wait / verify_page) — those are
                # legitimate "let me observe more" moves and shouldn't count
                # against the stuck quota. Only DOM-mutating intents (fill,
                # click, upload, next_step) without a resulting DOM change
                # signal real stuck-ness.
                last_action_was_observational = (
                    bool(actions) and actions[-1].kind in ("scroll", "wait", "verify_page")
                )
                if (current_hash and current_hash == prev_dom_hash and step > 1
                        and not last_action_was_observational):
                    stuck_count += 1
                    if stuck_count >= STUCK_THRESHOLD:
                        logger.warning(f"[AgentLoop] stuck for {stuck_count} steps — aborting")
                        return LoopResult(
                            success=False,
                            status="STUCK",
                            error=f"DOM unchanged for {stuck_count} consecutive steps",
                            steps_taken=step,
                            actions=actions,
                        )
                else:
                    stuck_count = 0
                prev_dom_hash = current_hash

                # ── Build per-turn user message ─────────────────────────────────
                # Candidate identity + job context already live in the system
                # prompt (baked once at AgentLoop init). Per-turn payload is
                # now ~80% smaller than the original design.
                user_msg = _USER_TURN_TEMPLATE.format(
                    step=step,
                    max_steps=self.max_steps,
                    platform=self._platform,
                    filled_summary=_filled_summary(actions),
                    n_actions=len(actions),
                    history=_format_history(actions),
                    dom_snapshot=dom,
                )

                # ── LLM call ────────────────────────────────────────────────────
                from ..llm import telemetry as _tele
                _tele.set_label("agent_loop.step")
                try:
                    raw = await self._llm.generate_json(
                        prompt=user_msg,
                        image_bytes=screenshot,
                        temperature=0.0,
                        timeout_s=STEP_TIMEOUT_S,
                        system=self._system_prompt,
                    )
                    llm_error_count = 0
                except LLMUnavailable as exc:
                    llm_error_count += 1
                    logger.warning(f"[AgentLoop] step={step} LLM error ({llm_error_count}/{LLM_RETRY_LIMIT}): {exc}")
                    if llm_error_count >= LLM_RETRY_LIMIT:
                        return LoopResult(
                            success=False,
                            status="LLM_UNAVAILABLE",
                            error=str(exc),
                            steps_taken=step,
                            actions=actions,
                        )
                    await asyncio.sleep(2.0)
                    continue

                # ── Parse action ────────────────────────────────────────────────
                if not isinstance(raw, dict):
                    logger.warning(f"[AgentLoop] step={step} LLM returned non-dict: {type(raw)}")
                    continue

                action = _parse_action(raw, step)
                if action is None:
                    continue

                logger.info(
                    f"[AgentLoop] step={step}/{self.max_steps} "
                    f"kind={action.kind} "
                    f"sel={action.selector!r} "
                    f"val={(action.value or '')[:50]!r} "
                    f"label={(action.field_label or '')[:50]!r}"
                )

                # ── Pre-submit completeness gate ─────────────────────────────
                # If this is the AI's FIRST submit click, force a full top-to-
                # bottom scroll and surface any unfilled required fields BEFORE
                # actually clicking submit. The #1 mistake the AI makes is
                # clicking submit while still mid-form, before scrolling to see
                # demographic questions / disability status / required dropdowns
                # at the very bottom of the page. This gate makes that impossible.
                def _is_submit_click(a: AgentAction) -> bool:
                    if a.kind != "click":
                        return False
                    s = (a.selector or "") + " " + (a.click_text or "")
                    return bool(re.search(r"submit|apply|send application", s, re.I))

                if _is_submit_click(action):
                    prior_submits = sum(1 for a in actions if _is_submit_click(a))
                    # Hard cap: 3 submit attempts total. Past that, the server
                    # is rejecting for reasons the AI can't fix from the DOM
                    # (anti-bot, CAPTCHA, email-verify, server-side validation).
                    # Continuing to spam submit gets the candidate flagged.
                    if prior_submits >= 3:
                        logger.error(
                            f"[AgentLoop] step={step} SUBMIT CAP: {prior_submits} prior submit "
                            "attempts — aborting to avoid anti-bot trip. Form likely has a "
                            "field rejected server-side that's not visible in the DOM."
                        )
                        action.ok = False
                        action.reason = (
                            f"ABORTED: {prior_submits} submit attempts already — "
                            "server-side rejection cannot be fixed via DOM"
                        )
                        actions.append(action)
                        return LoopResult(
                            success=False,
                            status="STUCK",
                            error=f"Submit clicked {prior_submits}x without success",
                            steps_taken=step,
                            actions=actions,
                        )
                    # Run the completeness gate on EVERY submit attempt, not just
                    # the first. Previously the AI could repeat-click submit and
                    # bypass the check on attempts 2+ — that defeated the whole
                    # point of the gate. Now: any submit click is blocked until
                    # all required fields are confirmed filled.
                    if True:
                        ordinal = "First" if prior_submits == 0 else f"Repeat (#{prior_submits + 1})"
                        logger.info(
                            f"[AgentLoop] {ordinal} submit click — running pre-submit "
                            "completeness gate (scroll to bottom + verify required fields)"
                        )
                        try:
                            unfilled = await page.evaluate("""() => {
                                // Scroll to absolute bottom of the page so the
                                // next screenshot+DOM snapshot shows everything
                                window.scrollTo(0, document.body.scrollHeight);
                                // Find required fields that look unfilled
                                const out = [];
                                const inputs = document.querySelectorAll(
                                    'input:not([type=hidden]):not([type=submit]):not([type=button]), '
                                    + 'select, textarea, [role="combobox"]'
                                );
                                inputs.forEach(el => {
                                    if (out.length >= 12) return;
                                    // Resolve label first (also used for demographic detection).
                                    // Must mirror the FORM STATUS resolver — including the
                                    // parent-container walk — so demographic questions like
                                    // the Greenhouse disability dropdown (no label[for=...],
                                    // no fieldset/legend) are correctly classified instead
                                    // of being silently skipped.
                                    let gLabel = el.getAttribute('aria-label') || '';
                                    if (!gLabel && el.id) {
                                        const lbl = document.querySelector(`label[for="${el.id}"]`);
                                        if (lbl) gLabel = (lbl.textContent || '').trim();
                                        if (!gLabel && el.id.endsWith('--input')) {
                                            const base = el.id.slice(0, -7);
                                            const lbl2 = document.querySelector(`label[for="${base}"]`);
                                            if (lbl2) gLabel = (lbl2.textContent || '').trim();
                                        }
                                    }
                                    if (!gLabel) {
                                        const fset = el.closest('fieldset');
                                        if (fset) { const lg = fset.querySelector('legend'); if (lg) gLabel = (lg.textContent || '').trim(); }
                                    }
                                    if (!gLabel) {
                                        let p = el.parentElement;
                                        for (let i = 0; i < 6 && p && !gLabel; i++) {
                                            const lbl = p.querySelector(':scope > label, :scope > .application-question__label, :scope > .question-label, :scope > .field-label, :scope > .form-question, :scope > div > label');
                                            if (lbl) gLabel = (lbl.textContent || '').trim();
                                            p = p.parentElement;
                                        }
                                    }
                                    if (!gLabel) gLabel = el.name || el.id || '';
                                    // Demographic / voluntary-self-id questions are not flagged
                                    // `required` but operator policy requires answering them —
                                    // gate submit until they're done (matches FORM STATUS logic).
                                    const DEMOGRAPHIC_RE = /\\b(gender identity|gender|racial|race|ethnic|sexual orientation|transgender|disabilit|veteran|armed forces|hispanic|latino)\\b/i;
                                    let isDemographic = DEMOGRAPHIC_RE.test(gLabel);
                                    if (!isDemographic) {
                                        const container = el.closest(
                                            '.application-question, .form-question, .question, '
                                          + '[class*="question"], [class*="Question"], fieldset, '
                                          + '[data-question-id], .field, [class*="field-"]'
                                        );
                                        if (container) {
                                            const txt = (container.textContent || '').slice(0, 400);
                                            if (DEMOGRAPHIC_RE.test(txt)) isDemographic = true;
                                        }
                                    }
                                    const isRequired = el.required
                                        || el.getAttribute('aria-required') === 'true'
                                        || (el.closest('[class*="required"]') !== null && !el.value)
                                        || isDemographic;
                                    if (!isRequired) return;
                                    const style = window.getComputedStyle(el);
                                    if (style.display === 'none' || style.visibility === 'hidden') return;
                                    // Skip file inputs — they never report .value reliably even
                                    // after a successful upload (Playwright's setInputFiles
                                    // attaches the file but the DOM value stays a path that
                                    // varies by browser). The upload_file action records
                                    // success separately; gating submit on input.value is
                                    // a false-positive trap.
                                    if (el.type === 'file') return;
                                    // Skip phantom fields with NO id, NO name, AND no label-for.
                                    // These can't be addressed by the AI (no selector to fill
                                    // them with) so blocking submit on them is an unresolvable
                                    // deadlock. They're almost always hidden honeypots,
                                    // react-select internal proxies, or stray inputs.
                                    const hasIdent = !!(el.id || el.name);
                                    const hasLabel = !!(
                                        el.getAttribute('aria-label')
                                        || (el.id && document.querySelector(`label[for="${el.id}"]`))
                                    );
                                    if (!hasIdent && !hasLabel) return;
                                    let val = '';
                                    if (el.tagName === 'SELECT') {
                                        val = el.value && el.options[el.selectedIndex]
                                              ? el.options[el.selectedIndex].text : '';
                                    } else if (el.type === 'checkbox' || el.type === 'radio') {
                                        // For radio groups, check if any sibling with same name is checked
                                        if (el.name) {
                                            const grp = document.querySelectorAll(
                                                `[name="${el.name}"]:checked`
                                            );
                                            val = grp.length ? 'checked' : '';
                                        } else {
                                            val = el.checked ? 'checked' : '';
                                        }
                                    } else {
                                        val = (el.value || '').trim();
                                    }
                                    // Custom react-select widgets — read both
                                    // single-value and multi-value (mark all
                                    // that apply) so a filled demographic
                                    // dropdown isn't treated as empty.
                                    if (!val && el.getAttribute('role') === 'combobox') {
                                        const ctrl = el.closest('.select__control, .react-select__control');
                                        if (ctrl) {
                                            const sv = ctrl.querySelector('.select__single-value, .react-select__single-value');
                                            if (sv) val = sv.textContent.trim();
                                            if (!val) {
                                                const mv = ctrl.querySelector('.select__multi-value, .react-select__multi-value, .select__multi-value__label');
                                                if (mv) val = mv.textContent.trim();
                                            }
                                        }
                                    }
                                    if (!val) {
                                        // Surface the BEST available label so the AI knows
                                        // which question to fill. Prefer the resolved label
                                        // (which already includes the parent-container walk);
                                        // fall back to container question text for demographic
                                        // fields whose <input> has no addressable label.
                                        let label = gLabel;
                                        if (!label || /^\\d+$/.test(label)) {
                                            const container = el.closest(
                                                '.application-question, .form-question, '
                                              + '[class*="question"], fieldset, .field'
                                            );
                                            if (container) {
                                                const ctxt = (container.textContent || '').trim();
                                                if (ctxt) label = ctxt.slice(0, 100);
                                            }
                                        }
                                        if (!label) label = el.name || el.id || '?';
                                        out.push({
                                            sel: el.id ? '#' + el.id : (el.name ? `[name="${el.name}"]` : ''),
                                            label: label.slice(0, 100),
                                        });
                                    }
                                });
                                return out;
                            }""")
                            await asyncio.sleep(1.0)
                            # Also pull visible validation-error messages. If
                            # the previous submit was rejected with a visible
                            # error, the form-state needs fixing BEFORE we
                            # let the AI re-click submit. Otherwise the AI
                            # spams submit and the server eventually triggers
                            # an email-verification / captcha challenge.
                            visible_errors = await page.evaluate("""() => {
                                const out = [];
                                const sels = [
                                    '.field-error', '.error-message', '.error',
                                    '[role="alert"]', '[aria-invalid="true"]',
                                    '[class*="invalid"]', '[class*="errorMessage"]',
                                    '.help-block.error', '.has-error',
                                    '.form-error', '.input-error',
                                ];
                                const seen = new Set();
                                sels.forEach(s => {
                                    document.querySelectorAll(s).forEach(el => {
                                        if (out.length >= 8) return;
                                        const txt = (el.textContent || '').trim();
                                        if (!txt || txt.length < 3 || txt.length > 200) return;
                                        const st = window.getComputedStyle(el);
                                        if (st.display === 'none' || st.visibility === 'hidden') return;
                                        if (seen.has(txt)) return;
                                        seen.add(txt);
                                        out.push(txt.slice(0, 160));
                                    });
                                });
                                return out;
                            }""")
                            if visible_errors:
                                logger.warning(
                                    f"[AgentLoop] PRE-SUBMIT GATE: {len(visible_errors)} "
                                    "validation error(s) visible on page — BLOCKING submit"
                                )
                                for e in visible_errors:
                                    logger.warning(f"  validation error: {e}")
                                action.ok = False
                                action.reason = (
                                    f"BLOCKED: validation errors on page — "
                                    f"FIX THESE NEXT: [{'; '.join(visible_errors[:5])}]"
                                )
                                action.raw["validation_errors"] = visible_errors
                                actions.append(action)
                                continue

                            # Safety hatch: if the gate has already blocked 2+
                            # submits and is STILL reporting the same phantom
                            # fields (no selector / no label), let the real
                            # form do the validation instead of looping forever.
                            prior_gate_blocks = sum(
                                1 for a in actions
                                if a.kind == "click"
                                and a.reason
                                and a.reason.startswith("BLOCKED:")
                            )
                            phantom_only = unfilled and all(
                                not (u.get("sel") or "").strip()
                                or (u.get("label") or "?") in ("?", "")
                                for u in unfilled
                            )
                            if unfilled and phantom_only and prior_gate_blocks >= 2:
                                logger.warning(
                                    f"[AgentLoop] PRE-SUBMIT GATE: {len(unfilled)} unfilled "
                                    "field(s) are all phantoms (no selector / no label) and "
                                    f"gate has already blocked {prior_gate_blocks} times — "
                                    "letting submit through; the form will surface real "
                                    "validation errors if any."
                                )
                                unfilled = []

                            if unfilled:
                                logger.warning(
                                    f"[AgentLoop] PRE-SUBMIT GATE: {len(unfilled)} required "
                                    f"field(s) still unfilled — BLOCKING submit, giving AI another turn"
                                )
                                for u in unfilled[:8]:
                                    logger.warning(f"  unfilled required: {u.get('label')!r} sel={u.get('sel')!r}")
                                # Record the action as "blocked" — don't actually click.
                                # Also stash the list of unfilled field labels into
                                # the action's reason so they appear in the next
                                # turn's history block — gives the AI explicit
                                # "fill these next" guidance instead of just
                                # vague "something's wrong".
                                unfilled_summary = ", ".join(
                                    f"{u.get('label', '?')[:40]}" for u in unfilled[:8]
                                )
                                action.ok = False
                                action.reason = (
                                    f"BLOCKED: {len(unfilled)} required field(s) still empty — "
                                    f"FILL THESE NEXT: [{unfilled_summary}]"
                                )
                                # Stash the structured unfilled list onto the action.raw
                                # too — useful if we ever want to render a richer
                                # explanation in the next turn's prompt.
                                action.raw["unfilled_required"] = [
                                    {"label": u.get("label"), "selector": u.get("sel")}
                                    for u in unfilled[:8]
                                ]
                                actions.append(action)
                                continue
                            else:
                                logger.info("[AgentLoop] PRE-SUBMIT GATE: all required fields filled — allowing submit")
                                # If the caller asked us to stop before any real submit
                                # (DRY_RUN_NO_SUBMIT=true), we've now done our due
                                # diligence — confirmed the form is complete — and
                                # exit successfully without actually clicking.
                                if self.stop_before_submit:
                                    logger.info(
                                        "[AgentLoop] stop_before_submit=True — "
                                        "form is complete, NOT clicking submit. Returning SUBMITTED."
                                    )
                                    actions.append(action)
                                    return LoopResult(
                                        success=True,
                                        status="SUBMITTED",
                                        confirmation="dry_run_stopped_before_submit",
                                        steps_taken=step,
                                        actions=actions,
                                    )
                        except Exception as exc:
                            logger.debug(f"[AgentLoop] pre-submit gate eval failed: {exc}")

                # ── Submit-loop breaker ──────────────────────────────────────
                # If the AI clicks Submit but the page didn't navigate (we'd
                # see a DOM change), the form is rejecting the submit because
                # of hidden validation errors below the fold. Auto-scroll to
                # bring them into view BEFORE the AI's next turn, so the next
                # screenshot + DOM-snapshot includes the error messages.

                _submit_pat = "submit"  # used by regex above via 're' module
                if _is_submit_click(action) and len(actions) >= 1:
                    prev_submit_clicks = sum(
                        1 for a in actions[-3:] if _is_submit_click(a)
                    )
                    if prev_submit_clicks >= 1:
                        logger.warning(
                            f"[AgentLoop] step={step} repeated submit click — "
                            "form is likely rejecting due to hidden validation errors. "
                            "Auto-scrolling to reveal error messages."
                        )
                        try:
                            # Scroll to the first visible validation error if any;
                            # otherwise scroll to the bottom of the form
                            scrolled = await page.evaluate("""() => {
                                const err = document.querySelector(
                                    '.field-error, .error-message, [aria-invalid="true"], '
                                    + '[class*="invalid"], [role="alert"]'
                                );
                                if (err) {
                                    err.scrollIntoView({behavior:'instant', block:'center'});
                                    return 'error_into_view';
                                }
                                window.scrollTo(0, document.body.scrollHeight);
                                return 'scrolled_to_bottom';
                            }""")
                            logger.info(f"[AgentLoop] auto-scroll on submit-loop: {scrolled}")
                            await asyncio.sleep(0.7)
                        except Exception as exc:
                            logger.debug(f"[AgentLoop] auto-scroll failed: {exc}")

                # ── Terminal actions ─────────────────────────────────────────────
                if action.kind == "abort":
                    reason = action.reason or "agent aborted"
                    # GATE: on the first few turns we DO NOT trust an abort — the AI
                    # often pattern-matches on a careers-search bar at the top of a
                    # company-mirrored ATS page and bails before scrolling/clicking
                    # Apply. Force it to keep trying: convert early aborts to
                    # scroll-then-look-for-Apply.
                    EARLY_ABORT_GATE = 6
                    if step <= EARLY_ABORT_GATE:
                        logger.warning(
                            f"[AgentLoop] step={step} suppressing early abort "
                            f"(reason={reason!r}); forcing scroll+click_apply recovery"
                        )
                        try:
                            await page.evaluate("window.scrollBy(0, 800)")
                            await asyncio.sleep(0.4)
                            for s in (
                                "a:has-text('Apply for this Job')",
                                "a:has-text('Apply Now')",
                                "a:has-text('Apply')",
                                "button:has-text('Apply')",
                                "a[href*='/apply']",
                            ):
                                try:
                                    loc = (frame or page).locator(s).first
                                    if await loc.count() > 0 and await loc.is_visible():
                                        await loc.scroll_into_view_if_needed()
                                        await loc.click(timeout=5000)
                                        await asyncio.sleep(2.5)
                                        logger.info(f"[AgentLoop] auto-recover clicked {s!r}")
                                        break
                                except Exception:
                                    continue
                        except Exception as exc:
                            logger.debug(f"[AgentLoop] auto-recover failed: {exc}")
                        # Continue the loop — give the AI one more turn with the new page
                        actions.append(action)
                        continue
                    logger.warning(f"[AgentLoop] ABORT: {reason}")
                    status = "WRONG_PAGE" if "newsletter" in reason.lower() or "wrong" in reason.lower() else "ABORTED"
                    return LoopResult(
                        success=False,
                        status=status,  # type: ignore[arg-type]
                        error=reason,
                        steps_taken=step,
                        actions=actions,
                    )

                if action.kind == "done":
                    actions.append(action)
                    logger.info(f"[AgentLoop] DONE after {step} steps: {action.confirmation!r}")
                    return LoopResult(
                        success=True,
                        status="SUBMITTED",
                        confirmation=action.confirmation,
                        steps_taken=step,
                        actions=actions,
                    )

                # ── verify_page gate ─────────────────────────────────────────────
                if action.kind == "verify_page":
                    page_verified = True
                    actions.append(action)
                    logger.info("[AgentLoop] page verified as job application form")
                    continue

                # Safety: only enforce verify-gate AFTER we've given the AI room to
                # click Apply / scroll / wait for SPA. Bumped 3 → 8 because the old
                # threshold was tripping on multi-page careers SPAs that need
                # navigation before the form ever appears.
                if not page_verified and step >= 8:
                    logger.warning("[AgentLoop] page never verified after 8 steps — aborting")
                    return LoopResult(
                        success=False,
                        status="WRONG_PAGE",
                        error="Agent did not verify page within 8 steps",
                        steps_taken=step,
                        actions=actions,
                    )

                # ── Execute action ───────────────────────────────────────────────
                # Stamp the pre-execute wall-clock so the verification-code
                # fetcher knows which inbox emails are "after submit".
                _pre_exec_epoch = int(time.time())
                ok = await _execute_action(
                    action, page, frame, self.resume_path, self.cover_letter_path
                )
                action.ok = ok

                # ── Email verification code wall (Greenhouse, etc.) ──────────
                # When the user submits a Greenhouse/Vercel-style hosted form,
                # the ATS sometimes emails an N-digit code to the candidate
                # and renders a code-entry page. Detect that shape and, if
                # the candidate has a `google_refresh_token` in the DB, fetch
                # the code from Gmail and fill it automatically.
                # No-ops cleanly when:
                #   - Gmail not connected on the candidate row
                #   - GOOGLE_CLIENT_ID / SECRET not in env
                #   - No matching email arrives within timeout
                # so this never causes a regression for non-Gmail candidates.
                _click_text = ((action.selector or "") + " " + (action.click_text or "")).lower()
                _is_submit = action.kind == "click" and bool(re.search(
                    r"submit|apply|send application", _click_text
                ))
                if ok and _is_submit:
                    try:
                        await self._handle_email_verification(
                            page, frame, after_epoch=_pre_exec_epoch, actions=actions
                        )
                    except Exception as exc:
                        logger.warning(
                            f"[AgentLoop] email-verification handler error "
                            f"(non-fatal): {exc}"
                        )

                # Lever 2: persist every successful fill into field_memory so
                # the NEXT application this candidate submits skips the LLM
                # entirely for that field. The recall happens in the pre-LLM
                # check below (see _try_memory_prefill).
                if ok and action.kind == "fill_field" and action.field_label and action.value:
                    try:
                        from ..forms import memory as _field_memory
                        _field_memory.remember(
                            label=action.field_label,
                            field_type="text",  # generic — memory module doesn't gate on this
                            value=str(action.value),
                            source="agent_loop",
                            candidate_id=self.candidate_id,
                        )
                    except Exception as exc:
                        logger.debug(f"[AgentLoop] memory.remember failed (non-fatal): {exc}")
                if not ok:
                    logger.debug(f"[AgentLoop] step={step} action {action.kind} returned ok=False")

                # ── Hallucinated-selector breaker ────────────────────────────
                # If the AI repeats a click on a selector that doesn't exist
                # on the page, it's pattern-completing from the DOM snapshot
                # rather than reading what's actually there. Detect 2 failed
                # clicks on the same selector in a row → abort with helpful
                # context so the AI's next turn (or the caller) knows.
                if action.kind == "click" and not ok and action.selector:
                    prev_fails = [
                        a for a in actions[-3:]
                        if a.kind == "click" and not a.ok and a.selector == action.selector
                    ]
                    if len(prev_fails) >= 2:
                        logger.warning(
                            f"[AgentLoop] step={step} selector {action.selector!r} failed "
                            f"{len(prev_fails) + 1} times — likely hallucinated. Forcing scroll "
                            "to refresh DOM view."
                        )
                        try:
                            await page.evaluate("window.scrollBy(0, 400)")
                            await asyncio.sleep(0.5)
                        except Exception:
                            pass

                # ── Auto-submit on READY ────────────────────────────────────
                # When the form is 100% filled (FORM STATUS shows READY) but
                # the AI fails to click Submit and instead scrolls or stalls,
                # the runner fires submit on its own. This is a SAFETY NET,
                # not the primary path — the AI's submit click is preferred
                # because it still goes through the pre-submit gate.
                if action.kind in ("scroll", "wait") and "READY FOR SUBMIT" in (dom or ""):
                    consecutive_idle = 0
                    for past in reversed(actions):
                        if past.kind in ("scroll", "wait", "verify_page"):
                            consecutive_idle += 1
                        else:
                            break
                    if consecutive_idle >= 2:
                        logger.warning(
                            f"[AgentLoop] step={step} FORM IS READY but AI is idling "
                            f"({consecutive_idle} consecutive non-acting turns) — "
                            "AUTO-FIRING submit on AI's behalf."
                        )
                        try:
                            submit_candidates = [
                                "button[type='submit']",
                                "button:has-text('Submit application')",
                                "button:has-text('Submit Application')",
                                "button:has-text('Submit')",
                                "input[type='submit']",
                            ]
                            clicked = False
                            for s in submit_candidates:
                                try:
                                    loc = (frame or page).locator(s).first
                                    if await loc.count() > 0 and await loc.is_visible():
                                        await loc.scroll_into_view_if_needed()
                                        if self.stop_before_submit:
                                            logger.info(
                                                f"[AgentLoop] stop_before_submit=True — "
                                                f"would have clicked {s!r}; returning SUBMITTED instead"
                                            )
                                            actions.append(action)
                                            return LoopResult(
                                                success=True,
                                                status="SUBMITTED",
                                                confirmation="auto_submit_when_ready_dry_run",
                                                steps_taken=step,
                                                actions=actions,
                                            )
                                        await loc.click(timeout=6000)
                                        clicked = True
                                        logger.info(f"[AgentLoop] auto-submit clicked {s!r}")
                                        await asyncio.sleep(2.5)
                                        break
                                except Exception:
                                    continue
                            if clicked:
                                # Move on — next turn will see the post-submit page
                                actions.append(action)
                                continue
                        except Exception as exc:
                            logger.debug(f"[AgentLoop] auto-submit attempt failed: {exc}")

                # ── Scroll-loop guard ────────────────────────────────────────
                # The stuck detector excludes scroll as "observational" and the
                # repetition guard only fires on selectored actions. Together
                # that leaves a hole: AI can scroll forever burning tokens.
                # Haiku just did this 51 times in a row on Vercel. Cap it.
                if action.kind == "scroll":
                    consecutive_scrolls = 0
                    for past in reversed(actions):
                        if past.kind == "scroll":
                            consecutive_scrolls += 1
                        else:
                            break
                    consecutive_scrolls += 1  # include current
                    if consecutive_scrolls >= 5:
                        # Last-ditch: before giving up STUCK, if there's a
                        # visible Submit button AND the form had at least some
                        # fields filled this session, try clicking it. Many
                        # "scroll forever" loops happen on filled forms where
                        # the AI just can't see the submit button — but
                        # Playwright can still find it. Worst case the form
                        # rejects with validation errors and we get clean
                        # feedback instead of a STUCK abort.
                        any_fills = any(a.kind == "fill_field" and a.ok for a in actions)
                        if any_fills and not self.stop_before_submit:
                            logger.warning(
                                f"[AgentLoop] step={step} SCROLL GUARD tripped — "
                                "form had fills this session, attempting last-ditch "
                                "auto-submit before aborting STUCK."
                            )
                            try:
                                submit_candidates = [
                                    "button[type='submit']",
                                    "button:has-text('Submit application')",
                                    "button:has-text('Submit Application')",
                                    "button:has-text('Submit')",
                                    "input[type='submit']",
                                ]
                                clicked = False
                                for s in submit_candidates:
                                    try:
                                        loc = (frame or page).locator(s).first
                                        if await loc.count() > 0:
                                            await loc.scroll_into_view_if_needed(timeout=3000)
                                            await loc.click(timeout=6000, force=True)
                                            clicked = True
                                            logger.info(
                                                f"[AgentLoop] last-ditch auto-submit clicked {s!r}"
                                            )
                                            await asyncio.sleep(3.0)
                                            break
                                    except Exception:
                                        continue
                                if clicked:
                                    actions.append(action)
                                    # Give the form a moment to navigate or
                                    # surface validation errors, then let the
                                    # loop continue one more turn to capture
                                    # the outcome.
                                    continue
                            except Exception as exc:
                                logger.debug(
                                    f"[AgentLoop] last-ditch auto-submit failed: {exc}"
                                )

                        logger.warning(
                            f"[AgentLoop] step={step} SCROLL GUARD: {consecutive_scrolls} "
                            "consecutive scrolls — aborting STUCK to prevent token burn. "
                            "If form is filled, the AI should be submitting; if not, "
                            "scrolling endlessly won't find what's missing."
                        )
                        actions.append(action)
                        return LoopResult(
                            success=False,
                            status="STUCK",
                            error=f"Scrolled {consecutive_scrolls} times in a row without acting",
                            steps_taken=step,
                            actions=actions,
                        )

                # ── Same-action repetition breaker (Haiku-loop guard) ─────────
                # If the AI repeats the EXACT same (kind, selector) for 5+
                # turns in a row regardless of ok/fail, it's stuck in a loop.
                # Haiku on Vercel was caught looping click+fill on the same
                # field 30 times. Abort with a clear STUCK so the caller can
                # escalate to a stronger model rather than burn tokens.
                if action.kind in ("click", "fill_field") and action.selector:
                    same_action_run = [
                        a for a in actions[-6:]
                        if a.kind == action.kind and a.selector == action.selector
                    ]
                    if len(same_action_run) >= 4:
                        logger.warning(
                            f"[AgentLoop] step={step} REPETITION GUARD: action "
                            f"({action.kind}, {action.selector!r}) repeated "
                            f"{len(same_action_run) + 1} times — aborting STUCK to "
                            "prevent runaway token burn."
                        )
                        actions.append(action)
                        return LoopResult(
                            success=False,
                            status="STUCK",
                            error=f"Looped on {action.kind}({action.selector!r}) {len(same_action_run) + 1}x",
                            steps_taken=step,
                            actions=actions,
                        )

                actions.append(action)

            # ── Max steps reached ────────────────────────────────────────────────
            logger.warning(f"[AgentLoop] reached MAX_STEPS={self.max_steps}")
            return LoopResult(
                success=False,
                status="MAX_STEPS",
                error=f"Exceeded {self.max_steps} steps without completing",
                steps_taken=self.max_steps,
                actions=actions,
            )
        finally:
            page.remove_listener("frameattached", on_frame_attached)
            page.remove_listener("framenavigated", on_frame_navigated)
            page.remove_listener("framedetached", on_frame_detached)
