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
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from playwright.async_api import Frame, Page

from ..adapters.hints import format_hints_for_prompt, get_platform_hints
from ..llm import LLMUnavailable, get_llm

logger = logging.getLogger(__name__)


def _linkedin_policy_value(profile: Optional[Dict[str, Any]]) -> str:
    """Value to fill into any LinkedIn-URL field, per operator policy.

    Default (unset / opted-out): ``"N/A"`` — the historical operator stance is
    "do NOT expose the candidate's real LinkedIn URL on application forms."

    Override: when the env var ``ALLOW_REAL_LINKEDIN`` is set to a truthy
    value (``1`` / ``true`` / ``yes``) AND the profile carries a real
    ``linkedin.com``-hosted URL, return that URL instead. Some ATSes (Lever
    in particular) treat LinkedIn as a REQUIRED field with client-side URL
    validation and silently reject the form when it holds the literal string
    ``"N/A"`` — the submit "succeeds" locally but the server never records
    the application. Opting in via env restores the real URL for those cases
    without changing the production default.
    """
    li = ((profile or {}).get("linkedin_url") or "").strip()
    if (
        os.getenv("ALLOW_REAL_LINKEDIN", "").strip().lower() in ("1", "true", "yes")
        and "linkedin.com" in li.lower()
    ):
        return li
    return "N/A"


# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

MAX_STEPS = int(__import__("os").getenv("AGENT_LOOP_MAX_STEPS", "60"))   # hard cap on loop iterations
# Wall-clock ceiling. Even if MAX_STEPS isn't reached, the loop ABORTS after
# this many seconds so a confused AI on an unsupported site doesn't burn a
# tester's patience. Env-overridable: AGENT_LOOP_WALL_TIMEOUT_S.
_DEFAULT_WALL_TIMEOUT_S = float(__import__("os").getenv("AGENT_LOOP_WALL_TIMEOUT_S", "240"))
# Extra wall-clock budget granted AFTER a real submit fires, reserved for the
# post-submit email-verification phase (Gmail code polling, up to ~3×90s).
# Without this the form-fill budget would expire mid-poll and the code would
# never be fetched. Env-overridable: AGENT_LOOP_POST_SUBMIT_GRACE_S.
_POST_SUBMIT_GRACE_S = float(__import__("os").getenv("AGENT_LOOP_POST_SUBMIT_GRACE_S", "330"))
STUCK_THRESHOLD = 4     # consecutive no-DOM-change steps before abort
LLM_RETRY_LIMIT = 3     # consecutive LLM failures before abort

# Server-side rejection banners some ATSes (Ashby, Workday, Lever) render as a
# normal 200 OK response instead of an HTTP error — the page just shows a red
# banner where a success message would be. The post-submit "no code screen
# after 2 turns -> SUBMITTED" check below previously only looked for an OTP
# wall; it never read the page content, so a rejected submission with no OTP
# screen was reported as a false-positive SUBMITTED. Checked platform-
# agnostically here (not just in adapters/ashby.py's verify_success(), which
# only runs on the deterministic-fallback path — never reached when AgentLoop
# completes the whole flow itself, the common case).
_POST_SUBMIT_REJECTION_PATTERNS = (
    "flagged as possible spam",
    "couldn't submit your application",
    "could not submit your application",
    "submission was flagged",
    "we were unable to submit",
    "already applied",
    "already submitted an application",
    "you've already applied",
    "you have already applied",
    "already have an application on file",
)
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
    "solve_captcha",
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
    # solve_captcha
    captcha_type: Optional[str] = None   # "recaptcha_v2" | "hcaptcha" | "image"
    # internal — set by the executor after the action runs
    step: int = 0
    ok: bool = True                      # False if Playwright couldn't execute
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LoopResult:
    success: bool
    status: Literal[
        "SUBMITTED", "FORM_COMPLETED", "ABORTED", "MAX_STEPS",
        "STUCK", "LLM_UNAVAILABLE", "WRONG_PAGE", "ERROR",
        "VERIFICATION_FAILED",
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
  solve_captcha  — {captcha_type: "recaptcha_v2" | "hcaptcha" | "image"}

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
8. CAPTCHA detected → return solve_captcha with captcha_type="recaptcha_v2" | "hcaptcha" | "image"
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
12b-bind. **CRITICAL — SELECTOR/LABEL BINDING.** When filling a field, the
    selector you emit MUST be the one whose LABEL in the DOM snapshot matches
    the question you intend to answer. The Vercel/Greenhouse demographic
    block has SIX adjacent fields with very similar IDs (e.g. 4015780004 /
    4015781004 / 4015783004 / 4015785004 / 4015786004 / 4015790004). Each
    ID belongs to a SPECIFIC question:
      * Read the DOM snapshot, find the line whose label STARTS WITH
        "How would you describe your <topic>" or "Do you identify as…" or
        "Are you a veteran…" or "Do you have a disability…"
      * The selector on THAT line is the one to use — do NOT reuse a
        neighbour's selector even if the IDs are close.
    The runner validates this: if the selector you emit resolves to a label
    in a different demographic group than your `field_label`, the fill is
    REFUSED and the action is marked ❌FAILED. You will see the warning in
    history and must re-emit with the correct selector.
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
12dd. **"DO YOU LIVE/RESIDE IN <list of specific places>?" QUESTIONS ARE A
    LOOKUP, NOT A GUESS.** These are Yes/No compliance gates that name a
    specific state, province, or country list (e.g. "Do you live in one of
    the following states? Alabama, Alaska, Delaware, Kansas, ..."). Compare
    the candidate's ACTUAL location (from the identity card above) against
    the named list, character by character:
      * If the candidate's city/state/country is explicitly named in the
        list → answer the affirmative option ("Yes").
      * If the candidate's city/state/country is NOT in the list → answer
        the negative option ("No"). This is the common case — most of these
        lists are a handful of specific states, and the candidate lives in
        exactly one place.
    Do NOT answer "Yes" just because "Yes" is a valid menu option — validity
    and correctness are different things. If the field's own label got
    truncated in the DOM snapshot (e.g. cut off mid-list), that does NOT
    excuse guessing: re-scroll or check the FULL label text before picking.
12e. **EDUCATION / WORK-HISTORY FIELDS COME FROM THE RESUME, NEVER A GUESS.**
    Degree, Discipline/Major, School/University, Graduation Year, Employer,
    Job Title, Years of Experience, Skills — the RESUME CONTENT block above
    is the ONLY source for these. Read its Education and Experience sections
    BEFORE answering:
      * "Degree" dropdown → match the resume's actual degree level (e.g.
        resume says "BS Software Engineering" → pick "Bachelor's Degree",
        NOT "Other" and NOT a random guess). "Other" is a last resort ONLY
        when the resume's education section is genuinely absent.
      * "Discipline"/"Major"/"Field of Study" → the exact major on the
        resume (e.g. "Software Engineering"), not a plausible-sounding
        substitute like "Computer Science" — those are different values on
        most Greenhouse dropdowns even though they're adjacent fields.
      * "Current/most recent employer", "job title", "years of experience"
        → pulled from the resume's most recent Experience entry, not
        invented. If the resume genuinely has none (e.g. entry-level), use
        the identity card's declared experience_years and answer honestly
        (e.g. "N/A" for employer only when there truly is none).
    If the RESUME CONTENT block is empty/absent, fall back to the identity
    card's `experience_years` / `tech_stack` fields — do not fabricate a
    company, school, or degree that appears nowhere in your context.
12f. **OPEN-ENDED TEXT BOXES NEED A REAL, GROUNDED ANSWER — NOT A PLACEHOLDER.**
    When a field is a genuine essay/free-text question ("Why do you want to
    work here?", "Tell us about yourself", "Describe a project you're proud
    of", "What makes you a good fit for this role?") — write a specific,
    2-4 sentence, first-person answer that:
      * References something CONCRETE from the RESUME CONTENT block (a real
        technology, project, or past role — never invent an employer,
        project, or achievement that isn't in the resume).
      * Connects it to something CONCRETE in the JOB card above (the title,
        company, or a phrase from the job description summary).
    Do NOT write generic filler ("I am a hard worker", "I would love this
    opportunity", "I am passionate about technology") — that reads as
    obviously templated to a recruiter. Do NOT answer "N/A" for a question
    you're capable of answering well; "N/A" is reserved for fields that
    genuinely don't apply (e.g. a GitHub URL field when the candidate has
    none). Plain text only — no markdown formatting, no bullet points; most
    ATS textareas render markdown literally as asterisks and hashes.
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
# Deterministic-prefill scan — visible fillable fields + resolved labels.
# Used by _deterministic_prefill to fill all fixed-policy fields without the LLM.
# ─────────────────────────────────────────────────────────────────────────────

_POLICY_SCAN_JS = r"""() => {
    const out = [];
    const seen = new Set();
    const skip = new Set(['hidden','submit','button','image','reset','file','search']);
    function vis(el){const s=window.getComputedStyle(el); if(s.display==='none')return false; if(s.visibility==='hidden'&&el.type!=='radio'&&el.type!=='checkbox')return false; return true;}
    function labelFor(el){
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
        if (el.id){
            const l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if(l) return (l.textContent||'').trim();
            if (el.id.endsWith('--input')){const b=el.id.slice(0,-7); const l2=document.querySelector('label[for="'+CSS.escape(b)+'"]'); if(l2) return (l2.textContent||'').trim();}
        }
        const fs=el.closest('fieldset'); if(fs){const lg=fs.querySelector('legend'); if(lg) return (lg.textContent||'').trim();}
        let p=el.parentElement;
        for(let i=0;i<6&&p;i++){const l=p.querySelector(':scope > label, :scope > .application-question__label, :scope > .question-label, :scope > div > label'); if(l) return (l.textContent||'').trim(); p=p.parentElement;}
        const c=el.closest('.application-question,[class*="question"],fieldset,.field'); return c ? (c.textContent||'').slice(0,500).trim() : (el.name||el.id||'');
    }
    document.querySelectorAll('input, select, textarea, [role="combobox"]').forEach(el => {
        if (out.length >= 60) return;
        const type=(el.type||el.tagName.toLowerCase()).toLowerCase();
        if (skip.has(type)) return;
        if (!vis(el)) return;
        const role = el.getAttribute('role');
        const sel = el.id ? '#'+CSS.escape(el.id) : (el.name ? el.tagName.toLowerCase()+'[name="'+el.name+'"]' : '');
        if (!sel || seen.has(sel)) return; seen.add(sel);
        let value='';
        if (el.tagName==='SELECT'){ value = el.value && el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : ''; }
        else if (type==='checkbox'||type==='radio'){ value = el.checked ? 'checked' : ''; }
        else { value = (el.value||'').trim(); }
        if (!value && role==='combobox'){ const ctrl=el.closest('[class*="select__control"],[class*="-control"]'); if(ctrl){const sv=ctrl.querySelector('[class*="singleValue"],[class*="-singleValue"],[class*="multi-value"]'); if(sv) value=sv.textContent.trim();} }
        // Bumped slice 160 -> 500 so long consent labels (e.g. dv01's "you
        // confirm that you will work within the united states...") fit in
        // full and the policy regex can match against the FULL question text.
        const entry={ sel, type: (role==='combobox'?'combobox':type), label: labelFor(el).slice(0,500), value };
        out.push(entry);
    });
    return out;
}"""

# Reads option text from any currently-open react-select / listbox menu.
_RENDERED_OPTIONS_JS = r"""() => {
    const out=[]; const seen=new Set();
    document.querySelectorAll(
        '.select__menu .select__option, .react-select__menu .react-select__option,'
      + '[role="listbox"] [role="option"], .select__menu [role="option"]'
    ).forEach(el => { const t=(el.textContent||'').trim(); if(t&&!seen.has(t)){seen.add(t); out.push(t);} });
    return out;
}"""

# Reads options ONLY from the menu the given field controls (via
# aria-controls / aria-owns / its own select container). This prevents the
# phone widget's 200-country dropdown — and any other open react-select on
# the page — from polluting the option list, which caused consent fields
# (whose real option is "Acknowledge/Confirm") to be missed.
_SCOPED_OPTIONS_JS = r"""(s) => {
    const el = document.querySelector(s);
    if (!el) return [];
    let menu = null;
    const lid = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
    if (lid) menu = document.getElementById(lid);
    if (!menu) {
        const cont = el.closest('[class*="select__container"],[class*="-container"],[class*="select"]');
        if (cont) menu = cont.querySelector('[class*="select__menu"],[role="listbox"]');
    }
    const scope = menu || document;
    const out = []; const seen = new Set();
    scope.querySelectorAll('[class*="select__option"],[role="option"]').forEach(el => {
        const t = (el.textContent || '').trim();
        if (t && !seen.has(t)) { seen.add(t); out.push(t); }
    });
    return out;
}"""


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
            // getComputedStyle only reflects the element's OWN display/
            // visibility — a hidden ANCESTOR (e.g. Lever's permanently
            // display:none `.resume-upload-oversize` wrapper) doesn't show up
            // here, so an inner error-message tag reads as "visible" even
            // though nothing is rendered on screen, poisoning every turn's
            // context with a phantom error the AI can never resolve.
            // checkVisibility() walks the whole ancestor chain.
            const reallyVisible = typeof el.checkVisibility === 'function'
                ? el.checkVisibility({checkVisibilityCSS: true, checkOpacity: true})
                : el.offsetParent !== null;
            if (!reallyVisible) return;
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
        let entryLabel = labelFor(el);
        // Ashby (and similar React ATS builders) mark a question required by
        // putting a "required"-named CSS-module class on the QUESTION LABEL
        // element, e.g. `_required_f7cvd_91` — never `required`/`aria-required`
        // on the input itself, and the label is a SIBLING of the input's
        // wrapper, not an ancestor of the input — so `el.closest('[class*=
        // "required"]')` (walking the INPUT's own ancestors) never finds it.
        // The one container that reliably wraps EVERY Ashby question (native
        // input, combobox, standalone checkbox, or a fieldset-grouped radio/
        // checkbox set) is `[data-field-path]`. Search WITHIN that container
        // for a required-marked descendant instead of walking the input's
        // ancestors.
        const reqContainer = el.closest('[data-field-path]');
        const containerRequired = !!(reqContainer && reqContainer.querySelector('[class*="required" i]'));
        let entryRequired = el.required || el.getAttribute('aria-required') === 'true' || containerRequired;
        // Multi-checkbox "select all that apply" groups (Ashby's pattern:
        // a <fieldset> with a question-title <label> that is NOT `for`-
        // associated with any single checkbox, plus 2+ checkboxes each
        // individually labeled with just the OPTION text, e.g. "Boston
        // (Cambridge)"). Without this, the AI sees a pile of checkboxes
        // labeled with city/option names and no visible connection to the
        // actual question or its required status — so it never engages
        // with them at all. Prefix the option label with the resolved
        // question text.
        // Radio groups have the identical labeling problem: each option's own
        // `label[for=radio.id]` is just the option text ("No AI", "1-2",
        // "Chat Prompting & In-App AI"), never `for`-associated with the
        // fieldset's real question label ("What is your fluency level with
        // modern coding workflows?"). Without the prefix the AI sees 5
        // disconnected option labels with no indication they're one question.
        if (type === 'checkbox' || type === 'radio') {
            const fs = el.closest('fieldset');
            const groupSelector = type === 'checkbox' ? 'input[type="checkbox"]' : 'input[type="radio"]';
            if (fs && (type === 'radio' || fs.querySelectorAll(groupSelector).length >= 2)) {
                const qLabel = fs.querySelector(':scope > label, :scope > legend');
                const qText = qLabel ? qLabel.textContent.trim() : '';
                if (qText && qText !== entryLabel) {
                    entryLabel = qText.slice(0, 100) + ' -> ' + entryLabel;
                }
            }
        }
        const entry = { sel, type, label: entryLabel, required: entryRequired };
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
        // Ashby's own combobox inputs (e.g. the "Location" autocomplete)
        // often carry NO id at all — falling back to `null` here silently
        // DROPPED the field from the AI's view entirely (it never even
        // appeared as "unaddressable", it just vanished). Fall back to a
        // selector scoped by the nearest [data-field-path] container, which
        // reliably wraps every Ashby question even when the input itself
        // has no id/name.
        const fieldPathEl = el.closest('[data-field-path]');
        const fieldPath = fieldPathEl ? fieldPathEl.getAttribute('data-field-path') : '';
        const sel = el.id ? '#' + el.id
            : (fieldPath ? `[data-field-path="${fieldPath}"] [role="combobox"]` : null);
        if (!sel || seen.has(sel)) return;
        seen.add(sel);
        const reqContainer = el.closest('[data-field-path]');
        const containerRequired = !!(reqContainer && reqContainer.querySelector('[class*="required" i]'));
        const entry = {
            sel, type: 'combobox', label: labelFor(el),
            required: el.getAttribute('aria-required') === 'true' || containerRequired,
        };
        // Surface the choosable options so the AI answers from them on the FIRST
        // turn instead of guessing from the question text (e.g. naming a state
        // for a "Do you live in one of these states?" Yes/No dropdown). Sources,
        // in order: a hidden native <select> Greenhouse syncs the widget with,
        // the listbox this combobox controls (aria-controls/owns), or already
        // rendered option nodes. react-select renders its menu only when open,
        // so any of these may be empty — that's fine, we just omit options then.
        try {
            let opts = [];
            const container = el.closest('.application-question, .form-question, [class*="question"], fieldset, [class*="field"]') || el.parentElement;
            if (container) {
                const realSel = container.querySelector('select');
                if (realSel) opts = Array.from(realSel.options).map(o => (o.text || '').trim()).filter(Boolean);
            }
            if (!opts.length) {
                const lid = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
                const lb = lid ? document.getElementById(lid) : null;
                const scope = lb || container || document;
                opts = Array.from(scope.querySelectorAll('[role="option"], .select__option, .react-select__option'))
                            .map(o => (o.textContent || '').trim()).filter(Boolean);
            }
            opts = opts.filter((v, i, a) => v && a.indexOf(v) === i);
            if (opts.length && opts.length <= 15) entry.options = opts;
        } catch (e) {}
        out.push(entry);
    });

    // Visible buttons (Next / Submit / Apply — AND Ashby's Yes/No toggle
    // question widget, which answers a required field via two <button>
    // elements instead of a visible checkbox/radio: the real form-bound
    // <input type="checkbox"> is display:none, so it's excluded above, and
    // this is the AI's ONLY way to actually see and click the real answer
    // control).
    document.querySelectorAll('button, input[type="submit"], input[type="button"]').forEach(el => {
        if (out.length >= MAX_FIELDS) return;
        if (inConsentBanner(el)) return;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return;
        const text = el.textContent.trim() || el.value || '';
        if (!text) return;
        // If this button lives inside an Ashby question container
        // ([data-field-path]), it's answering THAT specific question, not
        // acting as a page-level Submit/Next/Apply control. Prefix its
        // label with the resolved question text and scope the selector to
        // the container so a same-text button on a DIFFERENT question
        // ("Yes"/"No" pairs are common) can't be confused with this one.
        const fieldPathEl = el.closest('[data-field-path]');
        let label = text.slice(0, 80);
        let sel = el.id ? '#' + el.id : 'button:has-text("' + text.slice(0, 40) + '")';
        if (fieldPathEl) {
            const fp = fieldPathEl.getAttribute('data-field-path');
            const qLabel = fieldPathEl.querySelector(':scope > label, :scope > legend');
            const qText = qLabel ? qLabel.textContent.trim() : '';
            if (qText) label = qText.slice(0, 100) + ' -> ' + label;
            if (fp) sel = `[data-field-path="${fp}"] button:has-text("${text.slice(0, 40)}")`;
        }
        if (seen.has(sel)) return;
        seen.add(sel);
        out.push({ sel, type: 'button', label });
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
    const seenRadioGroups = new Set();
    const seenCheckboxFieldsets = new Set();
    document.querySelectorAll(
        'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]):not([type=reset]), '
      + 'select, textarea, [role="combobox"]'
    ).forEach(el => {
        if (inConsentBanner(el)) return;
        if (inPhoneWidget(el)) return;
        // Radio groups: HTML forms almost never set `required`/`aria-required`
        // on the individual <input type="radio"> — it lives on the group as a
        // whole (an asterisk in the question label, or aria-required/a
        // "required" class on the fieldset wrapping the group). The per-
        // element checks below (el.required, el.getAttribute('aria-required'))
        // are essentially always false for radios, so without this a required
        // radio-button question (e.g. a Yes/No compliance question rendered
        // as radios instead of a dropdown) never enters totalRequired at all
        // — the pre-submit gate reports "all required fields filled" and lets
        // Submit fire while a visible, unanswered radio question sits on the
        // page. Also dedupe by `name` so an N-option group counts as ONE
        // required field, not N.
        if (el.type === 'radio' && el.name) {
            if (seenRadioGroups.has(el.name)) return;
            seenRadioGroups.add(el.name);
        }
        // "Select all that apply" checkbox groups (Ashby: a <fieldset> with
        // 2+ checkboxes, each carrying the OPTION TEXT as its own unique
        // `name` — e.g. name="Boston (Cambridge)" — so there's no shared
        // name to dedupe by like radios. Group by the fieldset element
        // itself instead. Semantics: required means "at least one checked",
        // not "every checkbox checked" — treated as ONE field, satisfied if
        // ANY sibling in the same fieldset is checked (see val computation
        // below, which already unions by name and would otherwise mark
        // every unchecked option in the group as its own unfilled required
        // field).
        let checkboxGroupFieldset = null;
        if (el.type === 'checkbox') {
            const fs = el.closest('fieldset');
            if (fs && fs.querySelectorAll('input[type="checkbox"]').length >= 2) {
                if (seenCheckboxFieldsets.has(fs)) return;
                seenCheckboxFieldsets.add(fs);
                checkboxGroupFieldset = fs;
            }
        }
        const style = window.getComputedStyle(el);
        // Ashby's Yes/No toggle-question widget keeps the real, form-bound
        // <input type="checkbox"> as display:none and shows two custom
        // <button>Yes</button>/<button>No</button> elements the user actually
        // clicks — but Ashby's own React state still syncs onto the hidden
        // checkbox's `checked` property, so it's still the authoritative
        // answer even though invisible. Exempt checkbox/radio from the
        // display:none filter (mirrors the existing visibility:hidden
        // exemption below) so these required Yes/No questions are still
        // tracked instead of silently vanishing from required-field counting.
        if (style.display === 'none' && el.type !== 'radio' && el.type !== 'checkbox') return;
        if (style.visibility === 'hidden') return;
        // Resolve the field label early so we can detect demographic questions.
        // For a checkbox-group representative, prefer the fieldset's own
        // question-title label over the individual option's label (which
        // would just be the option text, e.g. "Boston (Cambridge)", and
        // never carries the group's asterisk/required marker).
        let fsLabel = checkboxGroupFieldset
            ? (checkboxGroupFieldset.querySelector(':scope > label, :scope > legend')?.textContent || '').trim()
            : '';
        if (!fsLabel) fsLabel = el.getAttribute('aria-label') || '';
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
        // Asterisk-in-label is the most common REAL-WORLD required marker —
        // many ATS question wrappers (especially radio-button questions,
        // which almost never carry `required`/`aria-required` on the
        // individual <input>) rely entirely on a visible "*" in the question
        // text with no matching DOM attribute at all. The Python-side
        // detect_form() already treats "*" in label as required for
        // consistency; mirror that signal here.
        // Ashby-style builders mark required on the QUESTION LABEL (a sibling
        // of the input's wrapper), not on the input itself or its ancestors —
        // el.closest('[class*="required"]') walks the wrong direction for
        // that pattern. [data-field-path] reliably wraps every Ashby question
        // regardless of field type; search WITHIN it for the marker instead.
        const reqContainer_fs = el.closest('[data-field-path]');
        const containerRequired_fs = !!(reqContainer_fs && reqContainer_fs.querySelector('[class*="required" i]'));
        const isRequired = el.required
            || el.getAttribute('aria-required') === 'true'
            || (el.closest('[class*="required"]') !== null)
            || isDemographic
            || /\\*/.test(fsLabel)
            || containerRequired_fs;
        if (!isRequired) return;
        // Mirror the pre-submit gate's phantom-field filter — these
        // computations MUST agree, otherwise the AI sees "NOT READY"
        // (FORM STATUS) but the gate would allow submit (mismatch =
        // scroll-loop-until-STUCK). Skip:
        //  • file inputs — .value is unreliable after Playwright upload
        //  • fields with no id, no name, AND no label — unaddressable
        //    phantoms (honeypots, react-select internal proxies, etc.)
        if (el.type === 'file') return;
        // Ashby's own combobox inputs (e.g. "Location") often have EMPTY id
        // AND name — without the data-field-path fallback these were wrongly
        // treated as unaddressable phantoms and silently dropped, so FORM
        // STATUS never even counted them (matching the gate's mirror fix).
        const hasIdent_fs = !!(el.id || el.name || reqContainer_fs?.getAttribute('data-field-path'));
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
            // Ashby's Yes/No toggle widget: a hidden checkbox paired with two
            // sibling <button>Yes</button>/<button>No</button> elements the
            // user actually clicks. The checkbox's .checked property is NOT
            // reliable here — some builds only set checked=true when "Yes"
            // is clicked and never set it for "No" (checked=false is then
            // ambiguous between "never answered" and "answered No"),
            // causing the AI to loop forever re-clicking "No" because
            // nothing it reads ever shows the question as answered. The one
            // signal that's reliable regardless of which option was picked
            // is the "active"-named class Ashby applies to whichever button
            // was clicked.
            const toggleContainer = el.closest('[data-field-path]');
            const activeToggleBtn = toggleContainer ? toggleContainer.querySelector('button[class*="active" i]') : null;
            if (activeToggleBtn) {
                val = 'checked';
            } else if (checkboxGroupFieldset) {
                // Group semantics: satisfied if ANY checkbox anywhere in the
                // fieldset is checked, not just this specific option (each
                // option has its own unique name, so a by-name query would
                // only ever see this one option's state).
                val = checkboxGroupFieldset.querySelector('input[type="checkbox"]:checked') ? 'checked' : '';
            } else if (el.name) {
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
    """SHA1 of form input values and basic text/node structure. 
    Filters out innerHTML which changes constantly due to loaders/ads."""
    try:
        data = await ctx.evaluate("""() => {
            let vals = Array.from(document.querySelectorAll('input, select, textarea')).map(e => {
                if (e.type === 'checkbox' || e.type === 'radio') return e.checked;
                return e.value;
            }).join('|');
            let formText = Array.from(document.querySelectorAll('form, button, [role="button"], label, h1, h2')).map(e => (e.textContent || '').trim()).join('|');
            let nodeCount = document.querySelectorAll('*').length;
            return vals + '|' + formText + '|' + nodeCount;
        }""")
        return hashlib.sha1(data.encode()).hexdigest()[:16]
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Action parser
# ─────────────────────────────────────────────────────────────────────────────

_VALID_KINDS: set[str] = {
    "verify_page", "fill_field", "upload_file", "click", "click_apply",
    "scroll", "next_step", "navigate_url", "wait", "abort", "done", "solve_captcha",
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
        captcha_type=raw.get("captcha_type") or None,
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
    # #<id>... → [id="<id>"]<rest> when the id is invalid as a CSS ident.
    # Triggered when:
    #   (a) first char after # is a digit (e.g. Vercel/Greenhouse digit IDs), OR
    #   (b) the id chunk contains a CSS-meta char that breaks tokenization
    #       (e.g. Greenhouse multi-select IDs like "question_37045238002[]"
    #        where the trailing []s make the whole selector unparseable).
    if len(s) >= 2 and s[0] == "#":
        body = s[1:]
        # The "id chunk" runs until a selector combinator/separator.
        # We treat ANY char other than [A-Za-z0-9_-] as id-terminating, then
        # decide whether the chunk needs the [id="…"] rewrite.
        cut = 0
        for ch in body:
            if ch.isalnum() or ch in ("_", "-"):
                cut += 1
            else:
                break
        ident, rest = body[:cut], body[cut:]
        # First char digit  → always rewrite (case a).
        # rest is a clearly pathological literal suffix that belongs to the id
        # token, not a real CSS combinator/attr-selector. We deliberately do
        # NOT trigger on `[attr=val]` etc. — those are valid CSS and the LLM
        # never emits a legit attribute selector after a malformed id anyway.
        starts_with_digit = ident and ident[0].isdigit()
        needs_attr_form = rest.startswith(("[]", "()", "{}"))
        if starts_with_digit or needs_attr_form:
            # Consume trailing junk that belongs to the id token (the literal
            # `[]` or `()` after question_37045238002). DO NOT consume `.` —
            # that begins a class selector and is a real combinator.
            extra = 0
            for ch in rest:
                if ch in ("[", "]", "(", ")"):
                    extra += 1
                else:
                    break
            full_ident = ident + rest[:extra]
            tail = rest[extra:]
            # Inside attr-value double quotes, [, ], (, ), . are all literal.
            # Only " and \ need escaping per CSS spec.
            escaped = full_ident.replace("\\", "\\\\").replace('"', '\\"')
            return f'[id="{escaped}"]' + tail
    return sel


async def _bezier_mouse_move(page: Page, target_x: float, target_y: float):
    import random
    import asyncio
    start_x = max(0, target_x + random.uniform(-200, 200))
    start_y = max(0, target_y + random.uniform(-200, 200))
    await page.mouse.move(start_x, start_y)
    
    cp_x = (start_x + target_x) / 2 + random.uniform(-100, 100)
    cp_y = (start_y + target_y) / 2 + random.uniform(-100, 100)
    
    steps = random.randint(15, 30)
    for i in range(1, steps + 1):
        t = i / steps
        x = (1-t)**2 * start_x + 2*(1-t)*t * cp_x + t**2 * target_x
        y = (1-t)**2 * start_y + 2*(1-t)*t * cp_y + t**2 * target_y
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.01, 0.03))

async def _human_scroll(page: Page, delta: float):
    import random
    import asyncio
    chunks = random.randint(3, 6)
    chunk_size = delta / chunks
    for _ in range(chunks):
        jitter = chunk_size * random.uniform(0.8, 1.2)
        await page.evaluate(f"window.scrollBy(0, {jitter})")
        await asyncio.sleep(random.uniform(0.05, 0.15))


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
                    box = await loc.bounding_box()
                    if box:
                        await _bezier_mouse_move(page, box["x"] + box["width"]/2, box["y"] + box["height"]/2)
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
        if direction == "down":
            # Travel most of the remaining distance to the true bottom in one
            # motion instead of a fixed 600px hop. A fixed hop needed 5+ "down"
            # scrolls to clear a long Greenhouse page (full job description +
            # EEO/demographic block), which tripped the consecutive-scroll
            # STUCK guard before the AI ever reached the Submit button or the
            # last demographic fields. Re-measured every call so dynamically
            # loaded content (lazy sections, expanding selects) is accounted
            # for on the next scroll if one motion doesn't fully clear it.
            try:
                metrics = await page.evaluate("""() => ({
                    scrollY: window.scrollY || document.documentElement.scrollTop,
                    innerHeight: window.innerHeight,
                    scrollHeight: Math.max(
                        document.body.scrollHeight,
                        document.documentElement.scrollHeight
                    ),
                })""")
                remaining = metrics["scrollHeight"] - (metrics["scrollY"] + metrics["innerHeight"])
                # Floor at the old 600px so short pages still get a visible
                # nudge; cap at ~2.5 viewports per action so the motion still
                # reads as human scrolling rather than an instant teleport.
                delta = max(600.0, min(remaining + 80.0, metrics["innerHeight"] * 2.5))
            except Exception as exc:
                logger.debug(f"[AgentLoop] scroll metrics read failed, using fallback delta: {exc}")
                delta = 1400.0
        else:
            delta = -1400.0
        await _human_scroll(page, delta)
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
                    box = await loc.bounding_box()
                    if box:
                        await _bezier_mouse_move(page, box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                    await loc.click(timeout=6000)
                    await asyncio.sleep(1.0)
                    return True
            except Exception:
                continue
        logger.warning("[AgentLoop] next_step: no Next button found")
        return False

    if kind == "solve_captcha":
        from ..captcha.service import CaptchaService
        if not action.captcha_type:
            logger.warning("[AgentLoop] solve_captcha: missing captcha_type")
            return False
        
        logger.info(f"[AgentLoop] LLM requested solve_captcha for type={action.captcha_type!r}")
        captcha_svc = CaptchaService() 
        solution = await captcha_svc.solve(page, action.captcha_type)
        if solution.success:
            logger.info(f"[AgentLoop] Captcha solved successfully in {solution.solve_time_seconds:.1f}s")
            await asyncio.sleep(2.0)
            return True
        else:
            logger.warning("[AgentLoop] CaptchaService failed to solve captcha")
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
                    box = await loc.bounding_box()
                    if box:
                        await _bezier_mouse_move(page, box["x"] + box["width"]/2, box["y"] + box["height"]/2)
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
            _li_target = _linkedin_policy_value(self.profile)
            if value.strip() != _li_target and value.strip().upper() != _li_target.upper():
                logger.info(
                    f"[AgentLoop] LinkedIn policy override: AI proposed {value[:50]!r} "
                    f"for {action.field_label!r}, forcing {_li_target!r}"
                )
                value = _li_target
                action.value = _li_target
        try:
            loc = ctx.locator(sel).first
            if await loc.count() == 0:
                logger.warning(f"[AgentLoop] fill_field: selector not found: {sel!r}")
                return False
            field_type = await loc.evaluate("el => el.type || el.tagName.toLowerCase()")
            field_type = (field_type or "text").lower()

            # ── Selector / label cross-check (demographic-swap defence) ──
            # The Vercel/Greenhouse demographic block has very similar field
            # types in adjacent rows — gender, race, orientation, transgender,
            # disability, veteran. The AI sometimes proposes the right
            # field_label but a neighbour's selector ID, causing the wrong
            # field to get filled (e.g. "Male" written into "sexual
            # orientation" because the AI grabbed the orientation field's
            # ID). On a "mark all that apply" multi-select this leaves the
            # wrong field with two values and the server silently rejects
            # the submit.
            #
            # Check: look up the actual label for `sel` and confirm it
            # shares a demographic keyword with the AI's `field_label`. If
            # not, abort the fill so the AI can re-plan with the right ID.
            ai_label = (action.field_label or "").lower()
            if ai_label:
                try:
                    actual_label = await loc.evaluate(
                        """(el) => {
                            // Resolve label using the same walk we do elsewhere.
                            if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
                            if (el.id) {
                                const l = document.querySelector('label[for="' + el.id + '"]');
                                if (l) return (l.textContent || '').trim();
                                if (el.id.endsWith('--input')) {
                                    const b = el.id.slice(0, -7);
                                    const l2 = document.querySelector('label[for="' + b + '"]');
                                    if (l2) return (l2.textContent || '').trim();
                                }
                            }
                            const fs = el.closest('fieldset');
                            if (fs) {
                                const lg = fs.querySelector('legend');
                                if (lg) return (lg.textContent || '').trim();
                            }
                            let p = el.parentElement;
                            for (let i = 0; i < 6 && p; i++) {
                                const lbl = p.querySelector(':scope > label, :scope > .application-question__label, :scope > .question-label, :scope > .field-label, :scope > div > label');
                                if (lbl) return (lbl.textContent || '').trim();
                                p = p.parentElement;
                            }
                            // Last resort: text of the closest question container
                            const c = el.closest('.application-question, [class*="question"], fieldset, .field');
                            return c ? (c.textContent || '').slice(0, 120).trim() : '';
                        }"""
                    )
                except Exception:
                    actual_label = ""
                actual_l = (actual_label or "").lower()
                # Demographic keyword groups — if AI's label contains a
                # keyword from one group but the actual label contains a
                # keyword from a DIFFERENT group, it's a mismatch.
                _GROUPS = [
                    ("gender identity", "gender"),
                    ("racial", "race", "ethnic"),
                    ("sexual orientation", "orientation"),
                    ("transgender",),
                    ("disabilit", "chronic condition"),
                    ("veteran", "armed forces", "military"),
                ]
                ai_groups = {i for i, kws in enumerate(_GROUPS) if any(k in ai_label for k in kws)}
                act_groups = {i for i, kws in enumerate(_GROUPS) if any(k in actual_l for k in kws)}
                if ai_groups and act_groups and not (ai_groups & act_groups):
                    logger.warning(
                        f"[AgentLoop] selector/label MISMATCH — refusing fill. "
                        f"AI said field_label={action.field_label!r} but selector "
                        f"{sel!r} actually resolves to {actual_label[:80]!r}. "
                        f"This is the demographic-swap bug; aborting to let the "
                        f"AI re-plan."
                    )
                    # Self-cleanup: if this same selector was already filled
                    # earlier in the session with a value that BELONGS to the
                    # AI's claimed group (i.e. we just leaked "Male" into the
                    # orientation field), clear the wrong chip. Otherwise the
                    # form keeps "Male" + "Heterosexual" both selected on a
                    # multi-select and the submit silently rejects.
                    try:
                        await loc.evaluate(
                            """(el) => {
                                // For react-select multi-value chips, find the
                                // wrong-value chip and click its X button to
                                // remove it.
                                const ctrl = el.closest('.select__control, .react-select__control');
                                if (!ctrl) return false;
                                const removes = ctrl.querySelectorAll(
                                    '.select__multi-value__remove, .react-select__multi-value__remove'
                                );
                                let n = 0;
                                removes.forEach(r => { try { r.click(); n++; } catch(e) {} });
                                return n;
                            }"""
                        )
                        logger.info(
                            f"[AgentLoop] cleared accidentally-filled chips on {sel!r}"
                        )
                    except Exception as exc:
                        logger.debug(f"[AgentLoop] chip-cleanup failed: {exc}")
                    return False

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
                        if options:
                            action.reason = (
                                f"INVALID OPTION: {value!r} (snapped to {target!r}) did not "
                                f"match this <select>. Actual options are: {options[:8]}. "
                                f"Re-emit fill_field with one of those exact strings."
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
                await asyncio.sleep(0.9)  # Forced delay for React hydration
                # 2. Read the actual options — SCOPED to THIS field's own menu
                #    (via aria-controls), so the phone widget's 200-country
                #    dropdown and any other open react-select don't pollute the
                #    list (that pollution was causing wrong/failed selections).
                try:
                    available_options = await ctx.evaluate(_SCOPED_OPTIONS_JS, sel)
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
                        """([s, target]) => {
                            const want = (target||'').trim().toLowerCase();
                            const el = document.querySelector(s);
                            let scope = document;
                            if (el) {
                                const lid = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
                                let menu = lid ? document.getElementById(lid) : null;
                                if (!menu) {
                                    const cont = el.closest('[class*="select__container"],[class*="-container"],[class*="select"]');
                                    if (cont) menu = cont.querySelector('[class*="select__menu"],[role="listbox"]');
                                }
                                if (menu) scope = menu;
                            }
                            const opts = scope.querySelectorAll('[class*="select__option"],[role="option"]');
                            for (const el2 of opts) {
                                if ((el2.textContent || '').trim().toLowerCase() === want) { el2.click(); return true; }
                            }
                            for (const el2 of opts) {
                                if ((el2.textContent || '').trim().toLowerCase().startsWith(want.slice(0,18))) { el2.click(); return true; }
                            }
                            if (opts.length === 1) { opts[0].click(); return true; }
                            return false;
                        }""",
                        [sel, target_text],
                    )
                    if clicked:
                        await asyncio.sleep(0.4)
                        logger.info(f"[AgentLoop] combobox clicked option {target_text!r} (exact-js)")
                        return True
                except Exception:
                    pass
                # If we KNOW the real options and the proposed value matches none
                # of them, do NOT type it as filter text: react-select would keep
                # that stray text in its input, which fools the pre-submit gate
                # (it reads the input value) into thinking the field is answered —
                # so the form submits with NO real selection. This is the "AI
                # answered a state for a Yes/No dropdown" bug. Instead: clear the
                # stray text, leave the menu OPEN so the next DOM snapshot surfaces
                # the actual options (e.g. ['Yes','No']) to the AI, and report
                # failure so the field reads empty and the AI re-picks a valid one.
                _norm_opts = [str(o).strip().lower() for o in (available_options or [])]
                _is_real_match = bool(_norm_opts) and (target_text or "").strip().lower() in _norm_opts
                if available_options and not _is_real_match:
                    try:
                        await loc.fill("")            # drop the stray filter text
                        await loc.click(timeout=1500)  # keep/reopen menu → options visible next turn
                    except Exception:
                        pass
                    logger.warning(
                        f"[AgentLoop] combobox: proposed {value!r} is NOT a valid option for "
                        f"{sel!r} (valid options: {available_options[:8]}). Cleared stray text and "
                        f"left the menu open; deferring to AI to choose a real option."
                    )
                    # Surface the REAL options in the action's reason so the
                    # history formatter shows them to the AI next turn. Without
                    # this the AI has zero new information after a failed
                    # attempt and just repeats the same wrong guess — this was
                    # the "AI answers a state name for a Yes/No dropdown" STUCK
                    # bug: the menu only renders options once opened (no hidden
                    # <select> sync, no aria-owns listbox), so the passive DOM
                    # snapshot can never discover them on its own; this runner
                    # discovery is the ONLY place that ever learns the truth.
                    action.reason = (
                        f"INVALID OPTION: {value!r} is not a real choice for this field. "
                        f"Actual options are: {available_options[:8]}. Re-emit fill_field "
                        f"with the SAME selector and one of those exact strings as value."
                    )
                    return False

                # Last resort — only reached when the initial open produced NO
                # options at all (search-style autocomplete widgets, e.g.
                # Ashby's location/school/company/source fields, render their
                # option list only after a query is typed — there's nothing to
                # enumerate before that). Type the value as a search query,
                # wait for results, then RE-SCAN and click the closest real
                # option — the same verified approach used above — instead of
                # blindly trusting Enter to select the right suggestion.
                try:
                    await loc.fill("")
                    await loc.type(target_text, delay=40)
                    await asyncio.sleep(0.9)
                    try:
                        searched_options = await ctx.evaluate(_SCOPED_OPTIONS_JS, sel)
                    except Exception:
                        searched_options = []
                    if searched_options:
                        search_target = _pick_closest_option(target_text, searched_options)
                        escaped2 = search_target.replace("'", "\\'")
                        try:
                            opt2 = ctx.locator(
                                f".select__menu .select__option:text-is('{escaped2}'), "
                                f".react-select__menu .react-select__option:text-is('{escaped2}'), "
                                f"[role='listbox'] [role='option']:text-is('{escaped2}')"
                            ).first
                            if await opt2.count() > 0:
                                await opt2.click(timeout=3000)
                                await asyncio.sleep(0.4)
                                logger.info(
                                    f"[AgentLoop] combobox search-then-click: query "
                                    f"{target_text!r} -> selected {search_target!r}"
                                )
                                return True
                        except Exception:
                            pass
                    # No options rendered even after typing (or the click above
                    # failed) — fall back to Enter, which commits the typed
                    # text directly for widgets that accept free text.
                    await page.keyboard.press("Enter")
                    logger.info(f"[AgentLoop] combobox fallback type+Enter for {target_text!r}")
                    return True
                except Exception as exc:
                    logger.warning(f"[AgentLoop] combobox fallback failed: {exc}")
                    return False

            # ── react-tel-input / intl-tel phone widgets ─────────────────────
            # These are controlled React widgets that PRE-SEED the country dial
            # code (e.g. "+1") and REFORMAT as you type. Two gotchas:
            #   1. A normal fill / native-setter is rejected (widget reverts).
            #   2. Typing the FULL "+1 3410084746" collides with the pre-existing
            #      "+1" and SHIFTS the digits → "+1 (134) 100-8474" (invalid).
            # Correct recipe: focus, clear thoroughly (select-all + many
            # backspaces — Delete leaves the "+1"), then type ONLY the national
            # digits. The widget re-adds the dial code and formats correctly.
            try:
                is_reacttel = await loc.evaluate(
                    "(el) => !!(el.closest && (el.closest('.react-tel-input') "
                    "|| el.closest('.iti')) || el.id === 'phone-input')"
                )
            except Exception:
                is_reacttel = False
            if is_reacttel:
                try:
                    digits = re.sub(r"\D", "", value)
                    # Reduce to the NATIONAL number (drop a leading country code).
                    # US-first product: "+1XXXXXXXXXX"/"1XXXXXXXXXX" → national 10.
                    national = digits
                    if len(digits) == 11 and digits.startswith("1"):
                        national = digits[1:]
                    await loc.click(timeout=3000)
                    try:
                        await loc.press("Control+a", timeout=1500)
                        for _ in range(20):
                            await loc.press("Backspace", timeout=800)
                    except Exception:
                        pass
                    target = national or digits
                    if hasattr(loc, "press_sequentially"):
                        await loc.press_sequentially(target, delay=70, timeout=15000)
                    else:
                        await loc.type(target, delay=70, timeout=15000)
                    await asyncio.sleep(0.3)
                    cur = (await loc.input_value(timeout=2000)) or ""
                    if re.sub(r"\D", "", cur).endswith(national[-7:] if len(national) >= 7 else national):
                        logger.info(f"[AgentLoop] react-tel phone filled → {cur!r}")
                        return True
                    logger.warning(
                        f"[AgentLoop] react-tel phone value after fill = {cur!r} "
                        f"(expected national {national!r}); the number may be "
                        "invalid (e.g. unassigned area code)."
                    )
                    # Return True anyway — re-typing would only thrash; the AI
                    # should not loop on it (a bad value is a data problem).
                    return True
                except Exception as exc:
                    logger.warning(f"[AgentLoop] react-tel fill failed: {exc}")
                    # fall through to the generic path

            # Default: text / email / textarea / url / tel / number
            try:
                await loc.scroll_into_view_if_needed(timeout=3000)
                await loc.wait_for(state="visible", timeout=3000)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.05, 0.15))

            # ── HUMAN-LIKE TYPING (anti-bot) ─────────────────────────────
            # Real keystrokes with per-character delay, instead of injecting
            # the whole value at once. This is BOTH more human-like (varied
            # cadence, real keydown/keyup events the site can observe) AND
            # more React-friendly (controlled inputs commit on real input
            # events). Falls back to the native setter only if typing doesn't
            # land — covers stubborn masked/controlled inputs.
            #   HUMAN_TYPING=false              → disable, use instant setter
            #   HUMAN_TYPE_MIN_MS / _MAX_MS     → per-char delay window
            typed_ok = False
            if os.getenv("HUMAN_TYPING", "true").lower() != "false":
                try:
                    lo = int(os.getenv("HUMAN_TYPE_MIN_MS", "35"))
                    hi = int(os.getenv("HUMAN_TYPE_MAX_MS", "120"))
                    per_char = random.uniform(lo, hi)
                    await loc.click(timeout=3000)
                    # Clear any pre-existing value the human way (select-all + delete).
                    try:
                        await loc.press("Control+a", timeout=1500)
                        await loc.press("Delete", timeout=1500)
                    except Exception:
                        pass
                    # pressSequentially fires real per-key events (Playwright's
                    # successor to type()); delay is per character. Fall back to
                    # the legacy .type() on older Playwright builds.
                    if hasattr(loc, "press_sequentially"):
                        await loc.press_sequentially(value, delay=per_char, timeout=15000)
                    else:
                        await loc.type(value, delay=per_char, timeout=15000)
                    # Commit on blur. Some React-controlled <textarea>/<input>
                    # fields (e.g. Greenhouse's long free-text screening
                    # questions) only persist their value to component state on
                    # the blur event — without it the value reverts to empty on
                    # the next re-render, so FORM STATUS keeps showing the field
                    # empty and the AI re-fills it forever (the free-text loop).
                    try:
                        await loc.evaluate(
                            "el => { el.dispatchEvent(new Event('change', {bubbles:true})); el.blur && el.blur(); }"
                        )
                    except Exception:
                        pass
                    # Verify it actually committed into the field's value.
                    # Masked / formatting widgets (e.g. react-tel-input phone
                    # fields) REFORMAT the value as you type — "+13410084746"
                    # becomes "+1 (341) 008-4746" — so an exact-string compare
                    # wrongly reports failure and triggers the native-setter
                    # fallback, which CORRUPTS the controlled widget (it reverts
                    # to its default). Treat the field as committed when the raw
                    # value matches OR the digit sequences match (covers phone).
                    cur = (await loc.input_value(timeout=2000)) or ""
                    _cur_d = re.sub(r"\D", "", cur)
                    _val_d = re.sub(r"\D", "", value)
                    typed_ok = (
                        cur.strip() == value.strip()
                        or (bool(_val_d) and _val_d in _cur_d)
                        or (bool(value.strip()) and value.strip() in cur)
                    )
                except Exception as exc:
                    logger.debug(f"[AgentLoop] human-typing failed for {sel!r}: {exc}")

            if not typed_ok:
                # Fallback: native setter for React-controlled / masked inputs.
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
            # Surface WHY a fill failed when the runner knows the real reason
            # (e.g. "not a valid option — actual options are [...]"). Without
            # this the AI sees only "❌FAILED" and repeats the same wrong
            # value forever since nothing in its context ever changes.
            extra = f"  ⚠ {a.reason}" if (a.ok is False and a.reason) else ""
            lines.append(f"  [{a.step}] fill_field  '{a.field_label or a.selector}' = '{(a.value or '')[:60]}'{tag}{extra}")
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
            lines.append(f"  [{a.step}] verify_page OK")
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
        # Set True the instant the loop fires a REAL submit click. The executor
        # reads this after run() to decide whether the scripted fallback is safe:
        # once the form has been sent to the ATS, re-running the scripted
        # detect→fill→submit pipeline would risk a DOUBLE submission, so the
        # executor treats any post-submit outcome as terminal.
        self._submit_fired: bool = False
        # Auto-populate hints from registry if not explicitly provided
        platform = str(job_context.get("platform") or "generic").lower()
        self._hints = platform_hints if platform_hints is not None else get_platform_hints(platform)
        self._platform = platform
        self.verify_gate_limit = self._hints.get("verify_gate_limit", 20)
        self._llm = get_llm()
        # Build the identity-anchored system prompt. Rebuilt if the effective
        # provider changes mid-session (e.g. Anthropic exhausts and Groq takes
        # over) so the resume-block size matches the live provider's caching.
        try:
            self._prompt_provider = self._llm.effective_provider()
        except Exception:
            self._prompt_provider = "anthropic"
        self._system_prompt = self._build_system_prompt()

    async def _human_delay(self, kind: str = "default") -> None:
        """Sleep a small randomized interval to mimic human pacing and reduce
        bot-detection risk. Tunable via HUMAN_DELAY_MIN_MS / HUMAN_DELAY_MAX_MS;
        set HUMAN_DELAY_MIN_MS=0 to disable. Kept minimal so runs stay fast.
        """
        try:
            lo = int(os.getenv("HUMAN_DELAY_MIN_MS", "250"))
            hi = int(os.getenv("HUMAN_DELAY_MAX_MS", "700"))
        except Exception:
            lo, hi = 250, 700
        if hi <= 0 or lo < 0 or hi < lo:
            return
        await asyncio.sleep(random.uniform(lo / 1000.0, hi / 1000.0))

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
            # on application forms — default answer is "N/A". Env override
            # ALLOW_REAL_LINKEDIN=1 flips this to the profile's real
            # linkedin.com URL for ATSes that reject N/A on required fields.
            f"  LinkedIn URL: {_linkedin_policy_value(p)}  (policy: {'real URL (opted in)' if _linkedin_policy_value(p) != 'N/A' else 'do NOT submit real LinkedIn URL on forms'})",
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
        #
        # Length budget: Anthropic prompt-cache makes 5500 chars effectively
        # free after the first call (cache hits ~ free). Groq specifically has
        # a tight 30k TPM cap (Llama-4-Scout) that a full resume block would
        # eat into every turn, so it gets a tighter budget. Gemini/OpenRouter
        # have no caching either, but no comparably tight TPM ceiling — giving
        # them the same 1800-char floor as Groq was starving the resume block
        # of exactly the content (Education/Experience sections, which tend
        # to sit toward the END of the extracted text) needed to answer
        # degree/discipline/employer questions correctly, since Gemini is the
        # actual primary provider in this pipeline (LLM_KEY_PRIORITY puts it
        # first) — the "non-Anthropic" bucket was in practice "the common
        # case", not the rare fallback the original budget assumed.
        resume_text = (p.get("_resume_text") or "").strip()
        resume_block = ""
        if resume_text:
            try:
                _eff = self._llm.effective_provider()
            except Exception:
                _eff = "anthropic"
            _default_budget = {
                "anthropic": "5500",   # prompt-cached — effectively free after turn 1
                "gemini": "4500",      # generous free tier, no comparable TPM wall
                "openrouter": "3000",  # paid per-token but no hard TPM ceiling
                "groq": "1800",        # 30k TPM cap — keep tight
            }.get(_eff, "1800")
            _budget = int(os.getenv("RESUME_PROMPT_CHARS", _default_budget))
            sep = "-" * 60
            resume_block = (
                "\n\nRESUME CONTENT (source of truth — when a form field asks "
                "for name, email, phone, education, employer, title, or "
                "skills, use values from THIS resume verbatim. If the "
                "identity card above disagrees with the resume, the RESUME "
                "WINS):\n"
                + sep + "\n"
                + resume_text[:_budget]
                + "\n" + sep + "\n"
            )
        # Platform-specific playbook — the per-ATS cheat sheet from
        # adapters/hints.py (iframe quirks, multi-step flow, reliable
        # selectors, success signals, known failure modes). This is what makes
        # the AI aware of, e.g., Talent.com's email-OTP-before-form flow or
        # Dice's 3-step wizard. Injected once into the (cached) system prompt.
        hints_block = ""
        try:
            rendered = format_hints_for_prompt(self._hints)
            if rendered and rendered.strip() and "no specific hints" not in rendered:
                hints_block = (
                    "\n\n══════════════════════════════════════════════════════════"
                    "════════════════\n"
                    f"PLATFORM PLAYBOOK — {self._platform} (empirical knowledge; "
                    "the live page is still authoritative)\n"
                    "══════════════════════════════════════════════════════════"
                    "════════════════\n"
                    + rendered + "\n"
                )
        except Exception as exc:
            logger.debug(f"[AgentLoop] hint render failed (non-fatal): {exc}")

        # Append the action-schema/rules block (unchanged from the original
        # system prompt, just relocated so the identity card comes first).
        # Then the screening-answers block, if M3 provided any.
        schema_block = _ACTION_SCHEMA_BLOCK
        if not self.cover_letter_path:
            schema_block += "\n\nCRITICAL OVERRIDE: You DO NOT have a cover letter. If a cover letter field exists, DO NOT upload anything to it. Skip it entirely.\n"
        return base + resume_block + hints_block + screening_block + schema_block

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
                    // Detect react-select / custom comboboxes and native <select>.
                    // A raw value-set on these does NOT commit the option (react
                    // keeps its own state), so memory pre-fill would falsely mark
                    // the field "filled" and trip a premature, server-rejected
                    // submit. Flag them so we defer to the AI's option-click path.
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    const ariaPop = (el.getAttribute('aria-haspopup') || '').toLowerCase();
                    const combo = (
                        el.tagName === 'SELECT'
                        || role === 'combobox'
                        || !!el.getAttribute('aria-autocomplete')
                        || ariaPop === 'listbox' || ariaPop === 'menu'
                        || !!el.closest('[class*="select__"], [class*="combobox" i], [role="combobox"]')
                    );
                    out.push({ sel, type, label: label.slice(0, 100), value, combo });
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
            # ── LinkedIn-URL hard override (operator policy) ────────────────
            # Memory pre-fill bypasses _execute_action, so the LinkedIn → "N/A"
            # rewrite that lives there does NOT apply here. Re-enforce it at
            # the memory layer too, otherwise stale "https://linkedin.com/..."
            # values cached on a previous session leak straight back into the
            # form (the exact bug we saw on Sabih's first end-to-end run).
            _lbl_l = label.lower()
            _sel_l = (sel or "").lower()
            if (
                ("linkedin" in _lbl_l or "linkedin" in _sel_l)
                and "website" not in _lbl_l
                and "portfolio" not in _lbl_l
                and "github" not in _lbl_l
            ):
                _li_target = _linkedin_policy_value(self.profile)
                if str(answer).strip() != _li_target and str(answer).strip().upper() != _li_target.upper():
                    logger.info(
                        f"[AgentLoop] memory pre-fill LinkedIn override: {label!r} "
                        f"was {str(answer)[:50]!r}, forcing {_li_target!r}"
                    )
                    answer = _li_target

            # ── Combobox / native-select: COMMIT via the proven option-click
            # path (the same _commit_policy_field used by deterministic prefill),
            # NOT a raw value-set. A native value-set never selects a react-select
            # option, so the field would look "filled" yet fail server validation
            # — that was the premature-submit STUCK on Greenhouse. We commit it
            # properly here, ONE-SHOT: the synthesized fill_field action below
            # records the label so this never retries (avoiding the per-turn
            # combobox race documented in _try_demographic_autofill).
            if f.get("combo"):
                try:
                    ok = await self._commit_policy_field(
                        page, ctx, sel, "combobox", str(answer), [str(answer)]
                    )
                except Exception as exc:
                    logger.debug(f"[AgentLoop] memory pre-fill combobox commit error for {label!r}: {exc}")
                    ok = False
                # Record the attempt either way so memory pre-fill won't fight
                # the field next turn; if it failed, the AI's own fill_field path
                # can still drive it (the AI is not gated by these records).
                actions.append(AgentAction(
                    kind="fill_field",
                    selector=sel,
                    value=str(answer),
                    field_label=label,
                    step=-1,
                    ok=bool(ok),
                    raw={"source": "memory_prefill_combobox"},
                ))
                if ok:
                    logger.info(f"[AgentLoop] memory pre-fill combobox committed: '{label}' = '{str(answer)[:60]}'")
                    filled += 1
                else:
                    logger.debug(f"[AgentLoop] memory pre-fill combobox commit failed for '{label}' — deferring to AI")
                continue

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
                # Same reasoning as the deterministic pre-fill loop — commit
                # these back-to-back with zero delay and the whole run reads
                # as clearly automated to timing-based anti-bot heuristics.
                await self._human_delay()
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

    async def _try_consent_autofill(
        self,
        page: Page,
        frame: Optional[Any],
        actions: List[AgentAction],
    ) -> int:
        """Affirm consent / acknowledgment fields deterministically before the
        LLM runs. These have ONE correct answer (agree / check / Yes) and the
        AI was fumbling them — pasting the question text as the value, or
        filling "Yes" into a checkbox that needs checking, leaving the
        pre-submit gate blocked so submit never fired.

        Handles: native checkbox (check it), native <select> / react-select
        combobox (pick the affirmative option). One-shot per selector to avoid
        racing with the AI on subsequent turns.

        Matches labels like:
          "By submitting my application, I acknowledge…"
          "I have read and understand…privacy notice"
          "Please double-check all the information…ensuring accuracy"
          "I confirm…", "I agree…"
        """
        ctx = frame or page
        attempted = {
            (a.selector or "").strip()
            for a in actions
            if a.raw and a.raw.get("source") == "consent_autofill"
        }
        try:
            targets = await ctx.evaluate("""() => {
                const CONSENT_RE = /\\b(i acknowledge|acknowledge that|i have read|i agree|i confirm|double-check all the information|ensuring accuracy|privacy notice|terms of service|consent to)\\b/i;
                const AFFIRM_RE = /^(yes|i acknowledge|i agree|i confirm|agree|acknowledged?|confirmed?|i have read|i understand)/i;
                function labelFor(el) {
                    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
                    if (el.id) {
                        const l = document.querySelector('label[for="' + el.id + '"]');
                        if (l) return (l.textContent || '').trim();
                        if (el.id.endsWith('--input')) {
                            const b = el.id.slice(0,-7);
                            const l2 = document.querySelector('label[for="' + b + '"]');
                            if (l2) return (l2.textContent || '').trim();
                        }
                    }
                    let p = el.parentElement;
                    for (let i=0;i<6&&p;i++){
                        const l = p.querySelector(':scope > label, :scope > .application-question__label, :scope > div > label');
                        if (l) return (l.textContent||'').trim();
                        p = p.parentElement;
                    }
                    const c = el.closest('.application-question, [class*="question"], fieldset, .field');
                    return c ? (c.textContent||'').slice(0,160).trim() : '';
                }
                const out = [];
                const seen = new Set();
                // Native checkboxes
                document.querySelectorAll('input[type="checkbox"]').forEach(el => {
                    const st = window.getComputedStyle(el);
                    if (st.display==='none'||st.visibility==='hidden') return;
                    const label = labelFor(el);
                    if (!CONSENT_RE.test(label)) return;
                    if (el.checked) return;
                    const sel = el.id ? '#'+el.id : (el.name ? 'input[name="'+el.name+'"]' : '');
                    if (!sel || seen.has(sel)) return;
                    seen.add(sel);
                    out.push({kind:'checkbox', selector:sel, label:label.slice(0,120), value:'Yes'});
                });
                // Native selects with an affirmative option
                document.querySelectorAll('select').forEach(el => {
                    const st = window.getComputedStyle(el);
                    if (st.display==='none'||st.visibility==='hidden') return;
                    const label = labelFor(el);
                    if (!CONSENT_RE.test(label)) return;
                    if (el.value && el.value.trim()) {
                        const cur = (el.options[el.selectedIndex]?.text||'').trim();
                        if (cur && !/^(select|choose|please|—|--)/i.test(cur)) return;
                    }
                    let pickText=null, pickVal=null;
                    for (const o of el.options){
                        if (!o.value) continue;
                        if (AFFIRM_RE.test((o.text||'').trim())){ pickText=o.text.trim(); pickVal=o.value; break; }
                    }
                    if (pickText===null) return;
                    const sel = el.id ? '#'+el.id : 'select[name="'+el.name+'"]';
                    if (seen.has(sel)) return; seen.add(sel);
                    out.push({kind:'select', selector:sel, label:label.slice(0,120), value:pickText, optionValue:pickVal});
                });
                // react-select comboboxes
                document.querySelectorAll('[role="combobox"]').forEach(el => {
                    const st = window.getComputedStyle(el);
                    if (st.display==='none'||st.visibility==='hidden') return;
                    const label = labelFor(el);
                    if (!CONSENT_RE.test(label)) return;
                    const ctrl = el.closest('[class*="select__control"],[class*="-control"]');
                    if (ctrl){
                        const sv = ctrl.querySelector('[class*="singleValue"],[class*="-singleValue"]');
                        if (sv && sv.textContent.trim()) return;
                    }
                    const sel = el.id ? '#'+el.id : null;
                    if (!sel || seen.has(sel)) return; seen.add(sel);
                    out.push({kind:'combobox', selector:sel, label:label.slice(0,120), value:'Yes'});
                });
                return out;
            }""")
        except Exception as exc:
            logger.debug(f"[AgentLoop] consent auto-fill scan failed: {exc}")
            return 0

        filled = 0
        for t in targets or []:
            sel = t.get("selector")
            kind = t.get("kind")
            label = t.get("label", "")
            value = t.get("value", "Yes")
            if (sel or "").strip() in attempted:
                continue
            try:
                loc = ctx.locator(sel).first
                if await loc.count() == 0:
                    continue
                if kind == "checkbox":
                    try:
                        await loc.check(timeout=3000, force=True)
                    except Exception:
                        await loc.click(timeout=3000, force=True)
                elif kind == "select":
                    try:
                        await loc.select_option(value=t.get("optionValue"), timeout=3000)
                    except Exception:
                        await loc.select_option(label=value, timeout=3000)
                elif kind == "combobox":
                    await loc.scroll_into_view_if_needed(timeout=2000)
                    await loc.click(timeout=3000)
                    await page.wait_for_timeout(200)
                    await loc.type(value, delay=30, timeout=3000)
                    await page.wait_for_timeout(400)
                    opt = ctx.locator("[role='option']", has_text=value).first
                    if await opt.count() > 0:
                        await opt.click(timeout=3000)
                    else:
                        await loc.press("Enter", timeout=2000)
                else:
                    continue
                actions.append(AgentAction(
                    kind="fill_field", selector=sel, value=value,
                    field_label=label, step=-1, ok=True,
                    raw={"source": "consent_autofill"},
                ))
                logger.info(f"[AgentLoop] consent auto-fill: '{label[:60]}' = '{value}'")
                filled += 1
            except Exception as exc:
                logger.debug(f"[AgentLoop] consent auto-fill apply failed for {label!r}: {exc}")
                continue
        return filled

    @staticmethod
    def _infer_gender(first_name: str) -> str:
        """Infer Male/Female from a first name. Defaults to Male on ambiguity
        (operator policy uses Male for masculine/ambiguous names like Sabih,
        Harmain, Ahmed). Female only for clearly feminine names."""
        fn = (first_name or "").strip().lower()
        female = {
            "sarah", "aisha", "ayesha", "jane", "mary", "emily", "fatima",
            "zainab", "maria", "anna", "laura", "sara", "hira", "amna",
            "noor", "mahnoor", "iqra", "kinza", "areeba", "emma", "olivia",
            "sophia", "mia", "isabella", "rabia", "sana", "maham",
        }
        return "Female" if fn in female else "Male"

    # Minimal city → state map for US locations. Used when the candidate's
    # location string lacks a state code. We keep it small and pragmatic —
    # only the cities that show up in real candidate profiles. Add more as
    # they surface in production.
    _US_CITY_TO_STATE: dict[str, tuple[str, str]] = {
        "san francisco": ("California", "CA"),
        "los angeles":   ("California", "CA"),
        "san diego":     ("California", "CA"),
        "san jose":      ("California", "CA"),
        "oakland":       ("California", "CA"),
        "sacramento":    ("California", "CA"),
        "new york":      ("New York", "NY"),
        "brooklyn":      ("New York", "NY"),
        "manhattan":     ("New York", "NY"),
        "nyc":           ("New York", "NY"),
        "seattle":       ("Washington", "WA"),
        "austin":        ("Texas", "TX"),
        "houston":       ("Texas", "TX"),
        "dallas":        ("Texas", "TX"),
        "chicago":       ("Illinois", "IL"),
        "boston":        ("Massachusetts", "MA"),
        "miami":         ("Florida", "FL"),
        "atlanta":       ("Georgia", "GA"),
        "denver":        ("Colorado", "CO"),
        "portland":      ("Oregon", "OR"),
        "philadelphia":  ("Pennsylvania", "PA"),
        "washington":    ("District of Columbia", "DC"),
        "phoenix":       ("Arizona", "AZ"),
    }
    _US_STATE_ABBR: dict[str, str] = {
        "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
        "CA": "California", "CO": "Colorado", "CT": "Connecticut",
        "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
        "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
        "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
        "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
        "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
        "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
        "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
        "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
        "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
        "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
        "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
        "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
        "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    }

    @classmethod
    def _resolve_us_state(cls, location: str) -> Optional[tuple[str, str]]:
        """Map a free-form candidate location to (state_name, state_abbr), or
        None if no real US state can be identified.

        Priority: explicit 2-letter code → full state name in string →
        city-to-state lookup. Returns None when NONE of these matched — the
        caller decides what to do (typically: skip the location-dependent
        policy entry and let the AI handle the field).
        """
        if not location:
            return None
        s = location.strip()
        low = s.lower()
        # Explicit 2-letter code after a comma, e.g. "San Francisco, CA, USA"
        import re as _re
        m = _re.search(r",\s*([A-Z]{2})\b", s)
        if m and m.group(1) in cls._US_STATE_ABBR:
            abbr = m.group(1)
            return (cls._US_STATE_ABBR[abbr], abbr)
        # Full state name appearing anywhere
        for abbr, name in cls._US_STATE_ABBR.items():
            if name.lower() in low:
                return (name, abbr)
        # City fallback — only when the city occurs as a whole word, not as
        # a substring (so "Norman" matches but "manchester" does NOT match
        # "man").
        for city, (name, abbr) in cls._US_CITY_TO_STATE.items():
            if _re.search(rf"\b{_re.escape(city)}\b", low):
                return (name, abbr)
        return None

    def _policy_fields(self) -> list:
        """The fixed-answer policy map. Each entry: (label_regex, value,
        [option aliases for fuzzy dropdown matching]). Order matters —
        most-specific patterns first (UK work-auth before generic US).

        These answers are IDENTICAL for every candidate (operator policy):
        country=US, UK-auth=No, US-auth=Yes, sponsorship=No, LinkedIn=N/A,
        gender(name-inferred), race=South Asian, orientation=Heterosexual,
        transgender/disability/veteran=No, consent=affirm. Filling them
        deterministically removes the AI's selector-swap / regex-mismatch /
        commit failures for the bulk of the form.
        """
        p = self.profile or {}
        gender = self._infer_gender(p.get("first_name") or (p.get("name", "").split() or [""])[0])
        gender_alias = "man" if gender == "Male" else "woman"
        referral = (p.get("referral_source") or "LinkedIn").strip() or "LinkedIn"

        # Derive city + US-state from the candidate's location string.
        # Profile format is typically "City, State[, Country]" or "City, USA".
        # We do NOT hardcode per-candidate defaults here — the system has to
        # be candidate-agnostic. If location is empty/generic (just a country
        # code like "US"), we SKIP the location-dependent policy entries
        # entirely and let the AgentLoop's AI handle those fields using the
        # enriched profile context (resume-extracted location lives there).
        loc_raw = (p.get("location") or "").strip()
        _country_like = {
            "", "us", "usa", "u.s.", "u.s.a.", "united states",
            "united states of america", "america",
        }
        if loc_raw.lower() in _country_like or len(loc_raw) <= 2:
            city = ""              # signals: skip Location (City) policy
            state_name = ""        # signals: skip dv01-state policy
            state_abbr = ""
        else:
            city = loc_raw.split(",")[0].strip()
            resolved = self._resolve_us_state(loc_raw)
            if resolved is None:
                # Real city but no identifiable US state — let AI handle the
                # state question. Location (City) policy can still fire.
                state_name = ""
                state_abbr = ""
            else:
                state_name, state_abbr = resolved
        salary = (p.get("salary_expectation") or "").strip()
        phone = (p.get("phone") or "").strip()

        entries = [
            # dv01-style consent multi-select: "...your application acknowledges
            # this and you confirm that you will work within the united states...
            # what state are you working from?". The options are US STATE NAMES,
            # so the answer is the candidate's state — NOT "Yes". This MUST go
            # before the generic `acknowledge` policy below, because both regexes
            # match this label (it contains both "acknowledges" and "you confirm"),
            # and the first-match-wins policy loop would otherwise hand it to the
            # ack policy and try to commit "Yes" against a state-only dropdown.
            # We only add this if the resolver gave us a real state — otherwise
            # the AI handles the field using the enriched profile context.
            *(
                [(r"you\s+confirm.*work\s+within\s+the\s+united\s+states|what\s+state\s+are\s+you\s+working\s+from",
                  state_name,
                  [state_name.lower(), state_abbr.lower()])]
                if state_name else []
            ),
            # "Where / how did you hear about us / this role?" — fixed referral
            # source. It's a react-select with options like LinkedIn / Job
            # board / Referral; default to LinkedIn. Listed FIRST so it's
            # handled by the robust scoped commit, not the AI (which kept
            # failing to commit it → red error → submit rejected).
            (r"(where|how)\s+did\s+you\s+(first\s+)?(hear|find|learn)\s+(about|of)",
             referral, [referral.lower(), "linkedin", "job board", "online", "other job boards"]),
            # Work authorization — UK / non-US first, then US.
            (r"authoriz\w*\s+to\s+work\s+in\s+(the\s+)?(uk|united kingdom|canada|eu|europe|australia|india|germany|ireland)",
             "No", ["no", "i am not authorized", "not authorized", "no, i am not authorized"]),
            (r"authoriz\w*\s+to\s+work(\s+in\s+(the\s+)?(us|u\.s\.|usa|united states))?",
             "Yes", ["yes", "i am authorized", "authorized", "yes, i am authorized"]),
            (r"(require|need).*(sponsor|visa)|sponsor\w*.*(now|future|work)",
             "No", ["no"]),
            (r"\blinkedin\b",
             _linkedin_policy_value(p),
             [_linkedin_policy_value(p).lower(), "n/a"]),
            (r"(currently based in|country of residence|which country|where.*based|^country)",
             "United States", ["united states", "united states of america", "usa", "u.s.a."]),
            (r"gender identity|what is your gender|\bgender\b",
             gender, [gender.lower(), gender_alias]),
            (r"racial|ethnic|\brace\b",
             "South Asian", ["south asian", "asian", "asian (not hispanic or latino)"]),
            (r"sexual orientation|orientation",
             "Heterosexual", ["heterosexual", "straight", "heterosexual / straight"]),
            (r"transgender",
             "No", ["no"]),
            (r"disabilit|chronic condition",
             "No", ["no", "i don't have a disability", "no, i don't have a disability"]),
            (r"veteran|armed forces|military service",
             "No", ["no", "i am not a veteran", "not a veteran", "not a protected veteran",
                    "i am not a protected veteran"]),
            (r"acknowledge|i have read|double[- ]check|ensuring accuracy|privacy notice|terms of service|i confirm|i agree|consent to",
             "Yes", ["yes", "i acknowledge", "i agree", "i confirm", "i have read", "i understand"]),
        ]

        # ── Remix-Greenhouse (job-boards.greenhouse.io) additions ───────
        # "Location (City)" autocomplete: candidate's city. Autocomplete branch
        # in _commit_policy_field handles the API-driven option load.
        if city:
            entries.append(
                (r"^location\s*\(city\)|location\s*\(city|^city\b",
                 city, [city.lower(), loc_raw.lower()]),
            )
        # Salary expectation: pass the candidate's stated number through. If
        # empty in profile we skip — better to let the AI answer than to type
        # a wrong default. We don't add aliases (text inputs don't fuzzy-match).
        if salary:
            entries.append(
                (r"salary\s+expect|expected\s+(salary|compensation)|compensation\s+expect",
                 salary, [salary]),
            )
        # Phone — stable identity data (like name/email). Fill it deterministically
        # whenever a phone field exists so an OPTIONAL phone isn't left blank for a
        # brand-new candidate (one with no field_memory yet). The carve-out in
        # _deterministic_prefill keeps the NUMBER out of country-code/extension
        # fields, and _commit_policy_field defers react-tel widgets to the AI.
        if phone:
            entries.append(
                (r"\bphone\b|phone\s*number|mobile\s*number|\bmobile\b|cell\s*phone|contact\s+(number|phone)",
                 phone, [phone]),
            )
        # dv01-style consent multi-select policy is inserted at the TOP of
        # `entries` above (priority over the generic acknowledge rule).
        return entries

    async def _deterministic_file_upload(
        self,
        page: Page,
        frame: Optional[Any],
        actions: List[AgentAction],
    ) -> int:
        """Upload resume and (optionally) cover letter to the right file
        inputs WITHOUT asking the AI. Idempotent — won't re-upload a slot
        that's already attached.

        Slot routing:
          • Resume slot → id/name/aria-label/parent-group-label contains any of:
            'resume', 'cv', 'curriculum'. Fallback: first file input on page
            when only ONE file input exists and the resume_path is set.
          • Cover-letter slot → contains 'cover', 'letter', 'coverletter'.
            ONLY uploaded when self.cover_letter_path is set; we NEVER fall
            back to the resume here (that was the source of the
            "two copies of resume_v5.pdf" bug).
        """
        ctx = frame or page
        attempted: set[str] = {
            (a.selector or "").strip()
            for a in actions
            if a.raw and a.raw.get("source") == "deterministic_file_upload"
        }

        try:
            inputs = await ctx.evaluate(r"""() => {
                const out = [];
                document.querySelectorAll('input[type="file"]').forEach(el => {
                    // Build a meaningful selector — prefer id, then name.
                    let sel = '';
                    if (el.id) sel = '#' + CSS.escape(el.id);
                    else if (el.name) sel = `input[name="${el.name}"]`;
                    else return;
                    // Group label: Greenhouse wraps each file input in a
                    // role="group" whose aria-labelledby points at "Resume/CV*"
                    // or "Cover Letter".
                    let groupLabel = '';
                    const grp = el.closest('[role="group"]');
                    if (grp) {
                        const lid = grp.getAttribute('aria-labelledby');
                        if (lid) {
                            const lbl = document.getElementById(lid);
                            if (lbl) groupLabel = (lbl.textContent || '').trim();
                        }
                        if (!groupLabel) {
                            const sib = grp.querySelector('.upload-label, [class*="upload-label"]');
                            if (sib) groupLabel = (sib.textContent || '').trim();
                        }
                    }
                    // Already-uploaded indicators (Greenhouse replaces the
                    // input with a filename display element after upload).
                    let alreadyAttached = false;
                    if (el.files && el.files.length > 0) alreadyAttached = true;
                    if (!alreadyAttached && grp) {
                        const filename = grp.querySelector(
                            '.file-attachment-name, .attachment-name, '
                          + '[class*="filename"], [class*="uploaded-file"]'
                        );
                        if (filename && (filename.textContent || '').trim()) {
                            alreadyAttached = true;
                        }
                    }
                    out.push({
                        sel,
                        id: el.id || '',
                        name: el.name || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        groupLabel,
                        alreadyAttached,
                    });
                });
                return out;
            }""")
        except Exception as exc:
            logger.debug(f"[AgentLoop] file-input scan failed: {exc}")
            return 0

        def _classify(info: dict) -> Optional[str]:
            """Return 'resume' / 'cover' / None for a file-input descriptor."""
            haystack = " ".join((
                info.get("id", ""), info.get("name", ""),
                info.get("ariaLabel", ""), info.get("groupLabel", ""),
            )).lower()
            if any(k in haystack for k in ("cover", "letter")):
                return "cover"
            if any(k in haystack for k in ("resume", "curriculum", "cv")):
                return "resume"
            return None

        uploaded = 0
        resume_inputs = []
        cover_inputs = []
        unknown_inputs = []
        for info in inputs or []:
            kind = _classify(info)
            if kind == "resume":
                resume_inputs.append(info)
            elif kind == "cover":
                cover_inputs.append(info)
            else:
                unknown_inputs.append(info)

        # Positional fallback: exactly two file inputs, no labels matched.
        # By convention resume comes first on every ATS we support.
        if not resume_inputs and not cover_inputs and len(unknown_inputs) >= 1:
            resume_inputs.append(unknown_inputs[0])
            if len(unknown_inputs) >= 2:
                cover_inputs.append(unknown_inputs[1])

        async def _do_upload(info: dict, path: str, kind: str) -> bool:
            sel = info["sel"]
            if sel in attempted:
                return False
            if info.get("alreadyAttached"):
                logger.info(f"[AgentLoop] file-upload skip {kind} ({sel!r}) — already attached")
                attempted.add(sel)
                return False
            try:
                loc = ctx.locator(sel).first
                if await loc.count() == 0:
                    return False
                await loc.set_input_files(path, timeout=8000)
                logger.info(f"[AgentLoop] deterministic upload {kind} → {sel!r} = {path!r}")
                if kind == "resume":
                    # Some ATSes (Lever confirmed: js/parseResume.js) fire an
                    # ASYNC background request to parse the resume server-side
                    # right after the file input's change event, auto-filling
                    # name/email/phone/location from it. If we let the AI
                    # start clicking around before that settles, a second
                    # file-input interaction (e.g. our own retry logic, or
                    # even an unrelated DOM re-scan) can abort the in-flight
                    # request (Lever's own code does `req.abort()` on a new
                    # change event) and leave the upload widget in a
                    # confused state that shows a stale/wrong error. Give it
                    # a real window to finish before moving on.
                    try:
                        await page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        await asyncio.sleep(3.0)
                actions.append(AgentAction(
                    kind="upload_file", selector=sel, value=kind,
                    field_label=info.get("groupLabel") or info.get("id") or "",
                    step=-1, ok=True,
                    raw={"source": "deterministic_file_upload"},
                ))
                attempted.add(sel)
                return True
            except Exception as exc:
                logger.debug(f"[AgentLoop] file upload failed for {sel!r}: {exc}")
                actions.append(AgentAction(
                    kind="upload_file", selector=sel, value=kind,
                    step=-1, ok=False,
                    raw={"source": "deterministic_file_upload"},
                ))
                attempted.add(sel)
                return False

        if self.resume_path:
            for info in resume_inputs:
                if await _do_upload(info, self.resume_path, "resume"):
                    uploaded += 1
        if self.cover_letter_path:
            for info in cover_inputs:
                if await _do_upload(info, self.cover_letter_path, "cover_letter"):
                    uploaded += 1

        return uploaded

    async def _deterministic_prefill(
        self,
        page: Page,
        frame: Optional[Any],
        actions: List[AgentAction],
    ) -> int:
        """Fill every fixed-policy field deterministically with a robust,
        self-verifying commit. No LLM. One-shot per selector per session.

        This is the architectural fix for the recurring "dropdown picked but
        not selected / regex mismatch / selector swap" failures: the AI never
        touches these fields, so it can't fumble them. Reliable react-select
        commit (open → read real options → fuzzy-match → click → verify →
        retry with keyboard) means the value actually lands in form state,
        which unblocks the pre-submit gate.
        """
        import re as _re
        ctx = frame or page
        policy = self._policy_fields()
        attempted = {
            (a.selector or "").strip()
            for a in actions
            if a.raw and a.raw.get("source") == "deterministic_prefill"
        }

        # Scan visible fillable fields with their resolved labels + options.
        try:
            fields = await ctx.evaluate(_POLICY_SCAN_JS)
        except Exception as exc:
            logger.debug(f"[AgentLoop] deterministic scan failed: {exc}")
            return 0

        filled = 0
        for f in fields or []:
            sel = (f.get("sel") or "").strip()
            if not sel or sel in attempted:
                continue
            label = (f.get("label") or "").lower()
            ftype = (f.get("type") or "text").lower()
            current = (f.get("value") or "").strip()
            if not label:
                continue
            # Match against policy (first match wins).
            value = None
            aliases = []
            for pat, val, als in policy:
                if _re.search(pat, label, _re.I):
                    # LinkedIn carve-out: don't let website/github/portfolio match.
                    if val == "N/A" and any(w in label for w in ("website", "portfolio", "github")):
                        continue
                    # Country carve-out: skip citizenship / country-code.
                    if val == "United States" and any(w in label for w in ("citizenship", "code", "nationality")):
                        continue
                    # Phone carve-out: never type the phone NUMBER into a
                    # country-code / extension field (separate small widgets).
                    _phone_v = ((self.profile or {}).get("phone") or "").strip()
                    if _phone_v and val == _phone_v and any(
                        w in label for w in ("country code", "country", "code", "extension", " ext")
                    ):
                        continue
                    value, aliases = val, als
                    break
            if value is None:
                continue
            # Skip if already correctly filled.
            if current and value.lower() in current.lower():
                attempted.add(sel)
                continue
            ok = await self._commit_policy_field(page, ctx, sel, ftype, value, aliases)
            # Record attempt regardless so we don't retry-thrash; if commit
            # failed, the AI can still try via its normal path next turns.
            actions.append(AgentAction(
                kind="fill_field", selector=sel, value=value,
                field_label=f.get("label", "")[:100], step=-1, ok=ok,
                raw={"source": "deterministic_prefill"},
            ))
            if ok:
                logger.info(f"[AgentLoop] deterministic fill: '{label[:50]}' = '{value}'")
                filled += 1
                # This loop previously committed every field back-to-back with
                # zero delay — logs showed 4 fields land within 223ms of each
                # other, which is a dead giveaway of automation to timing-
                # based anti-bot heuristics (Ashby in particular). A human
                # pauses between fields even when they're quick to answer.
                await self._human_delay()
            else:
                logger.debug(f"[AgentLoop] deterministic fill could not commit '{label[:50]}'")
        return filled

    async def _commit_policy_field(self, page, ctx, sel, ftype, value, aliases) -> bool:
        """Robust, verifying commit for one field. Returns True only if the
        value actually landed in form state."""
        try:
            loc = ctx.locator(sel).first
            if await loc.count() == 0:
                return False
        except Exception:
            return False

        # Resolve the real element type (the scan's type can be stale).
        try:
            real_type = (await loc.evaluate("el => (el.getAttribute('role')==='combobox') ? 'combobox' : (el.type || el.tagName.toLowerCase())")) or ftype
        except Exception:
            real_type = ftype
        real_type = real_type.lower()

        # ── Checkbox ──
        if real_type == "checkbox":
            try:
                await loc.check(timeout=3000, force=True)
                return bool(await loc.is_checked())
            except Exception:
                try:
                    await loc.click(timeout=2000, force=True)
                    return True
                except Exception:
                    return False

        # ── Radio ──
        if real_type == "radio":
            try:
                await loc.check(timeout=3000, force=True)
                return True
            except Exception:
                return False

        # ── Native <select> ──
        if real_type in ("select-one", "select"):
            try:
                opts = await loc.evaluate(
                    "el => Array.from(el.options).filter(o=>o.value).map(o=>o.text.trim())"
                )
            except Exception:
                opts = []
            target = self._best_option(value, aliases, opts) if opts else value
            for how in ("label", "value"):
                try:
                    if how == "label":
                        await loc.select_option(label=target, timeout=2500)
                    else:
                        await loc.select_option(value=target, timeout=2500)
                    return True
                except Exception:
                    continue
            return False

        # ── react-select / combobox ── open → read THIS field's options →
        # pick best → click → verify. Critically, options are read ONLY from
        # the menu this field controls (via aria-controls / aria-owns), NOT
        # every .select__option on the page. Without scoping, the phone
        # widget's 200-country dropdown polluted the list and the real
        # option (e.g. "Acknowledge/Confirm") was missed.
        if real_type == "combobox" or "select" in (sel or ""):
            # Acknowledgment / consent fields don't have a "Yes" option —
            # their only real option is a phrase like "Acknowledge/Confirm"
            # or "I have reviewed and confirmed…". When our literal value
            # isn't among the options, fall back to whichever option matches
            # an affirmative-acknowledgment pattern.
            ack_aliases = list(aliases) + [
                "acknowledge", "confirm", "i acknowledge", "i agree",
                "i confirm", "i have read", "i understand",
                "reviewed and confirmed", "accurate",
            ]
            for attempt in (1, 2):
                try:
                    await loc.scroll_into_view_if_needed(timeout=2000)
                    await loc.click(timeout=3000)
                    await page.wait_for_timeout(300)
                    # Read options ONLY from this field's own listbox.
                    rendered = await ctx.evaluate(_SCOPED_OPTIONS_JS, sel)
                    pool = rendered if rendered else []
                    target = self._best_option(value, aliases, pool)
                    # If the literal value didn't match any real option AND
                    # this is a consent-style field, pick the affirmative one.
                    if pool and target == value and value.lower() not in [o.lower() for o in pool]:
                        for o in pool:
                            ol = o.lower()
                            if any(k in ol for k in ack_aliases):
                                target = o
                                break
                    # Click the option by exact text, scoped to this menu.
                    clicked = await ctx.evaluate(
                        """([s, t]) => {
                            const want = (t||'').trim().toLowerCase();
                            const el = document.querySelector(s);
                            if (!el) return false;
                            // Resolve this field's listbox via aria-controls/owns,
                            // else the menu inside its own select container.
                            let menu = null;
                            const lid = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
                            if (lid) menu = document.getElementById(lid);
                            if (!menu) {
                                const cont = el.closest('[class*="select__container"],[class*="-container"],[class*="select"]');
                                if (cont) menu = cont.querySelector('[class*="select__menu"],[role="listbox"]');
                            }
                            const scope = menu || document;
                            const nodes = scope.querySelectorAll(
                                '[class*="select__option"],[role="option"]'
                            );
                            for (const n of nodes) {
                                if ((n.textContent||'').trim().toLowerCase() === want) { n.click(); return true; }
                            }
                            for (const n of nodes) {
                                if ((n.textContent||'').trim().toLowerCase().startsWith(want.slice(0,18))) { n.click(); return true; }
                            }
                            // Last resort: if exactly one real option exists, click it.
                            if (nodes.length === 1) { nodes[0].click(); return true; }
                            return false;
                        }""", [sel, target],
                    )
                    if not clicked:
                        # keyboard fallback. Two regimes:
                        #   • Static react-select: type filters, Enter picks.
                        #   • API-backed autocomplete (e.g. Greenhouse's
                        #     #candidate-location): typing fires an async query.
                        #     Suggestions arrive 300–1200 ms later; we must wait
                        #     for the listbox to populate before clicking,
                        #     otherwise Enter commits a free-text value the
                        #     server rejects.
                        try:
                            await loc.fill("")
                        except Exception:
                            pass
                        await loc.type(str(target)[:30], delay=35)

                        # Poll the scoped listbox for up to 1.6s. Quit early
                        # the moment the first option text matches our target
                        # so we don't burn the full budget on a fast static menu.
                        clicked_via_listbox = False
                        for _ in range(8):
                            await page.wait_for_timeout(200)
                            try:
                                pool2 = await ctx.evaluate(_SCOPED_OPTIONS_JS, sel) or []
                            except Exception:
                                pool2 = []
                            if not pool2:
                                continue
                            target2 = self._best_option(value, aliases, pool2)
                            clicked_via_listbox = await ctx.evaluate(
                                """([s, t]) => {
                                    const want = (t||'').trim().toLowerCase();
                                    const el = document.querySelector(s);
                                    if (!el) return false;
                                    let menu = null;
                                    const lid = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
                                    if (lid) menu = document.getElementById(lid);
                                    if (!menu) {
                                        const cont = el.closest('[class*="select__container"],[class*="-container"],[class*="select"]');
                                        if (cont) menu = cont.querySelector('[class*="select__menu"],[role="listbox"]');
                                    }
                                    const scope = menu || document;
                                    const nodes = scope.querySelectorAll('[class*="select__option"],[role="option"]');
                                    for (const n of nodes) {
                                        if ((n.textContent||'').trim().toLowerCase() === want) { n.click(); return true; }
                                    }
                                    // First suggestion when nothing matches exactly
                                    if (nodes.length) { nodes[0].click(); return true; }
                                    return false;
                                }""", [sel, target2],
                            )
                            if clicked_via_listbox:
                                break
                        if not clicked_via_listbox:
                            await loc.press("Enter")
                    await page.wait_for_timeout(300)
                    committed = await ctx.evaluate(
                        """(s) => {
                            const el = document.querySelector(s);
                            if (!el) return false;
                            const ctrl = el.closest('[class*="select__control"],[class*="-control"]');
                            if (ctrl) {
                                const sv = ctrl.querySelector('[class*="singleValue"],[class*="-singleValue"],[class*="multi-value"]');
                                return !!(sv && sv.textContent.trim());
                            }
                            return !!(el.value && el.value.trim());
                        }""", sel,
                    )
                    if committed:
                        return True
                except Exception as exc:
                    logger.debug(f"[AgentLoop] combobox commit attempt {attempt} failed: {exc}")
            return False

        # ── react-tel-input phone widget (e.g. Talent.com) ──
        # This is a strictly-controlled React widget that REJECTS a native-set
        # and mangles a full "+<cc>" (the country-code collision), so we defer it
        # to the AgentLoop's react-tel-aware fill (clear + national digits).
        # NOTE: do NOT defer intl-tel-input (`.iti`, used by Greenhouse) — that
        # is a plain <input> with a flag dropdown; a native-set commits fine and
        # the AI's national-digit logic would actually be WRONG for it. Only the
        # genuine react-tel-input (or #phone-input) is deferred.
        try:
            if await loc.evaluate(
                "(el) => !!(el.closest && el.closest('.react-tel-input')) "
                "|| el.id === 'phone-input'"
            ):
                return False
        except Exception:
            pass

        # ── text / email / tel / url / textarea ── native setter
        try:
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
                }""", value,
            )
            if not committed:
                await loc.fill(value, timeout=3000)
            return True
        except Exception:
            return False

    @staticmethod
    def _best_option(value: str, aliases: list, options: list) -> str:
        """Pick the option text that best matches value/aliases. Exact (ci)
        first, then alias exact, then substring, then the original value."""
        if not options:
            return value
        wants = [value.lower()] + [a.lower() for a in (aliases or [])]
        ol = [(o, o.strip().lower()) for o in options]
        for w in wants:
            for orig, low in ol:
                if low == w:
                    return orig
        for w in wants:
            for orig, low in ol:
                if w in low or low in w:
                    return orig
        return value

    async def _read_turnstile_token(self, ctx) -> str:
        try:
            return await ctx.evaluate(
                """() => { const i=document.querySelector(
                    'input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]');
                    return i ? (i.value || '') : ''; }"""
            ) or ""
        except Exception:
            return ""

    async def _settle_turnstile(self, page: Page, frame: Optional[Any]) -> bool:
        """Settle a Cloudflare Turnstile bot-check (e.g. Talent.com's review →
        'Send application' gate).

        IMPORTANT: Turnstile has NO audio challenge, so the vendored Whisper
        "capsolver" (which only solves reCAPTCHA v2) cannot touch it. Strategy,
        in order:
          1. Managed/auto mode — on a trusted (typically residential) IP CF
             auto-issues a token to the hidden ``cf-turnstile-response`` input;
             we poll for it (and click the interactive checkbox if present).
          2. CapSolver.com API fallback — when ``CAPSOLVER_API_KEY`` is set we
             fetch a token via AntiTurnstileTaskProxyLess and inject it. This is
             IP-independent and is the reliable path on datacenter IPs.

        Triggers when a Turnstile widget is present OR the submit button is
        disabled (the widget often loads a beat after the review page renders,
        so keying off the disabled submit catches it). Cheap when nothing
        matches; safe to call every turn.
        """
        ctx = frame or page
        try:
            info = await ctx.evaluate(
                """() => {
                    const i = document.querySelector(
                        'input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]');
                    const ifr = document.querySelector("iframe[src*='challenges.cloudflare.com']");
                    const div = document.querySelector('.cf-turnstile,[data-sitekey],[id*="turnstile"]');
                    const send = Array.from(document.querySelectorAll('button')).find(
                        b => /send application|submit application/i.test(b.innerText||''));
                    return {
                        widget: !!(i || ifr || div),
                        token: i ? (i.value || '') : '',
                        send_disabled: send ? !!send.disabled : null,
                    };
                }"""
            )
        except Exception:
            return True
        # Only do the long managed-token wait when an ACTUAL Turnstile widget is
        # present (visible/explicit variant that pre-populates a token). If the
        # submit is merely disabled with NO widget, it's either an invisible
        # on-submit Turnstile (nothing to pre-solve — it runs on the click) or a
        # non-captcha gate; a 100s wait there is pointless and makes the run feel
        # stuck. In that case do a brief check and move on.
        if len(info.get("token") or "") > 20:
            return True
        if not info.get("widget"):
            # No widget to settle. Give the page a brief moment in case the
            # widget renders late, then return without blocking.
            for _ in range(3):
                await asyncio.sleep(1.5)
                if len(await self._read_turnstile_token(ctx)) > 20:
                    return True
                try:
                    has_w = await ctx.evaluate(
                        "() => !!document.querySelector(\"iframe[src*='challenges.cloudflare.com'],.cf-turnstile,[data-sitekey]\")"
                    )
                except Exception:
                    has_w = False
                if has_w:
                    break
            else:
                return False  # invisible/on-submit turnstile or non-captcha gate

        logger.info(
            "[AgentLoop] Cloudflare Turnstile widget detected — settling "
            "(managed-token wait → CapSolver fallback)…"
        )
        import time as _t
        deadline = _t.monotonic() + 25.0
        while _t.monotonic() < deadline:
            try:
                for f in page.frames:
                    if "challenges.cloudflare.com" in (f.url or ""):
                        for cbsel in ("input[type='checkbox']", "label", "body"):
                            cb = f.locator(cbsel).first
                            if await cb.count() > 0 and await cb.is_visible():
                                await cb.click(timeout=1500)
                                break
                        break
            except Exception:
                pass
            await asyncio.sleep(2.0)
            if len(await self._read_turnstile_token(ctx)) > 20:
                logger.info("[AgentLoop] Turnstile token acquired (managed) — submit gate cleared")
                return True

        # ── FlareSolverr local API fallback ──────────────────────────────────────
        # Use local/system FlareSolverr to solve the Turnstile challenge
        # (This is fast, free, and local if running)
        flaresolverr_url = os.getenv("FLARESOLVERR_URL", "").strip()
        if flaresolverr_url:
            if await self._solve_turnstile_flaresolverr(page, ctx, flaresolverr_url):
                return True

        # ── CapSolver.com API fallback (IP-independent) ──────────────────────
        key = os.getenv("CAPSOLVER_API_KEY", "").strip()
        if key and not key.lower().startswith("your_"):
            if await self._solve_turnstile_capsolver(page, ctx, key):
                return True
        logger.warning(
            "[AgentLoop] Turnstile NOT settled. The managed token never populated "
            "(common on datacenter IPs) and no usable CAPSOLVER_API_KEY is set. "
            "Set CAPSOLVER_API_KEY (Turnstile-capable) or PROXY_URL (residential) "
            "to clear the final-submit gate. NOTE: the Whisper 'capsolver' only "
            "solves reCAPTCHA — it cannot solve Turnstile."
        )
        return False

    async def _solve_turnstile_capsolver(self, page: Page, ctx, key: str) -> bool:
        """Solve Cloudflare Turnstile via the CapSolver.com REST API
        (AntiTurnstileTaskProxyLess) and inject the token into the page."""
        import httpx
        import random
        try:
            meta = await ctx.evaluate(
                """() => {
                    let sk = '';
                    const d = document.querySelector('.cf-turnstile[data-sitekey],[data-sitekey]');
                    if (d) sk = d.getAttribute('data-sitekey') || '';
                    if (!sk) {
                        const ifr = document.querySelector("iframe[src*='challenges.cloudflare.com']");
                        if (ifr) { const m = (ifr.src||'').match(/[?&]k=([^&]+)/); if (m) sk = decodeURIComponent(m[1]); }
                    }
                    return { sitekey: sk, url: location.href };
                }"""
            )
            sitekey = (meta or {}).get("sitekey") or ""
            url = (meta or {}).get("url") or page.url
            if not sitekey:
                logger.warning("[AgentLoop] CapSolver: Turnstile sitekey not found on page")
                return False
            async with httpx.AsyncClient(timeout=30) as c:
                for task_attempt in range(2):
                    r = await c.post(
                        "https://api.capsolver.com/createTask",
                        json={"clientKey": key, "task": {
                            "type": "AntiTurnstileTaskProxyLess",
                            "websiteURL": url, "websiteKey": sitekey,
                        }},
                    )
                    j = r.json()
                    if j.get("errorId"):
                        logger.warning(f"[AgentLoop] CapSolver createTask failed: {j.get('errorDescription')}")
                        if task_attempt < 1:
                            await asyncio.sleep(random.uniform(1.5, 3.0))
                            continue
                        return False
                    task_id = j.get("taskId")
                    token_injected = False
                    for _ in range(24):
                        await asyncio.sleep(3.0 + random.uniform(0.1, 1.2))
                        rr = await c.post(
                            "https://api.capsolver.com/getTaskResult",
                            json={"clientKey": key, "taskId": task_id},
                        )
                        jr = rr.json()
                        if jr.get("errorId"):
                            err_desc = (jr.get("errorDescription") or "")
                            logger.warning(f"[AgentLoop] CapSolver getTaskResult error: {err_desc}")
                            if "UNSOLVABLE" in err_desc.upper() and task_attempt < 1:
                                break  # Break inner loop to retry createTask
                            return False
                        if jr.get("status") == "ready":
                            token = (jr.get("solution") or {}).get("token") or ""
                            if not token:
                                return False
                            token_injected = True
                            break
                    if token_injected:
                        await ctx.evaluate(
                            """(tok) => {
                                document.querySelectorAll(
                                    'input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]'
                                ).forEach(i => {
                                    i.value = tok;
                                    i.dispatchEvent(new Event('input',  {bubbles:true}));
                                    i.dispatchEvent(new Event('change', {bubbles:true}));
                                });
                            }""",
                            token,
                        )
                        logger.info("[AgentLoop] CapSolver Turnstile token injected — submit gate cleared")
                        return True
            return False
        except Exception as exc:
            logger.warning(f"[AgentLoop] CapSolver Turnstile solve failed: {exc}")
            return False

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
        # get_live_frame is async (was being called without await before, which
        # produced a RuntimeWarning and returned a coroutine object that the
        # downstream code can't iterate). Also note its signature is
        # (frame_locator, max_retries, page=...), not (page, frame).
        ctx_frame = frame
        try:
            from ..frame_utils import get_live_frame
            if frame is not None:
                resolved = await get_live_frame(frame, page=page)
                ctx_frame = resolved or frame
        except Exception as exc:
            logger.debug(f"[verify] frame re-resolve failed: {exc}")

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
        try:
            code = await fetch_verification_code(
                self.candidate_id, after_epoch=after_epoch, timeout_s=90.0
            )
        except Exception as exc:
            if "GMAIL_AUTH_FAILED" in str(exc):
                logger.error(f"[verify] {exc}")
                actions.append(AgentAction(
                    kind="abort",
                    reason="GMAIL_AUTH_FAILED: Gmail token expired or invalid. Please reconnect Gmail.",
                    step=-1, ok=False, raw={}
                ))
                return False
            code = None

        if not code:
            logger.warning(
                "[verify] no verification code fetched — Gmail not connected, "
                "no matching email arrived, or OAuth refused. Falling back."
            )
            return False
        # ── Make verification VISIBLE to a human watching headed mode ──
        # In headed runs the operator wants to see the code being typed (proof
        # that Gmail fetch worked and the right candidate's inbox was used).
        # Scroll the first input into the viewport with a small pause BEFORE
        # filling, and a longer pause AFTER, so the typing is observable rather
        # than a blink. Controlled by VERIFY_DEMO_PAUSE_MS (default 0 in headless,
        # set e.g. 1500 in headed local runs).
        _demo_pause_ms = int(os.getenv("VERIFY_DEMO_PAUSE_MS", "0"))
        try:
            ctx_for_scroll = ctx_frame or page
            await ctx_for_scroll.locator(shape.selectors[0]).first.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        if _demo_pause_ms > 0:
            await asyncio.sleep(_demo_pause_ms / 1000.0)
        ok = await fill_code(page, ctx_frame, shape, code)
        if not ok:
            logger.warning(f"[verify] failed to fill code {code!r} into shape={shape}")
            return False
        if _demo_pause_ms > 0:
            await asyncio.sleep(_demo_pause_ms / 1000.0)
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

        # ── Backward-navigation guard state ──────────────────────────────
        # Multi-step ATS flows (iCIMS, Workday, LinkedIn) move FORWARD only:
        # listing → consent → profile → questions → confirmation. If the AI
        # ever emits `navigate_url` to a host+path it has already visited it
        # is undoing its own progress — usually because a transient SPA
        # render confused it. We reject those navigations and force a
        # re-perceive so the next LLM turn sees the current screen again.
        def _norm_url(u: str) -> str:
            from urllib.parse import urlparse
            try:
                p = urlparse(u or "")
                return f"{(p.hostname or '').lower()}{(p.path or '').rstrip('/').lower()}"
            except Exception:
                return (u or "").lower()
        visited_urls: set[str] = set()
        try:
            visited_urls.add(_norm_url(page.url))
        except Exception:
            pass

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

        # Post-submit verification lock. Once a submit click has fired, the
        # NEXT priority is the email-verification code screen — NOT re-filling
        # the form. This flag flips the loop into a dedicated verification
        # phase so the AI never wanders back to filling fields after submit
        # (the "it keeps filling after submit" bug).
        submit_fired = False
        _submit_epoch = None
        post_submit_no_code_turns = 0   # confirmation turns with no verify-wall after submit
        verification_attempts = 0

        # ── Mid-flow OTP / email-verification state ──────────────────────────
        # Some ATSes (Talent.com) gate the application behind an emailed OTP
        # code BEFORE the form (email -> "check your email" -> 6-box code ->
        # contact info). The post-submit verification phase above only fires
        # AFTER a submit; this handles a code screen that appears mid-flow.
        # `_otp_after_epoch` bounds which inbox emails count as "this run's"
        # code — set to a few minutes before the loop starts so a code emailed
        # moments ago (after the AI submits the email) is in range.
        otp_attempts = 0
        otp_filled_once = False
        otp_last_fill_step = -10
        _otp_after_epoch = int(_time.time()) - 300

        try:
            for step in range(1, self.max_steps + 1):
                # Wall-clock check. Once submit has fired we are in the
                # post-submit verification phase (Gmail code polling can take
                # up to ~3 attempts × 90s). The form-fill budget alone would cut
                # that off mid-poll — the exact "clicks submit but never fetches
                # the code" failure — so grant a dedicated post-submit grace
                # budget on top of the fill budget.
                _wall_budget = _DEFAULT_WALL_TIMEOUT_S + (
                    _POST_SUBMIT_GRACE_S if submit_fired else 0.0
                )
                if _time.monotonic() - _wall_start > _wall_budget:
                    logger.warning(
                        f"[AgentLoop] wall-clock timeout {_wall_budget:.0f}s "
                        f"reached at step={step} (submit_fired={submit_fired}) — aborting"
                    )
                    return LoopResult(
                        success=False,
                        status="MAX_STEPS",
                        error=f"wall-clock timeout after {_wall_budget:.0f}s",
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

                # ── POST-SUBMIT VERIFICATION PHASE ───────────────────────────
                # Once submit has fired, stop driving the form-fill loop. Check
                # for the email-verification code screen and handle ONLY that.
                # This is a hard gate: no LLM call, no field re-fill — exactly
                # the "after submit, only fetch the code" behavior requested.
                if submit_fired:
                    try:
                        from ..verification import detect_code_input
                        _vframe = live_frame or frame
                        shape = await detect_code_input(page, _vframe)
                    except Exception as exc:
                        logger.debug(f"[AgentLoop] code-input detect failed: {exc}")
                        shape = None

                    if shape:
                        # Early-abort: if the candidate has no Gmail token at
                        # all, polling for the code is pointless — fail fast
                        # with a specific reason so the operator knows to
                        # reconnect Gmail rather than thinking it's a form bug.
                        # We check ONCE per loop (cached on self) to avoid a
                        # DB round-trip on every post-submit turn.
                        if getattr(self, "_gmail_token_ok", None) is None:
                            # _load_refresh_token now raises RuntimeError on
                            # transient DB failures (closed event loop, pool
                            # drops) — those are NOT "no token". Only a clean
                            # None return means the candidate genuinely lacks a
                            # token. Degrade OPEN on any exception so a
                            # transient DB blip doesn't kill the run.
                            try:
                                from ..verification.code_fetcher import _load_refresh_token
                                _tok = await _load_refresh_token(self.candidate_id) if self.candidate_id else None
                                self._gmail_token_ok = bool(_tok)
                            except Exception as exc:
                                logger.warning(
                                    f"[AgentLoop] gmail pre-check errored ({exc!r}) — "
                                    "degrading OPEN; fetch_verification_code will retry."
                                )
                                self._gmail_token_ok = True
                        if not self._gmail_token_ok:
                            logger.error(
                                "[AgentLoop] verification screen detected but candidate "
                                "has no Gmail refresh token — aborting (would otherwise "
                                "burn the wall-clock polling an inbox we can't read)."
                            )
                            return LoopResult(
                                success=False, status="VERIFICATION_FAILED",
                                error="GMAIL_NOT_CONNECTED: candidate has not authorized Gmail, cannot fetch verification code",
                                steps_taken=step, actions=actions,
                            )
                        # Only count a turn as an attempt when we actually
                        # FILLED a code. A failed Gmail fetch (token expired,
                        # email not yet delivered) is a wait, not a strike —
                        # otherwise three slow-delivering emails kill the run
                        # with VERIFICATION_FAILED without us ever entering a
                        # digit. The attempt counter increments AFTER ok_v=True.
                        if verification_attempts > 3:
                            logger.warning(
                                "[AgentLoop] verification code screen still present "
                                "after 3 FILLED attempts — aborting to avoid lockout."
                            )
                            return LoopResult(
                                success=False, status="VERIFICATION_FAILED",
                                error="Could not clear email verification code screen",
                                steps_taken=step, actions=actions,
                            )
                        logger.info(
                            f"[AgentLoop] step={step} POST-SUBMIT: verification "
                            f"code screen detected (filled_attempts={verification_attempts}); "
                            "fetching code from Gmail — NOT re-filling form."
                        )
                        ok_v = await self._handle_email_verification(
                            page, live_frame,
                            after_epoch=_submit_epoch if _submit_epoch else int(_time.time()) - 600,
                            actions=actions,
                        )
                        if ok_v:
                            verification_attempts += 1
                            # Code filled — click the verify/submit button and
                            # let the next turn observe the result. Greenhouse
                            # labels this "Submit Code", Ashby uses "Confirm",
                            # generic forms use Verify/Continue. List ordered
                            # most-specific first so the right button wins.
                            await self._human_delay()
                            _clicked = False
                            try:
                                for s in (
                                    "button:has-text('Submit Code')",
                                    "button:has-text('Submit code')",
                                    "button:has-text('Verify Code')",
                                    "button:has-text('Verify code')",
                                    "button:has-text('Confirm Code')",
                                    "button:has-text('Confirm')",
                                    "button:has-text('Verify')",
                                    "button:has-text('Continue')",
                                    "button:has-text('Next')",
                                    "button:has-text('Submit')",
                                    "button[type='submit']",
                                ):
                                    btn = (live_frame or page).locator(s).first
                                    if await btn.count() > 0 and await btn.is_visible():
                                        await btn.click(timeout=5000)
                                        logger.info(f"[AgentLoop] post-code submit via {s!r}")
                                        _clicked = True
                                        break
                            except Exception as exc:
                                logger.debug(f"[AgentLoop] post-code submit click failed: {exc}")
                            # Enter-key fallback. Some ATSes (Greenhouse split
                            # boxes, Lever single input) submit the code on
                            # Enter without a button — or the button is hidden
                            # behind a parent the locator can't see. Pressing
                            # Enter while focus is on the code input is a no-op
                            # if a button already handled it, but unblocks
                            # button-less widgets.
                            if not _clicked:
                                try:
                                    if shape and shape.selectors:
                                        await (live_frame or page).locator(shape.selectors[-1]).first.press("Enter")
                                    else:
                                        await page.keyboard.press("Enter")
                                    logger.info("[AgentLoop] post-code submit via Enter key fallback")
                                except Exception as exc:
                                    logger.debug(f"[AgentLoop] Enter-key fallback failed: {exc}")
                            # Wait for the screen to settle so the next-turn
                            # detect_code_input sees the new DOM, not the stale
                            # verification screen that's about to navigate away.
                            try:
                                await page.wait_for_load_state("networkidle", timeout=8000)
                            except Exception:
                                pass
                            await asyncio.sleep(2.5)
                        else:
                            # Gmail returned no code yet — wait for delivery.
                            # Do NOT bump the attempts counter; this is a poll,
                            # not a failed attempt. The wall-clock budget +
                            # _POST_SUBMIT_GRACE_S is what bounds total wait.
                            logger.info(
                                f"[AgentLoop] step={step} POST-SUBMIT: code not yet "
                                "in Gmail — waiting 6s before next poll (no strike)."
                            )
                            await asyncio.sleep(6.0)
                        continue
                    else:
                        # No code screen visible. Either the submit fully
                        # succeeded or the page navigated to a success state.
                        # Give it a couple of confirmation turns; if no
                        # verification wall ever appears, the application was
                        # accepted outright — return SUBMITTED deterministically
                        # rather than risk the AI drifting until the wall-clock
                        # (which the executor would misread as a non-terminal
                        # MAX_STEPS and wrongly re-run the scripted submit).
                        post_submit_no_code_turns += 1
                        logger.info(
                            f"[AgentLoop] step={step} POST-SUBMIT: no code screen "
                            f"(confirm turn {post_submit_no_code_turns}/2) — "
                            "submit likely complete."
                        )
                        if post_submit_no_code_turns >= 2:
                            # Before declaring success, actually READ the page —
                            # some ATSes (Ashby, Workday, Lever) render a
                            # rejection banner ("flagged as possible spam",
                            # "already applied") as a normal 200 OK page instead
                            # of an HTTP error or an OTP wall. Without this
                            # check, "no OTP screen" was being treated as proof
                            # of success, which is a false positive when the
                            # server actually rejected the submission.
                            rejection_hit = None
                            try:
                                _check_frame = live_frame or frame
                                for _ctx in ([_check_frame, page] if _check_frame else [page]):
                                    if _ctx is None:
                                        continue
                                    _content = (await _ctx.content()).lower()
                                    for _pattern in _POST_SUBMIT_REJECTION_PATTERNS:
                                        if _pattern in _content:
                                            rejection_hit = _pattern
                                            break
                                    if rejection_hit:
                                        break
                            except Exception as exc:
                                logger.debug(f"[AgentLoop] post-submit rejection scan failed (non-fatal): {exc}")
                            if rejection_hit:
                                logger.error(
                                    f"[AgentLoop] POST-SUBMIT: rejection banner detected "
                                    f"(pattern={rejection_hit!r}) — submission was NOT "
                                    "accepted despite no OTP wall. Reporting as failure, "
                                    "not SUBMITTED."
                                )
                                return LoopResult(
                                    success=False,
                                    status="ABORTED",
                                    error=f"SPAM_FLAGGED: server rejected the submission "
                                          f"(detected {rejection_hit!r} on the post-submit "
                                          "page). Do not retry — retrying would resubmit "
                                          "into the same rejection or escalate an anti-bot flag.",
                                    steps_taken=step,
                                    actions=actions,
                                )
                            logger.info(
                                "[AgentLoop] POST-SUBMIT: no verification wall after "
                                "2 turns — application submitted. Returning SUBMITTED."
                            )
                            return LoopResult(
                                success=True,
                                status="SUBMITTED",
                                confirmation="submitted_no_verification_required",
                                steps_taken=step,
                                actions=actions,
                            )
                        await asyncio.sleep(2.0)
                        continue

                # ── MID-FLOW OTP / EMAIL-VERIFICATION (e.g. Talent.com) ──────
                # Handle an emailed-code screen that appears BEFORE submit. Gate
                # strictly on a multi-box "split" widget (4–10 single-char
                # boxes): that shape is ALWAYS an OTP/code entry and is never a
                # normal email/contact input, so this cannot misfire on the
                # email-entry or contact-info pages. The runner fetches the code
                # from the candidate's Gmail and fills it — the AI never sees or
                # types the code itself.
                if (not submit_fired and self.candidate_id and otp_attempts < 4):
                    _otp_shape = None
                    try:
                        from ..verification import detect_code_input
                        _oframe = live_frame or frame
                        _otp_shape = await detect_code_input(page, _oframe)
                    except Exception as exc:
                        logger.debug(f"[AgentLoop] mid-flow code detect failed: {exc}")
                    if _otp_shape and _otp_shape.kind == "split":
                        # Anti-thrash: filling these auto-advancing OTP boxes and
                        # then RE-filling them on the next turn (~3s later) resets
                        # the widget mid-validation and prevents the auto-submit —
                        # the code never "takes". After a fill, give the page a few
                        # turns to validate + advance before touching it again.
                        if otp_filled_once and (step - otp_last_fill_step) <= 2:
                            logger.info(
                                f"[AgentLoop] step={step} MID-FLOW OTP already "
                                "filled recently — waiting for it to validate "
                                "(not re-filling)."
                            )
                            await asyncio.sleep(4.0)
                            continue
                        otp_attempts += 1
                        logger.info(
                            f"[AgentLoop] step={step} MID-FLOW OTP code screen "
                            f"detected (kind=split, digits={_otp_shape.digits}, "
                            f"attempt {otp_attempts}); fetching code from Gmail."
                        )
                        ok_otp = await self._handle_email_verification(
                            page, live_frame,
                            after_epoch=_otp_after_epoch,
                            actions=actions,
                        )
                        if ok_otp:
                            otp_filled_once = True
                            otp_last_fill_step = step
                            page_verified = True  # a code screen IS a real app flow
                            await self._human_delay()
                            for s in (
                                "button:has-text('Continue')",
                                "button:has-text('Verify')",
                                "button:has-text('Next')",
                                "button:has-text('Submit')",
                                "button:has-text('Confirm')",
                                "button[type='submit']",
                            ):
                                try:
                                    btn = (live_frame or page).locator(s).first
                                    if await btn.count() > 0 and await btn.is_visible():
                                        await btn.click(timeout=5000)
                                        logger.info(f"[AgentLoop] post-OTP advance via {s!r}")
                                        break
                                except Exception:
                                    continue
                            # These boxes auto-submit once all 6 chars are in;
                            # give the server time to validate + advance before
                            # the next turn re-checks (anti-thrash guard above
                            # prevents an immediate re-fill).
                            await asyncio.sleep(5.0)
                        else:
                            # Code not yet available — let Gmail deliver, retry.
                            await asyncio.sleep(4.0)
                        continue

                # ── CLOUDFLARE TURNSTILE settle (e.g. Talent.com submit gate) ──
                # Some flows gate the FINAL submit behind a Cloudflare Turnstile
                # (a hidden cf-turnstile-response that must be populated before
                # the submit button enables). Settle it deterministically each
                # turn — cheap when absent, and it ensures the token is present
                # by the time the AI clicks 'Send application'.
                try:
                    await self._settle_turnstile(page, live_frame or frame)
                except Exception as exc:
                    logger.debug(f"[AgentLoop] turnstile settle non-fatal: {exc}")

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

                    # ★ DETERMINISTIC FILE UPLOAD — runs BEFORE field prefill.
                    # The FORM STATUS check intentionally skips file inputs
                    # (.value is unreliable after set_input_files), so an
                    # un-uploaded resume looks like a "filled form" to the
                    # readiness gate. If we wait for the AI to upload, it
                    # frequently clicks Submit on its first turn because all
                    # NON-FILE fields are already populated by prefill/memory.
                    # Master-controlled, mechanical, idempotent — same model
                    # as _deterministic_prefill but for the file slots.
                    try:
                        up_count = await self._deterministic_file_upload(
                            page, live_frame, actions
                        )
                        if up_count:
                            logger.info(
                                f"[AgentLoop] step={step} deterministic uploaded "
                                f"{up_count} file(s) — no LLM used"
                            )
                            page_verified = True
                            # A resume upload followed instantly by fields
                            # committing is another timing tell — a human
                            # notices the upload finish before typing.
                            await self._human_delay()
                    except Exception as exc:
                        logger.debug(f"[AgentLoop] deterministic file upload error (non-fatal): {exc}")

                    # ★ DETERMINISTIC PRE-FILL — fill ALL fixed-policy fields
                    # (country=US, UK-auth=No, US-auth=Yes, sponsorship=No,
                    # LinkedIn=N/A, gender, race, orientation, transgender/
                    # disability/veteran=No, consent=affirm) with robust
                    # verifying commits. The AI never touches these, so it
                    # can't swap selectors / mismatch regex / fail to commit.
                    # Only genuinely custom questions are left for the LLM.
                    try:
                        det_filled = await self._deterministic_prefill(
                            page, live_frame, actions
                        )
                        if det_filled:
                            logger.info(
                                f"[AgentLoop] step={step} deterministic pre-fill committed "
                                f"{det_filled} fixed-policy field(s) — no LLM used"
                            )
                            page_verified = True
                    except Exception as exc:
                        logger.debug(f"[AgentLoop] deterministic prefill error (non-fatal): {exc}")

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
                        # Lever 3: 960x540 @ q35 is sufficient for Claude /
                        # Gemini. For Groq Llama-4-Scout the vision-token
                        # cost is higher per pixel and the TPM cap is tight
                        # (30k); shrinking to 720x405 @ q28 keeps each turn
                        # under ~2000 vision tokens.
                        try:
                            _eff = self._llm.effective_provider()
                        except Exception:
                            _eff = "anthropic"
                        if _eff == "groq":
                            sq = 28
                        else:
                            sq = 35
                        screenshot = await page.screenshot(
                            full_page=True,
                            type="jpeg",
                            quality=sq,
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
                # If the live provider changed (Anthropic exhausted → Groq),
                # rebuild the system prompt so the resume block is sized for
                # the new provider's token economics.
                try:
                    _live = self._llm.effective_provider()
                    if _live != self._prompt_provider:
                        logger.info(
                            f"[AgentLoop] live provider changed "
                            f"{self._prompt_provider}→{_live}; rebuilding system prompt"
                        )
                        self._prompt_provider = _live
                        self._system_prompt = self._build_system_prompt()
                except Exception:
                    pass
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
                                const seenRadioGroups = new Set();
                                const seenCheckboxFieldsets = new Set();
                                const inputs = document.querySelectorAll(
                                    'input:not([type=hidden]):not([type=submit]):not([type=button]), '
                                    + 'select, textarea, [role="combobox"]'
                                );
                                inputs.forEach(el => {
                                    if (out.length >= 12) return;
                                    // Radio groups: required lives on the GROUP (asterisk in the
                                    // question label / aria-required on the fieldset), almost
                                    // never on each individual <input type="radio">. Dedupe by
                                    // name so an N-option group is evaluated once, not N times.
                                    if (el.type === 'radio' && el.name) {
                                        if (seenRadioGroups.has(el.name)) return;
                                        seenRadioGroups.add(el.name);
                                    }
                                    // "Select all that apply" checkbox groups (Ashby: a
                                    // <fieldset> with 2+ checkboxes, each carrying the OPTION
                                    // TEXT as its own unique `name` — no shared name to dedupe
                                    // by like radios). Group by the fieldset element itself;
                                    // required means "at least one checked", not "every
                                    // checkbox checked".
                                    let checkboxGroupFieldset = null;
                                    if (el.type === 'checkbox') {
                                        const fs = el.closest('fieldset');
                                        if (fs && fs.querySelectorAll('input[type="checkbox"]').length >= 2) {
                                            if (seenCheckboxFieldsets.has(fs)) return;
                                            seenCheckboxFieldsets.add(fs);
                                            checkboxGroupFieldset = fs;
                                        }
                                    }
                                    // Resolve label first (also used for demographic detection).
                                    // Must mirror the FORM STATUS resolver — including the
                                    // parent-container walk — so demographic questions like
                                    // the Greenhouse disability dropdown (no label[for=...],
                                    // no fieldset/legend) are correctly classified instead
                                    // of being silently skipped. For a checkbox-group
                                    // representative, prefer the fieldset's own question-title
                                    // label over the individual option's label.
                                    let gLabel = checkboxGroupFieldset
                                        ? (checkboxGroupFieldset.querySelector(':scope > label, :scope > legend')?.textContent || '').trim()
                                        : '';
                                    if (!gLabel) gLabel = el.getAttribute('aria-label') || '';
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
                                    // Asterisk-in-label is the most common real-world required
                                    // marker (mirrors the FORM STATUS resolver and the Python-side
                                    // detect_form(), both of which already treat "*" in label as
                                    // required) — without it, radio-button questions with no
                                    // required/aria-required attribute on the input itself were
                                    // never gated, letting Submit fire while they sat unanswered.
                                    // Ashby-style builders mark required on the QUESTION LABEL (a
                                    // sibling of the input's wrapper), not the input or its
                                    // ancestors. [data-field-path] reliably wraps every Ashby
                                    // question; search WITHIN it instead of walking upward.
                                    const reqContainer_gate = el.closest('[data-field-path]');
                                    const containerRequired_gate = !!(reqContainer_gate && reqContainer_gate.querySelector('[class*="required" i]'));
                                    const isRequired = el.required
                                        || el.getAttribute('aria-required') === 'true'
                                        || (el.closest('[class*="required"]') !== null && !el.value)
                                        || isDemographic
                                        || /\\*/.test(gLabel)
                                        || containerRequired_gate;
                                    if (!isRequired) return;
                                    const style = window.getComputedStyle(el);
                                    // Ashby's Yes/No toggle widget keeps the real, form-bound
                                    // <input type="checkbox"> as display:none behind two visible
                                    // <button>Yes</button>/<button>No</button> elements — but
                                    // Ashby's React state still syncs onto the hidden checkbox's
                                    // `checked` property, so it remains the authoritative answer.
                                    // Exempt checkbox/radio from the display:none filter (mirrors
                                    // the FORM STATUS resolver) so these gate correctly.
                                    if (style.display === 'none' && el.type !== 'radio' && el.type !== 'checkbox') return;
                                    if (style.visibility === 'hidden') return;
                                    // Skip file inputs — they never report .value reliably even
                                    // after a successful upload (Playwright's setInputFiles
                                    // attaches the file but the DOM value stays a path that
                                    // varies by browser). The upload_file action records
                                    // success separately; gating submit on input.value is
                                    // a false-positive trap.
                                    if (el.type === 'file') return;
                                    // Skip phantom fields with NO id, NO name, no label-for, AND
                                    // no [data-field-path] container. These can't be addressed by
                                    // the AI (no selector to fill them with) so blocking submit on
                                    // them is an unresolvable deadlock. They're almost always
                                    // hidden honeypots, react-select internal proxies, or stray
                                    // inputs. Ashby's own combobox inputs (e.g. "Location") often
                                    // have EMPTY id AND name — without the data-field-path
                                    // fallback those were wrongly treated as unaddressable
                                    // phantoms and silently dropped from the gate entirely.
                                    const hasIdent = !!(el.id || el.name || reqContainer_gate?.getAttribute('data-field-path'));
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
                                        // Ashby's Yes/No toggle widget: a hidden checkbox paired
                                        // with two sibling <button>Yes</button>/<button>No</button>
                                        // elements. .checked is NOT reliable — some builds only
                                        // set checked=true for "Yes" and never for "No" (leaving
                                        // checked=false ambiguous between "unanswered" and
                                        // "answered No"), which caused the gate to loop the AI
                                        // forever re-clicking "No" since nothing ever read as
                                        // filled. The "active"-named class on whichever button
                                        // was clicked is the one reliable signal.
                                        const toggleContainer = el.closest('[data-field-path]');
                                        const activeToggleBtn = toggleContainer ? toggleContainer.querySelector('button[class*="active" i]') : null;
                                        if (activeToggleBtn) {
                                            val = 'checked';
                                        } else if (checkboxGroupFieldset) {
                                            // Checkbox-group semantics: satisfied if ANY checkbox
                                            // anywhere in the fieldset is checked (each option has
                                            // its own unique name, so a by-name query would only
                                            // ever see this one option's own state).
                                            val = checkboxGroupFieldset.querySelector('input[type="checkbox"]:checked') ? 'checked' : '';
                                        } else if (el.name) {
                                            // For radio groups, check if any sibling with same name is checked
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
                                        const fieldPathAttr = reqContainer_gate?.getAttribute('data-field-path');
                                        out.push({
                                            sel: el.id ? '#' + el.id
                                                : (el.name ? `[name="${el.name}"]`
                                                : (fieldPathAttr ? `[data-field-path="${fieldPathAttr}"] [role="combobox"]` : '')),
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
                                        // getComputedStyle only reflects the element's OWN
                                        // display/visibility — a hidden ANCESTOR (e.g. Lever's
                                        // `.resume-upload-oversize` wrapper, which stays
                                        // display:none forever unless the client-side oversize
                                        // check actually fires) doesn't show up here, so an
                                        // inner error-message tag can read as "visible" even
                                        // though nothing is rendered on screen. checkVisibility()
                                        // walks the whole ancestor chain and is the accurate
                                        // rendered-on-screen test; fall back to offsetParent for
                                        // browsers without it.
                                        const reallyVisible = typeof el.checkVisibility === 'function'
                                            ? el.checkVisibility({checkVisibilityCSS: true, checkOpacity: true})
                                            : el.offsetParent !== null;
                                        if (!reallyVisible) return;
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
                                # "File exceeds maximum upload size" is a
                                # validation state the AI has NO way to act
                                # on — it can't shrink the file or click
                                # anything to clear it, so it just repeat-
                                # clicked Submit (Lever's exact case: our
                                # actual resume was 4.4KB, nowhere near the
                                # 100MB limit — this is a spurious client-
                                # side validation state, not a real
                                # oversized file). Since we independently
                                # know our own resume file is small, the
                                # correct deterministic recovery is to
                                # re-upload it fresh rather than give the AI
                                # a turn it can't meaningfully use.
                                _oversize_hit = any(
                                    re.search(r"exceed|too large|maximum.{0,20}size", e, re.I)
                                    for e in visible_errors
                                )
                                # Only attempt this recovery ONCE per run. Some
                                # ATSes (Lever confirmed) abort any in-flight
                                # resume-parse request the instant a NEW file
                                # gets set on the same input (their own JS:
                                # `if (req.readyState < 4) req.abort()`).
                                # Re-uploading on every single gate-block was
                                # repeatedly interrupting our OWN prior
                                # attempt before it could ever finish
                                # settling, which likely compounded the
                                # problem instead of fixing it.
                                if (
                                    _oversize_hit and self.resume_path
                                    and not getattr(self, "_oversize_reupload_done", False)
                                ):
                                    self._oversize_reupload_done = True
                                    try:
                                        _ctx_for_reupload = frame or page
                                        _file_inputs = _ctx_for_reupload.locator("input[type='file']")
                                        _fi_count = await _file_inputs.count()
                                        _reuploaded = False
                                        for _fi in range(_fi_count):
                                            try:
                                                await _file_inputs.nth(_fi).set_input_files(
                                                    self.resume_path, timeout=8000
                                                )
                                                _reuploaded = True
                                            except Exception:
                                                continue
                                        if _reuploaded:
                                            logger.warning(
                                                "[AgentLoop] PRE-SUBMIT GATE: 'file too large' "
                                                "error looked spurious (our resume is small) — "
                                                "re-uploaded it deterministically. Waiting for "
                                                "the async resume-parse request to actually "
                                                "settle before giving the AI another turn."
                                            )
                                            try:
                                                await page.wait_for_load_state("networkidle", timeout=8000)
                                            except Exception:
                                                await asyncio.sleep(5.0)
                                    except Exception as exc:
                                        logger.debug(f"[AgentLoop] resume re-upload recovery failed: {exc}")
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
                            if unfilled and (phantom_only or prior_gate_blocks >= 2):
                                logger.warning(
                                    f"[AgentLoop] PRE-SUBMIT GATE: {len(unfilled)} unfilled "
                                    "field(s) detected. Gate has already blocked "
                                    f"{prior_gate_blocks} times — letting submit through; "
                                    "the form will surface real validation errors if any."
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
                                # Ashby's anti-bot scores submission TIMING, not just
                                # field completeness — a full run (resume upload,
                                # deterministic pre-fill, a couple of clicks) landing
                                # Submit within ~25s of first touching the page reads
                                # as bot-like and gets rejected with a "flagged as
                                # possible spam" banner even when every field was
                                # answered correctly. adapters/ashby.py's OWN submit()
                                # already had a dwell for exactly this reason, but it
                                # only fires on the deterministic-fallback path — never
                                # reached now that AgentLoop completes the flow itself.
                                # Apply the same dwell here, right before the real
                                # click, so it actually protects the common case.
                                if not self.stop_before_submit and "ashby" in (self.job_ctx or {}).get("platform", "").lower():
                                    _dwell_ms = int(os.getenv("ASHBY_PRE_SUBMIT_DWELL_MS", "4000"))
                                    if _dwell_ms > 0:
                                        # Jittered around the configured midpoint (±30%)
                                        # rather than a flat constant — waiting EXACTLY
                                        # the same number of milliseconds on every run is
                                        # itself a machine-like tell.
                                        _jittered_s = random.uniform(_dwell_ms * 0.7, _dwell_ms * 1.3) / 1000.0
                                        logger.info(f"[AgentLoop] Ashby pre-submit dwell {_jittered_s:.1f}s (anti-spam)")
                                        await asyncio.sleep(_jittered_s)
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
                if not page_verified and step >= self.verify_gate_limit:
                    logger.warning(f"[AgentLoop] page never verified after {self.verify_gate_limit} steps — aborting")
                    return LoopResult(
                        success=False,
                        status="WRONG_PAGE",
                        error=f"Agent did not verify page within {self.verify_gate_limit} steps",
                        steps_taken=step,
                        actions=actions,
                    )

                # ── Execute action ───────────────────────────────────────────────
                # Stamp the pre-execute wall-clock so the verification-code
                # fetcher knows which inbox emails are "after submit".
                _pre_exec_epoch = int(time.time())
                # Human-like pacing before mutating actions (fill/click/upload).
                # Observational actions (scroll/wait/verify) don't need it.
                if action.kind in ("fill_field", "click", "upload_file", "next_step"):
                    await self._human_delay()

                # ── Backward-navigation guard ─────────────────────────────
                # Reject navigate_url if it points at a host+path the loop
                # has already left. ATS application flows are forward-only
                # (listing → consent → profile → questions → confirm); going
                # back undoes progress and burns turns. The AI is told to
                # re-perceive instead of nav'ing away.
                if action.kind == "navigate_url" and action.url:
                    target_norm = _norm_url(action.url)
                    current_norm = _norm_url(page.url)
                    if target_norm and target_norm != current_norm and target_norm in visited_urls:
                        logger.warning(
                            f"[AgentLoop] step={step} NAV-BACK BLOCKED: target "
                            f"{action.url!r} → norm={target_norm!r} already visited. "
                            "Forcing re-perceive of current page instead."
                        )
                        action.ok = False
                        action.reason = (
                            f"BLOCKED: navigate_url to previously-visited "
                            f"{target_norm!r} would undo forward progress"
                        )
                        actions.append(action)
                        await asyncio.sleep(0.3)
                        continue

                # Computed here (before execution) rather than after, because
                # hcaptcha-challenger's AgentV must attach its page.on("response")
                # listener BEFORE the submit click fires hcaptcha.execute() —
                # the getcaptcha/hsw.js network exchange it needs to observe
                # are one-time events, not state we can inspect after the fact.
                _pre_click_text = ((action.selector or "") + " " + (action.click_text or "")).lower()
                _pre_is_submit = action.kind == "click" and bool(re.search(
                    r"submit|send application", _pre_click_text
                ))
                _lever_agentv = None
                _lever_net_events: List[Dict[str, Any]] = []
                _lever_net_handlers = None
                if (
                    _pre_is_submit
                    and "lever" in (self.job_ctx or {}).get("platform", "").lower()
                ):
                    try:
                        from hcaptcha_challenger import AgentV, AgentConfig
                        _lever_agentv = AgentV(page=page, agent_config=AgentConfig())
                    except Exception as exc:
                        logger.debug(f"[AgentLoop] Lever: hcaptcha-challenger unavailable: {exc}")

                    # Network instrumentation: whether our click on
                    # #hcaptchaSubmitBtn actually fires a form POST to Lever's
                    # server has been the mystery — the token gets injected,
                    # the click is dispatched, but the visible form doesn't
                    # transition. Watching every request from now until after
                    # the post-click poll finishes tells us: (a) did any POST
                    # to lever.co fire?, (b) what status code did it return?,
                    # (c) if it was an XHR/fetch, what was the response body?
                    # If NO POST ever fires, the click isn't reaching Lever's
                    # form-submit handler. If it fires with 4xx, server-side
                    # validation is rejecting. If it fires with 2xx, our
                    # thank-you detection is what's wrong.
                    def _on_req(req):
                        try:
                            _u = req.url or ""
                            if req.method == "POST" and "lever.co" in _u:
                                _lever_net_events.append({
                                    "kind": "request", "method": req.method,
                                    "url": _u, "time": time.monotonic(),
                                })
                                logger.info(
                                    f"[AgentLoop] Lever NET → POST {_u} "
                                    f"(headers={dict(req.headers).get('content-type','?')})"
                                )
                        except Exception:
                            pass

                    def _on_resp(resp):
                        try:
                            _u = resp.url or ""
                            if "lever.co" in _u and resp.request.method in ("POST", "PUT"):
                                _lever_net_events.append({
                                    "kind": "response", "status": resp.status,
                                    "url": _u, "time": time.monotonic(),
                                })
                                logger.info(
                                    f"[AgentLoop] Lever NET ← {resp.status} "
                                    f"{resp.request.method} {_u}"
                                )
                        except Exception:
                            pass

                    try:
                        page.on("request", _on_req)
                        page.on("response", _on_resp)
                        _lever_net_handlers = (_on_req, _on_resp)
                    except Exception as _exc:
                        logger.debug(f"[AgentLoop] Lever net listener install failed: {_exc}")

                ok = await _execute_action(
                    action, page, frame, self.resume_path, self.cover_letter_path
                )
                action.ok = ok

                # A successful fill/upload is self-evident proof we are on a
                # real application form — satisfy the verify-page gate even if
                # the model never emitted an explicit verify_page (weaker
                # fallback models often skip it and would otherwise trip the
                # "page never verified" abort). A wrong page could not have a
                # field filled or a file uploaded successfully.
                if ok and action.kind in ("fill_field", "upload_file"):
                    page_verified = True

                # Track every URL the page ends up on after each action so a
                # later navigate_url that points back at it gets blocked above.
                try:
                    visited_urls.add(_norm_url(page.url))
                except Exception:
                    pass

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
                # Genuine submit only — exclude the "Apply" button (which opens
                # the form) so the verification phase doesn't trigger early.
                _is_submit = action.kind == "click" and bool(re.search(
                    r"submit|send application", _click_text
                ))

                # ── Lever hCaptcha handling ──────────────────────────────
                # Lever gates real submission behind an INVISIBLE hCaptcha
                # that only starts executing on click of the visible
                # #btn-submit button (their own JS registers
                # `hcaptcha.execute(captchaId)` inside that button's click
                # listener). The button our AI clicks is NOT the real
                # <button type="submit"> — Lever renders a SEPARATE hidden
                # `#hcaptchaSubmitBtn` and only auto-clicks it from the
                # hCaptcha `onSuccess` callback, which is scoped inside a
                # closure and unreachable as `window.onSuccess` — so even a
                # correctly solved+injected token doesn't trigger the real
                # submission on its own. Confirmed live: the hidden
                # `#hcaptchaResponseInput` stays empty indefinitely under
                # our automated session; it never resolves invisibly like it
                # would for a normal user. Without this, the AI can click
                # "Submit" forever and the form will never actually send.
                if ok and _is_submit and "lever" in (self.job_ctx or {}).get("platform", "").lower():
                    try:
                        # Poll #hcaptchaResponseInput for up to 5s — many Lever
                        # postings resolve their hCaptcha INVISIBLY (no challenge
                        # frame ever renders, hcaptcha.execute() returns a
                        # pass:true response, the token is injected into the
                        # input by hCaptcha's own callback). In that mode there
                        # is nothing for AgentV to solve, and calling it errors
                        # with "Cannot find a valid challenge frame". A short
                        # poll catches this common case before we spend
                        # AgentV's budget on a non-existent challenge.
                        # #hcaptchaResponseInput is top-level Lever DOM — always
                        # use `page` (not `frame`, which may point at a nested
                        # frame and return null even when the top-level input
                        # has a value).
                        _hc_value = None
                        for _pre_tick in range(10):
                            await asyncio.sleep(0.5)
                            try:
                                _v = await page.evaluate(
                                    "() => { const el = document.getElementById('hcaptchaResponseInput'); "
                                    "return el ? el.value : null; }"
                                )
                            except Exception:
                                continue
                            if _v:
                                _hc_value = _v
                                logger.info(
                                    f"[AgentLoop] Lever: hCaptcha resolved invisibly at "
                                    f"t={(_pre_tick+1)*0.5:.1f}s — token present without AgentV."
                                )
                                break
                            if _pre_tick == 3:
                                # First 2s only — after that keep polling but
                                # don't spam the log.
                                logger.info(
                                    "[AgentLoop] Lever: invisible resolution not "
                                    "yet complete after 2s — will keep polling "
                                    "another 3s before invoking AgentV."
                                )
                            _hc_value = _v  # keep last-seen (None or "")
                        if _hc_value is not None and not _hc_value and _lever_agentv is not None:
                            # Attempt 1: hcaptcha-challenger's AgentV, listener
                            # already attached pre-click above. It intercepts
                            # hCaptcha's own /getcaptcha/ response — if hCaptcha's
                            # server-side risk check already says "pass": true,
                            # it captures that directly; otherwise it injects
                            # hCaptcha's own hsw.js into this real page and runs
                            # hCaptcha's real verification function. No external
                            # token farm, reuses our existing GEMINI_API_KEY.
                            try:
                                logger.info(
                                    "[AgentLoop] Lever: trying hcaptcha-challenger "
                                    "(AgentV) before falling back to nopecha."
                                )
                                from hcaptcha_challenger.models import ChallengeSignal
                                # 100s, not 60s: live runs showed a successful
                                # drag-drop solve taking ~39s once and >60s
                                # another time — 60 was cutting off genuine
                                # in-progress solves. Close to the library's
                                # own EXECUTION_TIMEOUT(120)+RESPONSE_TIMEOUT(30)
                                # budget while still leaving room in our own
                                # 360s AgentLoop wall-clock for the rest of the
                                # flow (nopecha fallback, hidden-button click).
                                signal = await asyncio.wait_for(
                                    _lever_agentv.wait_for_challenge(), timeout=100,
                                )
                                logger.info(f"[AgentLoop] Lever: AgentV signal={signal}")
                                # Instrumentation: dump top-of-page state at the
                                # exact moment SUCCESS fires. Prior runs lost this
                                # evidence entirely — if the run turns out to have
                                # submitted successfully already, this proves it.
                                try:
                                    _sig_snap = await page.evaluate(
                                        """() => ({
                                            url: window.location.href,
                                            title: document.title,
                                            hasSubmitBtn: !!document.getElementById('hcaptchaSubmitBtn'),
                                            hasRespInput: !!document.getElementById('hcaptchaResponseInput'),
                                            respLen: (document.getElementById('hcaptchaResponseInput') || {}).value?.length || 0,
                                            hasForm: !!document.querySelector('form[action*="/apply"], form.application-form, form#application-form'),
                                            bodyHead: (document.body && document.body.innerText || '').slice(0, 300),
                                        })"""
                                    )
                                    logger.info(f"[AgentLoop] Lever @SUCCESS snap: {_sig_snap}")
                                except Exception as _e:
                                    logger.info(
                                        f"[AgentLoop] Lever @SUCCESS snap failed ({_e}) — "
                                        "context likely destroyed by post-solve navigation."
                                    )
                                if signal == ChallengeSignal.SUCCESS and _lever_agentv.cr_list:
                                    # Read the solved token directly from AgentV's
                                    # own captured CaptchaResponse instead of
                                    # re-querying the DOM immediately — hCaptcha's
                                    # internal iframes commonly reload right after
                                    # a challenge round completes, which destroys
                                    # the execution context and made the naive
                                    # re-query below race and fail on the first
                                    # live test (signal=SUCCESS was thrown away).
                                    _cr = _lever_agentv.cr_list[-1]
                                    _agentv_token = _cr.generated_pass_UUID or None
                                    if _agentv_token:
                                        logger.info(
                                            "[AgentLoop] Lever: AgentV solved the "
                                            "challenge — injecting its token directly "
                                            "(skipping DOM re-query)."
                                        )
                                        # Page may have just navigated/reloaded an
                                        # internal iframe from the challenge round —
                                        # give it a moment before touching the DOM.
                                        await page.wait_for_timeout(1000)
                                        try:
                                            await page.evaluate(
                                                """(token) => {
                                                    const el = document.getElementById('hcaptchaResponseInput');
                                                    if (el) {
                                                        el.value = token;
                                                        el.dispatchEvent(new Event('change', {bubbles: true}));
                                                    }
                                                }""",
                                                _agentv_token,
                                            )
                                            _hc_value = _agentv_token
                                        except Exception as exc:
                                            logger.warning(
                                                f"[AgentLoop] Lever: AgentV token injection failed: {exc}"
                                            )
                            except Exception as exc:
                                logger.warning(f"[AgentLoop] Lever: AgentV attempt failed (non-fatal): {exc}")
                                # Salvage: AgentV's page.on("response") listener
                                # may have captured a pass:true CaptchaResponse
                                # BEFORE wait_for_challenge decided there was no
                                # challenge frame to solve (invisible pass case).
                                # Its own cr_list is the source of truth for
                                # "did hCaptcha give us a token" — check it
                                # regardless of whether wait_for_challenge threw.
                                try:
                                    if _lever_agentv is not None and _lever_agentv.cr_list:
                                        _cr_salv = _lever_agentv.cr_list[-1]
                                        _t_salv = _cr_salv.generated_pass_UUID or None
                                        if _t_salv:
                                            logger.info(
                                                "[AgentLoop] Lever: AgentV wait threw "
                                                "but its network listener captured a "
                                                "passing response — using its token."
                                            )
                                            try:
                                                await page.evaluate(
                                                    """(token) => {
                                                        const el = document.getElementById('hcaptchaResponseInput');
                                                        if (el) {
                                                            el.value = token;
                                                            el.dispatchEvent(new Event('change', {bubbles: true}));
                                                        }
                                                    }""",
                                                    _t_salv,
                                                )
                                            except Exception:
                                                pass
                                            _hc_value = _t_salv
                                except Exception:
                                    pass
                                # Whether or not we salvaged from cr_list, give
                                # Lever's own invisible resolution one more
                                # chance to populate the input before falling
                                # to the deterministic (NopeCHA / fail) path.
                                if not _hc_value:
                                    for _post_tick in range(10):  # up to 5s more
                                        await asyncio.sleep(0.5)
                                        try:
                                            _v = await page.evaluate(
                                                "() => { const el = document.getElementById('hcaptchaResponseInput'); "
                                                "return el ? el.value : null; }"
                                            )
                                        except Exception:
                                            continue
                                        if _v:
                                            _hc_value = _v
                                            logger.info(
                                                f"[AgentLoop] Lever: token appeared "
                                                f"post-AgentV-fail at t={(_post_tick+1)*0.5:.1f}s "
                                                "— Lever's own invisible resolution."
                                            )
                                            break
                        if _hc_value is not None and not _hc_value:
                            logger.info(
                                "[AgentLoop] Lever: submit clicked but hCaptcha token not "
                                "set — solving hCaptcha deterministically."
                            )
                            from ..captcha.service import CaptchaService
                            # Hard-coded to nopecha (not the global CAPTCHA_PROVIDER)
                            # deliberately: CapSolver's account doesn't support
                            # hCaptcha on this site ("we don't support this
                            # service" — confirmed live). NopeCHA solves the
                            # actual widget via local AI, not a generic token
                            # farm, and its Token API explicitly targets
                            # invisible/programmatic hcaptcha.execute() sites
                            # like Lever's. Scoped to this Lever-only call site
                            # so Greenhouse/Ashby/Talent/Dice's already-validated
                            # CapSolver-based flows are untouched.
                            _captcha_svc = CaptchaService(provider="nopecha")
                            _solution = await _captcha_svc.solve(page, "hcaptcha")
                            if _solution.success and _solution.token:
                                await _hc_ctx.evaluate(
                                    """(token) => {
                                        const el = document.getElementById('hcaptchaResponseInput');
                                        if (el) {
                                            el.value = token;
                                            el.dispatchEvent(new Event('change', {bubbles: true}));
                                        }
                                    }""",
                                    _solution.token,
                                )
                                _hc_value = _solution.token
                            else:
                                logger.warning(
                                    "[AgentLoop] Lever: hCaptcha solve failed — submission "
                                    "did not go through this attempt."
                                )
                                ok = False
                                action.ok = False
                        # Regardless of which path produced the token (Lever's own
                        # natural invisible resolution, AgentV, or NopeCHA), Lever's
                        # onSuccess callback that would normally auto-click the real
                        # hidden submit button is unreachable from outside (scoped
                        # inside a closure, not window.onSuccess) — so ANY genuine
                        # token still requires us to click #hcaptchaSubmitBtn
                        # ourselves. class="hidden" is display:none, which has NO
                        # bounding box at all — Playwright's force=True only skips
                        # actionability checks (visible/stable/covered), it still
                        # needs a bounding box to compute click coordinates, so it
                        # fails with "Element is not visible" on a display:none
                        # target regardless. A JS-level .click() call sidesteps
                        # that entirely — it invokes the browser's native click
                        # handling (and the form's submit-button semantics) with
                        # no coordinate/visibility requirement at all. Confirmed
                        # live: locator.click(force=True) failed here in practice.
                        if _hc_value:
                            # Fast path: Lever's own onSuccess callback commonly
                            # fires the form POST directly (multipart/form-data
                            # to /apply) 5-10ms after ChallengeSignal.SUCCESS
                            # and the server responds 302 → /thanks. By the time
                            # our code gets here the win has ALREADY happened —
                            # the button we're about to poll for is legitimately
                            # gone because the page navigated. Check first:
                            #   (a) has the URL changed to /thanks / a
                            #       confirmation path?
                            #   (b) did our NET listener already log a
                            #       success-shaped POST response?
                            # If either is true, log terminal success and skip
                            # the click loop entirely — otherwise it errors
                            # "hidden submit button never became reachable"
                            # even though we actually won.
                            _url_now = ""
                            try:
                                _url_now = page.url or ""
                            except Exception:
                                pass
                            _net_win = any(
                                ev.get("kind") == "response"
                                and 200 <= ev.get("status", 0) < 400
                                and "/apply" in ev.get("url", "")
                                for ev in _lever_net_events
                            )
                            if "/thanks" in _url_now.lower() or _net_win:
                                logger.info(
                                    f"[AgentLoop] Lever: submission already landed "
                                    f"before pre-click poll (url={_url_now!r}, "
                                    f"net_win={_net_win}) — skipping button-click loop."
                                )
                                # Give the post-submit verification phase the
                                # normal `submit_fired` flow — leave `ok` True.
                                # (Fall through past the pre-click loop below.)
                                _clicked = True  # sentinel: treat as already-clicked
                                _lever_submit_confirmed = True  # for the terminal branch
                            else:
                                _clicked = False
                                _lever_submit_confirmed = False

                            # Live evidence from a prior real run showed exactly
                            # what happens after AgentV SUCCESS on Lever:
                            #   t=0.0s   Execution context destroyed
                            #            (page/frame reload during hCaptcha's
                            #             own crumb-round reset)
                            #   t=0.5s   hasForm=False, hasBtn=False
                            #            (form briefly removed from DOM)
                            #   t=1.0s   hasForm=False, hasBtn=False
                            #   t=1.5s   hasForm=True,  hasBtn=True
                            #            (form + hidden submit button
                            #             REAPPEAR — this is the window)
                            #   t=2.0s+  form stays present until we click
                            # So the correct move is NOT to click once at t=0
                            # (button not there yet) and NOT to look for URL
                            # change (there won't be one until AFTER our click)
                            # — it's to poll for the button + token to become
                            # reachable together, then click, THEN watch for
                            # terminal state.
                            _url_before_click = page.url
                            # Preferentially wait for hCaptcha's OWN callback to
                            # write a fresh token into the input after the reset
                            # — re-injecting our round-1 token (which is what
                            # this branch used to do) makes the form POST a
                            # stale token that Lever's server silently rejects
                            # (confirmed: form stays present, no navigation, no
                            # thank-you, even though our click "succeeded"
                            # locally). Only fall back to injecting our saved
                            # token if hCaptcha's callback hasn't fired after
                            # ~6s of waiting — at that point the stale token is
                            # better than nothing.
                            _fresh_token_deadline = 12  # 6s @ 0.5s per tick
                            _reinjected = False
                            for _wait_tick in range(0 if _clicked else 30):  # up to 15s total; skipped on fast-path
                                try:
                                    _state = await page.evaluate(
                                        """() => {
                                            const btn = document.getElementById('hcaptchaSubmitBtn');
                                            const inp = document.getElementById('hcaptchaResponseInput');
                                            return {
                                                hasBtn: !!btn,
                                                hasInp: !!inp,
                                                inpLen: (inp && inp.value || '').length,
                                            };
                                        }"""
                                    )
                                except Exception as _e:
                                    logger.debug(
                                        f"[AgentLoop] Lever pre-click wait t={_wait_tick*0.5:.1f}s "
                                        f"context not ready ({_e}); waiting."
                                    )
                                    await asyncio.sleep(0.5)
                                    continue

                                if _state.get("hasBtn") and _state.get("hasInp"):
                                    _inp_len = _state.get("inpLen", 0)
                                    if _inp_len > 0:
                                        # hCaptcha's own callback wrote a fresh
                                        # token OR our reinjection landed and
                                        # nothing overwrote it. Either way the
                                        # form-side belief matches what will get
                                        # POSTed — click now.
                                        try:
                                            _clicked = await page.evaluate(
                                                "() => { const el = document.getElementById('hcaptchaSubmitBtn'); "
                                                "if (el) { el.click(); return true; } return false; }"
                                            )
                                        except Exception as _e3:
                                            logger.debug(
                                                f"[AgentLoop] Lever click race @t={_wait_tick*0.5:.1f}s: "
                                                f"{_e3}; retrying."
                                            )
                                            await asyncio.sleep(0.5)
                                            continue
                                        if _clicked:
                                            logger.info(
                                                f"[AgentLoop] Lever: #hcaptchaSubmitBtn clicked "
                                                f"at t={_wait_tick*0.5:.1f}s "
                                                f"(inp_len={_inp_len} — hCaptcha's own token"
                                                f"{' + our reinjection fallback' if _reinjected else ''})."
                                            )
                                            break
                                    else:
                                        # Button back, input empty. Give hCaptcha
                                        # up to _fresh_token_deadline ticks to
                                        # write its own token; then fall back to
                                        # our saved round-1 value.
                                        if _wait_tick >= _fresh_token_deadline and not _reinjected:
                                            logger.info(
                                                f"[AgentLoop] Lever t={_wait_tick*0.5:.1f}s: "
                                                "hCaptcha's own callback hasn't written a "
                                                "fresh token within 6s — falling back to "
                                                "reinjecting our saved token."
                                            )
                                            try:
                                                await page.evaluate(
                                                    """(token) => {
                                                        const el = document.getElementById('hcaptchaResponseInput');
                                                        if (el) {
                                                            el.value = token;
                                                            el.dispatchEvent(new Event('change', {bubbles: true}));
                                                        }
                                                    }""",
                                                    _hc_value,
                                                )
                                                _reinjected = True
                                            except Exception as _e2:
                                                logger.warning(
                                                    f"[AgentLoop] Lever token reinject failed: {_e2}"
                                                )
                                await asyncio.sleep(0.5)

                            if not _clicked:
                                logger.error(
                                    "[AgentLoop] Lever: hidden submit button never "
                                    "became reachable within 10s of SUCCESS."
                                )
                                ok = False
                                action.ok = False
                            else:
                                # Now watch for terminal-state signals: URL
                                # change, form gone, thank-you text. Give the
                                # click ~8s to propagate through Lever's
                                # server-side submission handler.
                                _lever_submit_confirmed = False
                                _ctx_destroyed_streak = 0
                                for _tick in range(16):
                                    await asyncio.sleep(0.5)
                                    try:
                                        _snap = await page.evaluate(
                                            """() => ({
                                                url: window.location.href,
                                                title: document.title,
                                                hasForm: !!document.querySelector('form[action*="/apply"], form.application-form, form#application-form'),
                                                hasThanks: /thank\\s*you|application (?:sent|received|submitted)|we've received|received your application|success(?:fully)?\\s+(?:submitted|applied)/i.test(
                                                    (document.body && document.body.innerText) || ''
                                                ),
                                            })"""
                                        )
                                        _ctx_destroyed_streak = 0
                                        logger.info(
                                            f"[AgentLoop] Lever post-click-real t={(_tick+1)*0.5:.1f}s: "
                                            f"url={_snap.get('url')!r} "
                                            f"hasForm={_snap.get('hasForm')} "
                                            f"hasThanks={_snap.get('hasThanks')}"
                                        )
                                        if _snap.get("hasThanks"):
                                            logger.info(
                                                "[AgentLoop] Lever: terminal SUCCESS — "
                                                "thank-you/received string present."
                                            )
                                            _lever_submit_confirmed = True
                                            break
                                        if (not _snap.get("hasForm")) and _snap.get("url") != _url_before_click:
                                            logger.info(
                                                "[AgentLoop] Lever: terminal SUCCESS — "
                                                "URL changed and form gone."
                                            )
                                            _lever_submit_confirmed = True
                                            break
                                    except Exception as _e:
                                        _ctx_destroyed_streak += 1
                                        logger.info(
                                            f"[AgentLoop] Lever post-click-real evaluate threw "
                                            f"({_e}) — streak={_ctx_destroyed_streak}."
                                        )
                                        if _ctx_destroyed_streak >= 3:
                                            logger.info(
                                                "[AgentLoop] Lever: repeated context-destroyed "
                                                "post-click — treating as in-flight submission."
                                            )
                                            _lever_submit_confirmed = True
                                            break

                                if _lever_submit_confirmed:
                                    logger.info(
                                        "[AgentLoop] Lever: submission confirmed."
                                    )
                                else:
                                    logger.info(
                                        "[AgentLoop] Lever: click landed but no explicit "
                                        "thank-you within 8s — trusting downstream "
                                        "verification to catch it."
                                    )
                    except Exception as exc:
                        logger.debug(f"[AgentLoop] Lever hCaptcha handling error (non-fatal): {exc}")

                if ok and _is_submit:
                    # Flip into the dedicated POST-SUBMIT verification phase.
                    # The top-of-loop gate now owns code detection + Gmail
                    # fetch + code fill — no more form re-filling after submit.
                    submit_fired = True
                    self._submit_fired = True   # executor reads this to block the scripted re-submit fallback
                    _submit_epoch = _pre_exec_epoch - 60
                    logger.info(
                        f"[AgentLoop] step={step} SUBMIT fired — entering "
                        "post-submit verification phase (form-fill disabled)."
                    )

                # Lever 2: persist every successful fill into field_memory so
                # the NEXT application this candidate submits skips the LLM
                # entirely for that field. The recall happens in the pre-LLM
                # check below (see _try_memory_prefill).
                if ok and action.kind == "fill_field" and action.field_label and action.value:
                    # Do NOT memorize per-application LOCATION fields. They're
                    # job-specific, not stable identity, and they're the source
                    # of the recurring "memory poisoning" (e.g. an address from
                    # one run, or a Yes/No that aliases onto a 'state' field,
                    # getting wrongly recalled on the next application).
                    _lbl_l = (action.field_label or "").strip().lower()
                    _skip_remember = any(
                        kw in _lbl_l for kw in (
                            "address", "street", "city", "state", "province",
                            "postal", "zip", "post code",
                        )
                    )
                    if not _skip_remember:
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
                        # Before declaring STUCK on fill_field, check the actual
                        # DOM value. React/SPA forms sometimes don't reflect the
                        # memory pre-fill (native event setter) visually, causing
                        # the LLM to keep re-issuing the same fill. Two cases:
                        #  1. DOM already has correct value → LLM is confused;
                        #     just skip the action and continue.
                        #  2. DOM has wrong/empty value → force Playwright fill+Tab
                        #     to commit it properly to the SPA state.
                        if action.kind == "fill_field" and action.selector and action.value:
                            try:
                                target_val = str(action.value)
                                loc = (frame or page).locator(action.selector).first
                                if await loc.count() > 0:
                                    current_val = await loc.evaluate("el => el.value || ''")
                                    if current_val.strip() == target_val.strip():
                                        # Value is already there — LLM is just confused
                                        logger.warning(
                                            f"[AgentLoop] step={step} REPETITION GUARD: "
                                            f"{action.selector!r} already has correct value "
                                            f"'{current_val[:40]}' — skipping redundant fill."
                                        )
                                        actions.append(action)
                                        continue
                                    else:
                                        # Value is wrong — force-fill to commit to SPA
                                        logger.warning(
                                            f"[AgentLoop] step={step} REPETITION GUARD rescue: "
                                            f"force-filling {action.selector!r} = '{target_val[:40]}' "
                                            f"(DOM had '{current_val[:40]}'). SPA did not commit pre-fill."
                                        )
                                        await loc.click(timeout=3000)
                                        await loc.fill(target_val, timeout=3000)
                                        await page.keyboard.press("Tab")
                                        await asyncio.sleep(0.5)
                                        same_action_run = []  # reset — don't abort
                            except Exception as _rg_exc:
                                logger.debug(f"[AgentLoop] REPETITION GUARD rescue failed: {_rg_exc}")

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
